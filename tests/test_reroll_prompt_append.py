"""
G2 re-roll with a prompt amendment (ep05full: Studio's headcount wording for F01/F06/F07):
retry_shot(shot_id, prompt_append) re-rolls a still that SUCCEEDED (shot_attempts) and appends the
text to that retry's prompt only; the original prompt is unchanged, G2 then animates the retry.

Every activity is stubbed by name: no network, no spend.
"""
import asyncio
import time
from datetime import timedelta

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker


def _activities(log):
    def mk(name, fn):
        async def w(*a):
            return await fn(*a)
        return activity.defn(name=name)(w)

    async def ok(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}
    async def parse(*a):
        return {"shots": [{"shot_id": "A02", "prompt": "Exactly 0 animals in frame. Real-time, no slow motion. Probe shot.",
                           "duration": 3.0, "params": {"end_frame": False}}]}
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
    async def path(ep, sid, url, kind, ver=1, *rest):
        log.append(("save", sid, kind, ver))
        return f"/tmp/fake-{kind}-v{ver}"
    async def passed(*a): return {"passed": True, "issues": []}
    async def submit_still(ep, sid, prompt, version, *rest):
        log.append(("submit_still", sid, version, prompt))
        return {"job_id": f"still-{ep}-{sid}-v{version}", "line_name": "L", "reserved_amount": 1}
    polled = set()
    async def poll(job_id, *a):
        if job_id.startswith("still-") and job_id not in polled:
            polled.add(job_id)
            return {"status": "processing"}
        log.append(("landed", job_id))
        return {"status": "completed", "output_url": f"fake://{job_id}.jpg", "cost": 1}
    async def submit_clip(ep, sid, *rest):
        log.append(("submit_clip", sid, rest))
        return {"job_id": f"clip-{ep}-{sid}", "line_name": "L", "reserved_amount": 1}

    out = [mk(n, f) for n, f in [
        ("acquire_run_lock", true_), ("release_run_lock", true_), ("load_gate_policy_activity", empty_d),
        ("parse_beatmap_activity", parse), ("load_refs_to_generate_activity", empty_l), ("generate_run_report", empty_d),
        ("compute_g1_plan_activity", g1), ("init_episode_budget_lines_activity", binit), ("approve_g2_shots", zero),
        ("list_anchor_stills", empty_l), ("precheck_still_qc", passed), ("submit_still_job_enforced", submit_still),
        ("poll_job_status", poll), ("commit_job_budget", none), ("commit_credits_activity", none),
        ("release_job_budget", none), ("release_credits_activity", none), ("save_asset_locally", path),
        ("review_still", passed), ("record_shot_result", none), ("mark_job_pending_reconcile", none),
        ("review_clip", passed), ("precheck_clip_qc", passed), ("submit_clip_job_enforced", submit_clip)]]
    out += [mk(n, ok) for n in [
        "init_episode_credit_line_activity", "preflight_validate_episode_activity", "upsert_episode_activity",
        "verify_episode_files_activity", "approve_g108_activity", "recheck_refs_activity", "update_credit_cap_activity"]]
    return out



AMEND = "EXACTLY THREE capybaras: two adults (char_r, char_h) and one small pup (char_m)."


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_reroll_with_prompt_append(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    log = []
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

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="reroll-amend", workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(log)):
            h = await env.client.start_workflow(EpisodeWorkflowV2.run, args=["ep05", None, True, ["A02"], 1000],
                                                id="ep05", task_queue="reroll-amend")
            try:
                st, ok = await wait(h, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
                assert ok, st
                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                st, ok = await wait(h, lambda s: s.get("current_gate") == "G2_SCENE_STILLS"
                                    and any(e[0] == "landed" for e in log), budget=30.0)
                assert ok, st
                # bad amendments are refused before anything is launched
                for bad in ("   ", "x" * 1001):
                    with pytest.raises(WorkflowUpdateFailedError):
                        await h.execute_update(EpisodeWorkflowV2.retry_shot, args=["A02", bad])
                plan = await h.query(EpisodeWorkflowV2.retry_prompt_plan, args=["A02", AMEND])
                assert plan["found"] and plan["clip_prompt"].endswith(" " + AMEND) and plan["clip_model"] == "kling"
                r = await h.execute_update(EpisodeWorkflowV2.retry_shot, args=["A02", f"  {AMEND}\n "])
                assert r["status"] == "queued" and r["prompt_amendments"] == [AMEND], r
                st, ok = await wait(h, lambda s: not s.get("pending_retries"), budget=30.0)
                assert ok, st
                stills = [e for e in log if e[0] == "submit_still"]
                assert [e[2] for e in stills] == [1, 2], log
                assert AMEND not in stills[0][3]
                assert stills[1][3].startswith(stills[0][3].rstrip()) and stills[1][3].endswith(" " + AMEND)

                # GAP-14: the re-roll lands as still_v2, never over the original still_v1
                saves = [e for e in log if e[0] == "save" and e[1] == "A02" and e[2] == "still"]
                assert [e[3] for e in saves] == [1, 2], log

                await h.execute_update(EpisodeWorkflowV2.approve_g2_scene_stills)
                end = time.time() + 15
                while time.time() < end and not any(e[0] == "submit_clip" for e in log):
                    await env.sleep(timedelta(seconds=1))
                clips = [e for e in log if e[0] == "submit_clip"]
                assert len(clips) == 1, log  # only the re-roll is animated
                assert any(isinstance(a, str) and a.endswith(AMEND) for a in clips[0][2]), clips
                end = time.time() + 10
                while time.time() < end and not any(e[0] == "save" and e[2] == "clip" for e in log):
                    await env.sleep(timedelta(seconds=1))
                assert [e[3] for e in log if e[0] == "save" and e[2] == "clip"] == [2], log
            finally:
                for wid in ("ep05", "ep05-shot-A02", "ep05-shot-A02-retry1"):
                    try:
                        await env.client.get_workflow_handle(wid).terminate("test done")
                    except Exception:
                        pass
