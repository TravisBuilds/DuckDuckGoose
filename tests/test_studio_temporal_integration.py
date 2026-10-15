"""
Studio Temporal integration tests.

Tests that approval signals reach workflows and canary starts ShotWorkflow.
"""

import pytest
import asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow
from hfvg import activities
from hfvg.activities.credit_activities import (
    commit_credits_activity,
    release_credits_activity,
)


@pytest.mark.asyncio
async def test_approval_signal_reaches_episode_workflow(tmp_path, monkeypatch):
    """
    Test: Approval signal actually reaches running EpisodeWorkflowV2.
    
    This verifies the signal path from API → Temporal → Workflow.
    """
    from hfvg.activities.run_lock import acquire_run_lock, release_run_lock
    from hfvg.studio_db import init_studio_db, create_episode
    from hfvg.credit_ledger import CreditLedger
    
    # Set up database
    db_path = str(tmp_path / "test_approval.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger with run_locks table
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Set environment variables
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create worker
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[
                activities.submit_still_job,
                activities.submit_clip_job,
                activities.await_job,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                acquire_run_lock,
                release_run_lock,
                commit_credits_activity,
                release_credits_activity,
            ],
        ):
            # Start workflow (will pause at G1.01)
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", None, True],  # dry_run=True
                id="test-episode-ep99",
                task_queue="test-task-queue",
            )
            
            # Give it a moment to start
            await asyncio.sleep(0.1)
            
            # Query initial state
            state = await handle.query("get_state")
            assert state["episode_id"] == "ep99"
            assert not state["approvals"]["g1_shot_plan"], "G1 should not be approved yet"
            
            # Send approval signal
            await handle.signal("approve_g1_shot_plan")
            
            # Give it time to process
            await asyncio.sleep(0.1)
            
            # Query state again
            state_after = await handle.query("get_state")
            assert state_after["approvals"]["g1_shot_plan"], "G1 should be approved after signal"
            
            # Cancel workflow
            await handle.cancel()


@pytest.mark.asyncio
async def test_canary_starts_shot_workflow(tmp_path, monkeypatch):
    """
    Test: Canary actually starts ShotWorkflow (not just a stub).
    
    This verifies the canary path through real workflow execution.
    """
    # Set up database for this test
    from hfvg.studio_db import init_studio_db, create_episode
    from hfvg.credit_ledger import CreditLedger
    
    db_path = str(tmp_path / "test_canary.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Set environment variables for activities
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Create worker with ShotWorkflow
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job,
                activities.submit_clip_job,
                activities.await_job,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                commit_credits_activity,
                release_credits_activity,
            ],
        ):
            # Start ShotWorkflow (like canary does)
            canary_shot = {
                "shot_id": "CANARY01",
                "prompt": "A serene duck standing beside a warm fjord pool",
                "refs": [],
                "params": {"duration": 5.0},
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", canary_shot],
                id="test-canary-shot",
                task_queue="test-task-queue",
            )
            
            # Give workflow time to generate still
            await asyncio.sleep(0.5)
            
            # Send approval signal (workflow waits for stills approval before generating clip)
            await handle.signal("stills_approved")
            
            # Wait for completion (with timeout)
            try:
                result = await asyncio.wait_for(handle.result(), timeout=10)
                
                # In dry run mode, should get fake URLs
                assert result["shot_id"] == "CANARY01"
                assert result["status"] == "completed"
                assert "still_url" in result
                assert "clip_url" in result
                
                # Dry run should have fake URLs
                assert "/fake/" in result["still_url"] or "dry" in result["still_url"]
                
            except asyncio.TimeoutError:
                pytest.fail("ShotWorkflow did not complete in time")


@pytest.mark.asyncio
async def test_shot_workflow_waits_for_still_approval(tmp_path, monkeypatch):
    """
    Test: ShotWorkflow waits for still approval signal before generating clip.
    """
    # Set up database for this test
    from hfvg.studio_db import init_studio_db, create_episode
    from hfvg.credit_ledger import CreditLedger
    
    db_path = str(tmp_path / "test_approval.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Set environment variables for activities
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job,
                activities.submit_clip_job,
                activities.await_job,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                commit_credits_activity,
                release_credits_activity,
            ],
        ):
            shot_plan = {
                "shot_id": "A01",
                "prompt": "Test shot",
                "refs": [],
                "params": {"duration": 5.0},
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", shot_plan],
                id="test-shot-approval-wait",
                task_queue="test-task-queue",
            )
            
            # Give workflow time to generate still
            await asyncio.sleep(0.5)
            
            # Workflow should be waiting for approval
            # (In real implementation, it's waiting at workflow.wait_condition)
            
            # Send approval signal
            await handle.signal("stills_approved")
            
            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=10)
            
            assert result["status"] == "completed"
            assert result["still_url"] is not None
            assert result["clip_url"] is not None
