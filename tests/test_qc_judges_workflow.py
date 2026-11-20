"""The QC judges inside the real ShotWorkflow (docs/QC-JUDGES.md). The still review gets the judges'
rubric (the still's prompt, references, cast and local file), and a judge's FAIL is never re-rolled
automatically: the shot fails, and only the bounded ``retry_shot`` makes another still (the QC retry
loop removed in 10e9fd2 stays gone). Every activity is stubbed by name: no network, no spend."""
import asyncio
import base64
import json
import time
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS

PROMPT = "Exactly 1 animals in frame. Pip the otter sits on a dock. Real-time, no slow motion."
META = {"count": 1, "characters": ["Pip"], "duck": False}
JUDGE_FAIL = "QC judge: two otters in frame, the prompt has one"


def _patch_markers(history) -> list[str]:
    """The patch ids recorded in a workflow history."""
    ids = []
    for event in json.loads(history.to_json())["events"]:
        marker = event.get("markerRecordedEventAttributes") or {}
        for payload in (marker.get("details", {}).get("patch-data", {}).get("payloads") or []):
            ids.append(json.loads(base64.b64decode(payload["data"])).get("id"))
    return ids


def _activities(log, still_dir):
    def mk(name, fn):
        async def w(*a):
            return await fn(*a)
        return activity.defn(name=name)(w)

    async def ok(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}
    async def parse(*a):
        return {"shots": [{"shot_id": "A02", "prompt": PROMPT, "duration": 3.0, "params": {},
                           "shot_meta": dict(META)}]}
    async def g1(*a):
        return {"shots": [{"shot_id": "A02", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21}],
                "total_credits": 0.21, "total_usd": "0.21", "estimates_available": True}
    async def binit(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}
    async def true_(*a): return True
    async def empty_d(*a): return {}
    async def empty_l(*a): return []
    async def zero(*a): return 0
    async def none(*a): return None
    async def passed(*a): return {"passed": True, "issues": []}
    async def save(ep, sid, url, kind, version, *rest):
        return str(still_dir / f"{sid}_{kind}_v{version}.png")
    async def submit_still(ep, sid, prompt, version, *rest):
        log.append(("submit_still", sid, version))
        return {"job_id": f"still-{ep}-{sid}-v{version}", "line_name": "L", "reserved_amount": 1}
    async def poll(job_id, *a):
        return {"status": "completed", "output_url": f"fake://{job_id}.jpg", "cost": 1}
    async def review_still(url, rubric, ep, sid):
        reviews = [e for e in log if e[0] == "review_still"]
        log.append(("review_still", url, rubric))
        if not reviews:
            return {"passed": False, "issues": [JUDGE_FAIL], "escalate": False}
        return {"passed": True, "issues": []}
    async def submit_clip(ep, sid, *rest):
        log.append(("submit_clip", sid))
        return {"job_id": f"clip-{ep}-{sid}", "line_name": "L", "reserved_amount": 1}

    out = [mk(n, f) for n, f in [
        ("acquire_run_lock", true_), ("release_run_lock", true_), ("load_gate_policy_activity", empty_d),
        ("parse_beatmap_activity", parse), ("load_refs_to_generate_activity", empty_l), ("generate_run_report", empty_d),
        ("compute_g1_plan_activity", g1), ("init_episode_budget_lines_activity", binit), ("approve_g2_shots", zero),
        ("list_anchor_stills", empty_l), ("precheck_still_qc", passed), ("submit_still_job_enforced", submit_still),
        ("poll_job_status", poll), ("commit_job_budget", none), ("commit_credits_activity", none),
        ("release_job_budget", none), ("release_credits_activity", none), ("save_asset_locally", save),
        ("review_still", review_still), ("record_shot_result", none), ("mark_job_pending_reconcile", none),
        ("review_clip", passed), ("precheck_clip_qc", passed), ("submit_clip_job_enforced", submit_clip)]]
    out += [mk(n, ok) for n in [
        "init_episode_credit_line_activity", "preflight_validate_episode_activity", "upsert_episode_activity",
        "verify_episode_files_activity", "approve_g108_activity", "recheck_refs_activity", "update_credit_cap_activity"]]
    return out


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_a_judged_fail_is_rerolled_only_by_retry_shot_and_the_review_gets_the_rubric(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    log = []
    still_dir = tmp_path / "stills"

    async def state(h):
        return await asyncio.wait_for(h.query(EpisodeWorkflowV2.get_state, rpc_timeout=timedelta(seconds=2)), 3)

    async def wait(h, pred, budget=10.0):
        end, st = time.time() + budget, None
        while time.time() < end:
            try:
                st = await state(h)
                if pred(st):
                    return st, True
            except Exception:
                pass
            await env.sleep(timedelta(seconds=1))
        return st, False

    def reviews():
        return [e for e in log if e[0] == "review_still"]

    def stills():
        return [e for e in log if e[0] == "submit_still"]

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="qc-judges-shot", workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(log, still_dir)):
            h = await env.client.start_workflow(EpisodeWorkflowV2.run, args=["ep05", None, True, ["A02"], 1000],
                                                id="ep05", task_queue="qc-judges-shot")
            try:
                st, ok = await wait(h, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
                assert ok, st
                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                st, ok = await wait(h, lambda s: s.get("failed_shots"))
                assert ok, f"a judged FAIL never failed the shot: {st}"

                _, url, rubric = reviews()[0]
                assert url == "fake://still-ep05-A02-v1.jpg"
                assert rubric == {"prompt": PROMPT, "refs": [], "shot_meta": META,
                                  "still_path": str(still_dir / "A02_still_v1.png")}, rubric

                # Workflow time passes with the shot failed: nothing re-rolls it on its own
                await env.sleep(timedelta(minutes=30))
                assert len(stills()) == 1 and len(reviews()) == 1, log
                assert not any(e[0] == "submit_clip" for e in log), log

                r = await h.execute_update(EpisodeWorkflowV2.retry_shot, args=["A02"])
                assert r["status"] == "queued", r
                end = time.time() + 30
                while time.time() < end and len(reviews()) < 2:
                    await env.sleep(timedelta(seconds=1))
                assert len(stills()) == 2 and len(reviews()) == 2, f"retry_shot never re-rolled the still: {log}"
                _, url, rubric = reviews()[1]
                assert url != "fake://still-ep05-A02-v1.jpg"
                assert rubric["prompt"] == PROMPT and rubric["shot_meta"] == META, rubric
                st, ok = await wait(h, lambda s: not s.get("pending_retries"), budget=30.0)
                assert ok, f"the retried still passed review but the retry never finished: {st}"
                assert len(stills()) == 2, f"a passing review re-rolled again: {log}"

                # A history recorded with the patch carries its marker and replays on this code
                history = await env.client.get_workflow_handle("ep05-shot-A02").fetch_history()
                assert "qc-still-rubric" in _patch_markers(history), _patch_markers(history)
                replay = await Replayer(workflows=PRODUCTION_WORKFLOWS,
                                        data_converter=temporal_data_converter).replay_workflow(history)
                assert replay.replay_failure is None
            finally:
                for wid in ("ep05", "ep05-shot-A02-retry1"):
                    try:
                        await env.client.get_workflow_handle(wid).terminate("test done")
                    except Exception:
                        pass
