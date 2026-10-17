"""Test episode_v2 handles failed shots that return error dicts."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import generate_still, generate_clip
from temporalio import activity


# Fake shot activity that RETURNS a failed dict (doesn't raise)
@activity.defn(name="generate_still")
async def fake_still_failed(shot_id: str, prompt: str, **kwargs):
    """Fake still activity that returns failed status with provider detail."""
    return {
        "status": "failed",
        "error": "422 PROVIDER_DETAIL_XYZ"
    }


@activity.defn(name="generate_still")
async def fake_still_success(shot_id: str, prompt: str, **kwargs):
    """Fake still activity that returns success."""
    return {
        "status": "completed",
        "url": "http://fake.cdn/shot.png",
        "cost": 0.5
    }


@pytest.mark.asyncio
async def test_episode_v2_shot_returns_failed_dict():
    """Test that a shot returning {'status':'failed','error':'...'} ends up in failed_shots with exact reason."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Use the fake failed activity
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[fake_still_failed],
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
                id="test-episode-failed-shot",
                task_queue="test-task-queue",
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
        # Use the fake success activity
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[fake_still_success],
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
                id="test-episode-success-shot",
                task_queue="test-task-queue",
            )
            
            result = await handle.result()
            
            # Verify no failed shots
            assert "failed_shots" in result
            assert len(result["failed_shots"]) == 0, f"Expected no failed shots, got: {result['failed_shots']}"
            
            # Verify completed_shots includes the successful shot
            assert "completed_shots" in result
            assert len(result["completed_shots"]) == 1
            assert result["completed_shots"][0]["shot_id"] == "shot_002"
            assert result["completed_shots"][0]["status"] == "completed"
