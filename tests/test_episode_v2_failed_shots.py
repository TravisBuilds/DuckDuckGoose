"""Test episode_v2 handles failed shots that return error dicts."""

import asyncio
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity, workflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


# Mock ShotWorkflow that returns failed dict
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class FailedShotWorkflow:
    def __init__(self):
        self.approved = False

    @workflow.signal
    def stills_approved(self):
        self.approved = True

    @workflow.signal
    def resume_after_cap_raise(self):
        pass

    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        # Return failed dict with provider detail
        return {
            "status": "failed",
            "error": "422 PROVIDER_DETAIL_XYZ"
        }


# Mock ShotWorkflow that returns success dict
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class SuccessShotWorkflow:
    def __init__(self):
        self.approved = False

    @workflow.signal
    def stills_approved(self):
        self.approved = True

    @workflow.signal
    def resume_after_cap_raise(self):
        pass

    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
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


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_episode_v2_shot_returns_failed_dict():
    """Test that a shot returning {'status':'failed','error':'...'} ends up in failed_shots with exact reason."""
    script_data = {
        "shots": [
            {
                "shot_id": "shot_001",
                "prompt": "A test shot that will fail",
                "duration": 3.0
            }
        ]
    }
    
    def _make_activities_with_script():
        @activity.defn(name="acquire_run_lock")
        async def lock(*a): return True

        @activity.defn(name="release_run_lock")
        async def unlock(*a): return True

        @activity.defn(name="load_gate_policy_activity")
        async def policy(*a): return {}

        @activity.defn(name="parse_beatmap_activity")
        async def parse(*args):
            return script_data

        @activity.defn(name="load_refs_to_generate_activity")
        async def refs(*a): return []

        @activity.defn(name="generate_run_report")
        async def report(*a): return {}

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
        return [lock, unlock, policy, parse, refs, report, budget_init, *extra]
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-failed-shot",
            workflows=[EpisodeWorkflowV2, FailedShotWorkflow],
            activities=_make_activities_with_script(),
        ):
            
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_test_fail", None, True, ["shot_001"], 40],
                id="test-failed-shot-workflow",
                task_queue="test-failed-shot",
            )
            
            # Wait for G1 gate and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for failed_shots to populate
            state = await _wait_for_state(
                handle,
                lambda s: len(s["failed_shots"]) > 0
            )
            
            # Verify the failed shot is recorded with exact provider detail
            assert len(state["failed_shots"]) == 1
            failed = state["failed_shots"][0]
            assert failed["shot_id"] == "shot_001"
            assert failed["status"] == "failed"
            assert failed["reason"] == "422 PROVIDER_DETAIL_XYZ", f"Expected exact provider detail, got: {failed['reason']}"
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_episode_v2_shot_success_path():
    """Test that a successful shot is recorded properly."""
    script_data = {
        "shots": [
            {
                "shot_id": "shot_002",
                "prompt": "A test shot that succeeds",
                "duration": 3.0
            }
        ]
    }
    
    def _make_activities_with_script():
        @activity.defn(name="acquire_run_lock")
        async def lock(*a): return True

        @activity.defn(name="release_run_lock")
        async def unlock(*a): return True

        @activity.defn(name="load_gate_policy_activity")
        async def policy(*a): return {}

        @activity.defn(name="parse_beatmap_activity")
        async def parse(*args):
            return script_data

        @activity.defn(name="load_refs_to_generate_activity")
        async def refs(*a): return []

        @activity.defn(name="generate_run_report")
        async def report(*a): return {}

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
        return [lock, unlock, policy, parse, refs, report, budget_init, *extra]
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-success-shot",
            workflows=[EpisodeWorkflowV2, SuccessShotWorkflow],
            activities=_make_activities_with_script(),
        ):
            
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_test_success", None, True, ["shot_002"], 40],
                id="test-success-shot-workflow",
                task_queue="test-success-shot",
            )
            
            # Wait for G1 gate and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for G2 gate
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS")
            
            # Verify no failed shots at this point
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert len(state["failed_shots"]) == 0, f"Expected no failed shots, got: {state['failed_shots']}"
            
            await handle.terminate("test done")
