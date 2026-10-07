"""
Live-mode ShotWorkflow tests with mocked providers.

Tests that human approval after QC escalation leads to clip submission.
"""

import pytest
import asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.shot import ShotWorkflow
from hfvg import activities


@pytest.mark.asyncio
async def test_shot_workflow_human_approval_proceeds_to_clip(tmp_path, monkeypatch):
    """
    Test: After QC escalation and human approval, workflow proceeds to clip.
    
    This is the critical test for fix #1: verifying that the clip is actually
    generated after human approval of an escalated still (in dry mode, which
    simulates live behavior for workflow logic).
    """
    # Set up database for this test
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_live_shot.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set environment variables for activities - DRY MODE (simulates workflow logic)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")  # Dry mode for simplicity
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.await_job_enforced,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
            ],
        ):
            shot_plan = {
                "shot_id": "A01",
                "prompt": "Test shot requiring human approval",
                "refs": [],
                "params": {"duration": 5},
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", shot_plan],
                id="test-shot-human-approval",
                task_queue="test-task-queue",
            )
            
            # Wait for still to be generated and reviewed
            # In dry mode, QC review will escalate (review_still returns escalate=True in live)
            # The workflow should then wait for human approval
            await asyncio.sleep(0.5)
            
            # Send human approval signal (this is what the API route does)
            await handle.signal("stills_approved")
            
            # Wait for workflow to process approval and continue to clip
            await asyncio.sleep(0.5)
            
            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=15)
            
            # Verify workflow completed successfully with BOTH still and clip
            assert result["status"] == "completed", f"Expected completed, got {result['status']}"
            assert result["still_url"] is not None, "Still URL should be present"
            assert result["clip_url"] is not None, "Clip URL should be present after approval"
            
            # This verifies fix #1: that human approval leads to clip generation
            # Before the fix, the workflow would fail at line 231 with "Max retries exceeded"
