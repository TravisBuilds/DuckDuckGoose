"""
The REAL hfvg ShotWorkflow (not a test double) must send retry_still_ready to the episode
after a retry's still(s) have landed (start + end frame, QC'd, recorded) and BEFORE it waits
for stills_approved. Otherwise G2's validator (refuses while pending_retries is non-empty)
and the retry child (waits for the stills_approved that only G2 sends) deadlock forever.

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
                           "duration": 3.0, "params": {"end_frame": True}}]}
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
    async def path(*a): return "/tmp/fake-still.jpg"
    async def passed(*a): return {"passed": True, "issues": []}
    async def submit_still(ep, sid, prompt, version, *rest):
        log.append(("submit_still", sid, version))
        if version == 1:
            raise ApplicationError("original still fails", type="ProbeFail", non_retryable=True)
        return {"job_id": f"still-{ep}-{sid}-v{version}", "line_name": "L", "reserved_amount": 1}
    polled = set()
    async def poll(job_id, *a):
        if job_id.startswith("still-") and job_id not in polled:
            polled.add(job_id)
            return {"status": "processing"}
        log.append(("landed", job_id))
        return {"status": "completed", "output_url": f"fake://{job_id}.jpg", "cost": 1}
    async def submit_clip(ep, sid, *rest):
        log.append(("submit_clip", sid))
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


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_real_shot_workflow_retry_unblocks_g2_after_both_stills_land(tmp_path, monkeypatch):
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
            await asyncio.sleep(0.05)
        return st, False

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="real-shot-retry", workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(log)):
            h = await env.client.start_workflow(EpisodeWorkflowV2.run, args=["ep05", None, True, ["A02"], 1000],
                                                id="ep05", task_queue="real-shot-retry")
            try:
                st, ok = await wait(h, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
                assert ok, st
                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                st, ok = await wait(h, lambda s: s.get("failed_shots"))
                assert ok, f"original A02 never failed: {st}"
                r = await h.execute_update(EpisodeWorkflowV2.retry_shot, args=["A02"])
                assert r["status"] == "queued", r

                # pending_retries must clear on its own once the retry still(s) land (no G2 needed)
                # (each retry still's first poll says "processing" -> ShotWorkflow sleeps ~5s of
                # real time: queries do not unlock time skipping, so allow ~2x5s plus slack)
                st, ok = await wait(h, lambda s: not s.get("pending_retries"), budget=30.0)
                assert ok, f"real ShotWorkflow retry never cleared pending_retries (G2 deadlock): {st.get('pending_retries')}"
                landed = [e[1] for e in log if e[0] == "landed"]
                assert "still-ep05-A02-v2" in landed and "still-ep05-A02_end-v2" in landed, \
                    f"pending_retries cleared before both retry stills landed: {log}"
                assert not any(e[0] == "submit_clip" for e in log), f"clip started before G2: {log}"

                await h.execute_update(EpisodeWorkflowV2.approve_g2_scene_stills)
                end = time.time() + 10
                while time.time() < end and not any(e[0] == "submit_clip" for e in log):
                    await asyncio.sleep(0.05)
                assert any(e[0] == "submit_clip" for e in log), f"clip never started after G2: {log}"
            finally:
                for wid in ("ep05", "ep05-shot-A02-retry1"):
                    try:
                        await env.client.get_workflow_handle(wid).terminate("test done")
                    except Exception:
                        pass
