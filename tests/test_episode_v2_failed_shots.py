"""Test episode_v2 handles failed shots that return error dicts."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import workflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


# Mock ShotWorkflow that returns failed dict
@workflow.defn
class FailedShotWorkflow:
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        # Return failed dict with provider detail
        return {
            "status": "failed",
            "error": "422 PROVIDER_DETAIL_XYZ"
        }


# Mock ShotWorkflow that returns success dict
@workflow.defn
class SuccessShotWorkflow:
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        # Return success
        return {
            "status": "completed",
            "shot_id": shot_dict["shot_id"],
            "still_url": "dry-run://still.png",
            "clip_url": "dry-run://clip.mp4",
        }


@pytest.mark.asyncio
async def test_episode_v2_shot_returns_failed_dict():
    """Test that a shot returning {'status':'failed','error':'...'} ends up in failed_shots with exact reason."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-failed-shot",
            workflows=[EpisodeWorkflowV2, FailedShotWorkflow],
            activities=[],
        ):
            script_data = {
                "shots": [
                    {
                        "shot_id": "shot_001",
                        "prompt": "A test shot that will fail",
                        "duration": 3.0
                    }
                ]
            }
            
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_test_fail", script_data],
                id="test-failed-shot-workflow",
                task_queue="test-failed-shot",
            )
            
            result = await handle.result()
            
            # Verify the failed shot is recorded with exact provider detail
            assert "failed_shots" in result
            assert len(result["failed_shots"]) == 1
            failed = result["failed_shots"][0]
            assert failed["shot_id"] == "shot_001"
            assert failed["status"] == "failed"
            assert failed["reason"] == "422 PROVIDER_DETAIL_XYZ", f"Expected exact provider detail, got: {failed['reason']}"


@pytest.mark.asyncio
async def test_episode_v2_shot_success_path():
    """Test that a successful shot is recorded properly."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-success-shot",
            workflows=[EpisodeWorkflowV2, SuccessShotWorkflow],
            activities=[],
        ):
            script_data = {
                "shots": [
                    {
                        "shot_id": "shot_002",
                        "prompt": "A test shot that succeeds",
                        "duration": 3.0
                    }
                ]
            }
            
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_test_success", script_data],
                id="test-success-shot-workflow",
                task_queue="test-success-shot",
            )
            
            result = await handle.result()
            
            # Verify no failed shots
            assert "failed_shots" in result
            assert len(result["failed_shots"]) == 0, f"Expected no failed shots, got: {result['failed_shots']}"
            
            # Verify the shot completed (not just assert True)
            assert "status" in result
            # Either completed or has completed_shots list
            if "completed_shots" in result:
                assert len(result["completed_shots"]) > 0, "Expected at least one completed shot"
            elif result.get("status") == "failed":
                pytest.fail(f"Workflow failed when it should have succeeded: {result.get('error', 'no error')}")
