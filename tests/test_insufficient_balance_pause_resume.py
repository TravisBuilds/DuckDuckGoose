"""Test workflow-level pause and resume on InsufficientBalance.

When a paid activity hits 403 InsufficientBalance, the shot workflow must:
- Set visible status to 'paused_insufficient_balance'
- Wait on a resume_balance signal
- Retry the activity with the same idempotency key
- Not double-charge (single reservation/commit)
"""
import pytest
from datetime import timedelta
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.exceptions import ApplicationError, ActivityError
from temporalio import activity
from unittest.mock import patch, AsyncMock
from hfvg.workflows.shot import ShotWorkflow


# Track activity calls globally to verify single charge
activity_call_count = {"submit_still": 0, "submit_clip": 0, "poll": 0, "commit": 0}


@activity.defn(name="submit_still_job_enforced")
async def mock_submit_still(*args, **kwargs):
    """Mock submit that fails first time with InsufficientBalance, succeeds second time."""
    activity_call_count["submit_still"] += 1
    if activity_call_count["submit_still"] == 1:
        # First call: insufficient balance
        raise ApplicationError("Insufficient balance", type="InsufficientBalance", non_retryable=True)
    # Second call: success (after resume signal)
    return {
        "job_id": f"test-still-{activity_call_count['submit_still']}",
        "line_name": "hf_still",
        "reserved_amount": 10.0,
    }


@activity.defn(name="submit_clip_job_enforced")
async def mock_submit_clip(*args, **kwargs):
    """Mock clip submit that fails first time with InsufficientBalance."""
    activity_call_count["submit_clip"] += 1
    if activity_call_count["submit_clip"] == 1:
        raise ApplicationError("Insufficient balance", type="InsufficientBalance", non_retryable=True)
    return {
        "job_id": f"test-clip-{activity_call_count['submit_clip']}",
        "line_name": "hf_clip",
        "reserved_amount": 50.0,
    }


@activity.defn(name="poll_job_status")
async def mock_poll_job(*args, **kwargs):
    """Mock poll that returns completed."""
    activity_call_count["poll"] += 1
    job_id = args[0] if args else "test-job"
    if "still" in job_id:
        return {
            "status": "completed",
            "url": "https://example.com/still.jpg",
            "cost": 10.0,
        }
    else:
        return {
            "status": "completed",
            "url": "https://example.com/clip.mp4",
            "cost": 50.0,
        }


@activity.defn(name="commit_job_budget")
async def mock_commit_budget(*args, **kwargs):
    """Mock commit budget activity."""
    activity_call_count["commit"] += 1
    return {"success": True}


@activity.defn(name="commit_credits_activity")
async def mock_commit_credits(*args, **kwargs):
    """Mock commit credits activity."""
    return {"success": True}


@activity.defn(name="save_asset_locally")
async def mock_save_asset(*args, **kwargs):
    """Mock save asset locally."""
    return "/tmp/test-asset.jpg"


@activity.defn(name="review_still")
async def mock_review_still(*args, **kwargs):
    """Mock still review."""
    return {"approved": True}


@activity.defn(name="review_clip")
async def mock_review_clip(*args, **kwargs):
    """Mock clip review."""
    return {"approved": True}


@activity.defn(name="record_shot_result")
async def mock_record_shot(*args, **kwargs):
    """Mock record shot result."""
    return {"success": True}


@activity.defn(name="release_job_budget")
async def mock_release_budget(*args, **kwargs):
    """Mock release budget."""
    return {"success": True}


@activity.defn(name="release_credits_activity")
async def mock_release_credits(*args, **kwargs):
    """Mock release credits."""
    return {"success": True}


@activity.defn(name="precheck_still_qc")
async def mock_precheck_still(*args, **kwargs):
    """Mock still precheck."""
    return {"passed": True}


@activity.defn(name="precheck_clip_qc")
async def mock_precheck_clip(*args, **kwargs):
    """Mock clip precheck."""
    return {"passed": True}


