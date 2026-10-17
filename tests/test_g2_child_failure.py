"""Gate-2 child-failure behaviour (box review tests).

(a) A01 fails before G2 while A02 waits for stills approval: failure recorded, no hang,
    G2 still requires approval, and no shot is released to clips before approval.
(b) All shots succeed at the stills stage: G2 still requires approval.
"""
import asyncio
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

SHOTS = [{"shot_id": "A01", "prompt": "p1"}, {"shot_id": "A02", "prompt": "p2"}]
CLIP_RELEASED: list[str] = []


@workflow.defn(name="ShotWorkflow", sandboxed=False)
class FakeShot:
    def __init__(self):
        self.approved = False

    @workflow.signal
    def stills_approved(self):
        self.approved = True

    @workflow.signal
    def resume_after_cap_raise(self):
        pass

    @workflow.run
    async def run(self, episode_id: str, plan: dict) -> dict:
        if plan.get("fail"):
            raise ApplicationError(f"{plan['shot_id']} still failed", non_retryable=True)
        await workflow.wait_condition(lambda: self.approved)
        CLIP_RELEASED.append(plan["shot_id"])
        return {"shot_id": plan["shot_id"], "status": "success"}


def _acts(fail_ids):
    @activity.defn(name="acquire_run_lock")
    async def lock(*a): return True

    @activity.defn(name="release_run_lock")
    async def unlock(*a): return True

    @activity.defn(name="load_gate_policy_activity")
    async def policy(*a): return {}

    @activity.defn(name="parse_beatmap_activity")
    async def parse(*a):
        return {"shots": [{**s, "fail": s["shot_id"] in fail_ids} for s in SHOTS]}

    @activity.defn(name="load_refs_to_generate_activity")
    async def refs(*a): return []

    @activity.defn(name="generate_run_report")
    async def report(*a): return {}

    async def noop(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}
    
    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_init(*a):
        # Return proper structure expected by workflow
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}

    names = ["init_episode_credit_line_activity",
             "preflight_validate_episode_activity",
             "upsert_episode_activity", "verify_episode_files_activity", "approve_g108_activity",
             "recheck_refs_activity", "update_credit_cap_activity", "compute_g1_plan_activity"]
    def mk(n):
        async def f(*a):
            return await noop(*a)
        return activity.defn(name=n)(f)
    extra = [mk(n) for n in names]
    return [lock, unlock, policy, parse, refs, report, budget_init, *extra]


async def _state(h):
    return await h.query(EpisodeWorkflowV2.get_state)


async def _wait_for(h, pred, timeout=10.0):
    loop = asyncio.get_event_loop(); end = loop.time() + timeout
    while loop.time() < end:
        s = await _state(h)
        if pred(s):
            return s
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not reached; last state={s}")


async def _run(fail_ids):
    CLIP_RELEASED.clear()
    tq = f"g2-{uuid.uuid4().hex[:6]}"
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=tq, workflows=[EpisodeWorkflowV2, FakeShot],
                          activities=_acts(fail_ids)):
            h = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=["ep05", None, True, ["A01", "A02"], 40],
                id=f"ep05-{uuid.uuid4().hex[:6]}", task_queue=tq)
            await _wait_for(h, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await h.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            s = await _wait_for(h, lambda s: s["current_gate"] == "G2_SCENE_STILLS")
            return env, h, s


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_a_failed_shot_recorded_no_hang_and_g2_still_needs_approval(caplog):
    CLIP_RELEASED.clear()
    tq = f"g2-{uuid.uuid4().hex[:6]}"
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=tq, workflows=[EpisodeWorkflowV2, FakeShot],
                          activities=_acts({"A01"})):
            h = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=["ep05", None, True, ["A01", "A02"], 40],
                id=f"ep05-{uuid.uuid4().hex[:6]}", task_queue=tq)
            await _wait_for(h, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await h.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            # failure is recorded while A02 still waits for stills approval
            s = await _wait_for(h, lambda s: any(f["shot_id"] == "A01" for f in s["failed_shots"]))
            assert s["current_gate"] == "G2_SCENE_STILLS"
            assert "G2_SCENE_STILLS" not in s["passed_gates"]
            # the any-failed exit fired: workflow logged the gate-2 failure summary (no hang in wait 1)
            await asyncio.sleep(1.0)
            assert any("approval still required" in r.getMessage() for r in caplog.records), \
                "first G2 wait did not exit on the A01 failure"
            # G2 still needs approval and nothing was released to clips
            await asyncio.sleep(1.0)
            s = await _state(h)
            assert s["approvals"]["g2_scene_stills"] is False
            assert "G2_SCENE_STILLS" not in s["passed_gates"]
            assert CLIP_RELEASED == []
            await h.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_b_all_shots_ok_g2_still_needs_approval():
    CLIP_RELEASED.clear()
    tq = f"g2-{uuid.uuid4().hex[:6]}"
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=tq, workflows=[EpisodeWorkflowV2, FakeShot],
                          activities=_acts(set())):
            h = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=["ep05", None, True, ["A01", "A02"], 40],
                id=f"ep05-{uuid.uuid4().hex[:6]}", task_queue=tq)
            await _wait_for(h, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await h.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            await _wait_for(h, lambda s: s["current_gate"] == "G2_SCENE_STILLS")
            await asyncio.sleep(2.0)
            s = await _state(h)
            assert s["failed_shots"] == []
            assert "G2_SCENE_STILLS" not in s["passed_gates"]
            assert CLIP_RELEASED == []
            # approval releases clips
            await h.signal(EpisodeWorkflowV2.approve_g2_scene_stills)
            await _wait_for(h, lambda s: "G2_SCENE_STILLS" in s["passed_gates"])
            await asyncio.sleep(1.0)
            assert sorted(CLIP_RELEASED) == ["A01", "A02"]
            await h.terminate("test done")
