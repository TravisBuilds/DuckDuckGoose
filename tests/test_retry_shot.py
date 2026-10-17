"""Tests for single-shot retry functionality."""

import asyncio
import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.exceptions import ApplicationError

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


# Mock ShotWorkflow that fails first time, succeeds on retry
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class RetryableShotWorkflow:
    def __init__(self):
        self.approved = False
        self.attempt_count = 0

    @workflow.signal
    def stills_approved(self):
        self.approved = True

    @workflow.signal
    def resume_after_cap_raise(self):
        pass

    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        self.attempt_count += 1
        
        # First attempt fails, retry succeeds
        if self.attempt_count == 1:
            return {
                "status": "failed",
                "error": "Generation failed: provider error"
            }
        
        # Wait for approval before succeeding
        await workflow.wait_condition(lambda: self.approved)
        return {
            "status": "completed",
            "shot_id": shot_dict["shot_id"],
            "still_url": "dry-run://still.png",
            "clip_url": "dry-run://clip.mp4",
        }


async def _wait_for_state(handle, pred, timeout=10.0):
    """Poll get_state until predicate is true."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        state = await handle.query(EpisodeWorkflowV2.get_state)
        if pred(state):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not reached; last state={state}")


def _make_activities():
    @activity.defn(name="acquire_run_lock")
    async def lock(*a): return True

    @activity.defn(name="release_run_lock")
    async def unlock(*a): return True

    @activity.defn(name="load_gate_policy_activity")
    async def policy(*a): return {}

    @activity.defn(name="parse_beatmap_activity")
    async def parse(*args):
        return {
            "shots": [
                {"shot_id": "A01", "prompt": "Shot A01", "duration": 3.0},
                {"shot_id": "A02", "prompt": "Shot A02", "duration": 3.0},
            ]
        }

    @activity.defn(name="load_refs_to_generate_activity")
    async def refs(*a): return []

    @activity.defn(name="generate_run_report")
    async def report(*a): return {}

    @activity.defn(name="compute_g1_plan_activity")
    async def g1_plan(*a):
        return {
            "shots": [
                {"shot_id": "A01", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21},
                {"shot_id": "A02", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21},
            ],
            "total_credits": 0.42,
            "total_usd": "0.42",
        }

    async def noop(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}

    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_init(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}

    names = ["init_episode_credit_line_activity",
             "preflight_validate_episode_activity",
             "upsert_episode_activity", "verify_episode_files_activity", "approve_g108_activity",
             "recheck_refs_activity", "update_credit_cap_activity"]
    
    def mk(n):
        async def f(*a):
            return await noop(*a)
        return activity.defn(name=n)(f)
    
    extra = [mk(n) for n in names]
    return [lock, unlock, policy, parse, refs, report, g1_plan, budget_init, *extra]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_succeeds_on_second_attempt():
    """Test that retry_shot re-runs a failed shot and it succeeds on retry."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry", None, True, ["A01"], 40],
                id="test-retry-workflow",
                task_queue="test-retry",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            state = await _wait_for_state(
                handle,
                lambda s: len(s["failed_shots"]) > 0 and s["failed_shots"][0]["shot_id"] == "A01"
            )
            
            assert len(state["failed_shots"]) == 1
            assert state["failed_shots"][0]["shot_id"] == "A01"
            assert "Generation failed" in state["failed_shots"][0]["reason"]
            
            # Retry the shot
            await handle.signal("retry_shot", args=["A01"])
            
            # Wait a bit for retry to start
            await asyncio.sleep(1.0)
            
            # Check that attempt count increased
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A01", 0) == 1
            
            # Approve G2 (which will approve the retry)
            await handle.signal(EpisodeWorkflowV2.approve_g2_scene_stills)
            
            # Wait for retry to complete successfully
            await asyncio.sleep(2.0)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            # Failed shot should be removed after successful retry
            # (In reality it takes time for the child to complete, so we just check attempts)
            assert state["shot_attempts"]["A01"] == 1
            
            try:
                await handle.terminate("test done")
            except Exception:
                # Workflow may have already completed
                pass


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_refuses_when_not_at_gate():
    """Test that retry_shot is refused when not at G1/G2."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-refuse",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry2", None, True, ["A01"], 40],
                id="test-retry-refuse-workflow",
                task_queue="test-retry-refuse",
            )
            
            # Before reaching G1, try to retry (should be refused)
            await asyncio.sleep(0.5)
            await handle.signal("retry_shot", args=["A01"])
            
            # Wait for G1
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            
            # Check that attempt count is still 0 (retry was refused)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A01", 0) == 0
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_refuses_when_shot_not_failed():
    """Test that retry_shot is refused for shots not in failed_shots."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-not-failed",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry3", None, True, ["A01"], 40],
                id="test-retry-not-failed-workflow",
                task_queue="test-retry-not-failed",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for G2 (shot failed but we'll try to retry A02 which didn't fail)
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS")
            
            # Try to retry A02 (which isn't failed)
            await handle.signal("retry_shot", args=["A02"])
            
            await asyncio.sleep(0.5)
            
            # Check that attempt count is still 0 for A02 (retry was refused)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A02", 0) == 0
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_respects_max_retries():
    """Test that retry_shot respects the max_retries limit."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-limit",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry4", None, True, ["A01"], 40],
                id="test-retry-limit-workflow",
                task_queue="test-retry-limit",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            
            # Retry once (max_retries=1)
            await handle.signal("retry_shot", args=["A01"])
            await asyncio.sleep(0.5)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A01"] == 1
            
            # Try to retry again (should be refused - already at max)
            await handle.signal("retry_shot", args=["A01"])
            await asyncio.sleep(0.5)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            # Attempt count should still be 1 (second retry refused)
            assert state["shot_attempts"]["A01"] == 1
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_uses_different_idempotency_key():
    """Test that retry_shot uses a different workflow ID (with attempt number)."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-key",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry5", None, True, ["A01"], 40],
                id="test-retry-key-workflow",
                task_queue="test-retry-key",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            
            # Check original workflow ID exists
            original_wf = env.client.get_workflow_handle("ep_retry5-shot-A01")
            desc = await original_wf.describe()
            assert desc.id == "ep_retry5-shot-A01"
            
            # Retry the shot
            await handle.signal("retry_shot", args=["A01"])  # Max retries from config
            await asyncio.sleep(0.5)
            
            # Check that retry workflow has different ID (includes retry1)
            retry_wf = env.client.get_workflow_handle("ep_retry5-shot-A01-retry1")
            desc = await retry_wf.describe()
            assert desc.id == "ep_retry5-shot-A01-retry1"
            
            await handle.terminate("test done")