@pytest.mark.asyncio
async def test_insufficient_balance_pause_resume():
    """Test that InsufficientBalance pauses workflow, waits for signal, then resumes without double charge."""
    
    # Reset counters
    activity_call_count["submit_still"] = 0
    activity_call_count["poll"] = 0
    activity_call_count["commit"] = 0
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create worker with mock activities
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=[
                mock_submit_still,
                mock_submit_clip,
                mock_poll_job,
                mock_commit_budget,
                mock_commit_credits,
                mock_save_asset,
                mock_review_still,
                mock_review_clip,
                mock_record_shot,
                mock_release_budget,
                mock_release_credits,
                mock_precheck_still,
                mock_precheck_clip,
            ],
        ):
            # Start workflow
            shot_plan = {
                "shot_id": "A01",
                "prompt": "test prompt",
                "clip_prompt": "test clip",
                "duration": 5.0,
                "shot_meta": {},
                "missing_refs": [],
                "qc_fail_if": [],
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["test-episode", shot_plan],
                id="test-workflow-pause-resume",
                task_queue="test-queue",
            )
            
            # Wait a bit for workflow to hit the pause
            await env.sleep(2)
            
            # Query status - should be paused_insufficient_balance
            status = await handle.query("get_status")
            assert status == "paused_insufficient_balance", f"Expected paused_insufficient_balance, got {status}"
            
            # Verify submit was called once (and failed)
            assert activity_call_count["submit_still"] == 1, "submit_still should be called once before pause"
            assert activity_call_count["commit"] == 0, "commit should not be called yet"
            
            # Send resume signal
            await handle.signal("resume_balance")
            
            # Wait for workflow to process resume and retry
            await env.sleep(2)
            
            # Signal the workflow to approve stills (so it can complete)
            await handle.signal("stills_approved")
            
            # Wait for workflow to complete
            result = await handle.result()
            
            # Verify: submit was called exactly twice (once failed, once succeeded)
            assert activity_call_count["submit_still"] == 2, f"Expected 2 submit calls, got {activity_call_count['submit_still']}"
            
            # Verify: commit was called exactly once (no double charge)
            assert activity_call_count["commit"] == 1, f"Expected 1 commit call, got {activity_call_count['commit']}"
            
            # Verify workflow completed successfully
            assert result["status"] in ["completed", "done"], f"Workflow should complete, got {result}"
            assert result.get("still_url") is not None, "Still URL should be present"


@pytest.mark.asyncio
async def test_insufficient_balance_clip_pause_resume():
    """Test InsufficientBalance pause/resume for clip generation."""
    
    # Reset counters
    activity_call_count["submit_still"] = 100  # Start at 100 so it succeeds
    activity_call_count["submit_clip"] = 0
    activity_call_count["poll"] = 0
    activity_call_count["commit"] = 0
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=[
                mock_submit_still,
                mock_submit_clip,
                mock_poll_job,
                mock_commit_budget,
                mock_commit_credits,
                mock_save_asset,
                mock_review_still,
                mock_review_clip,
                mock_record_shot,
                mock_release_budget,
                mock_release_credits,
                mock_precheck_still,
                mock_precheck_clip,
            ],
        ):
            shot_plan = {
                "shot_id": "A02",
                "prompt": "test still",
                "clip_prompt": "test clip",
                "duration": 5.0,
                "shot_meta": {},
                "missing_refs": [],
                "qc_fail_if": [],
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["test-episode", shot_plan],
                id="test-workflow-clip-pause",
                task_queue="test-queue",
            )
            
            # Approve still immediately
            await handle.signal("stills_approved")
            
            # Wait for clip submit to hit pause
            await env.sleep(3)
            
            # Query status
            status = await handle.query("get_status")
            assert status == "paused_insufficient_balance", f"Expected paused_insufficient_balance, got {status}"
            
            # Verify clip submit called once
            assert activity_call_count["submit_clip"] == 1
            
            # Resume
            await handle.signal("resume_balance")
            
            # Wait for completion
            result = await handle.result()
            
            # Verify clip submit called twice, but only 2 commits total (still + clip, no double charge)
            assert activity_call_count["submit_clip"] == 2
            assert activity_call_count["commit"] == 2  # One for still, one for clip
            assert result["status"] in ["completed", "done"]
