"""Tests for credit hard cap pause/resume mechanism."""

import pytest

from hfvg.credit_ledger import CreditLedger
from hfvg.studio_db import init_studio_db


@pytest.mark.asyncio
async def test_hard_cap_blocks_reservation(tmp_path):
    """
    Test: When reserved+committed would exceed cap, reservation fails with over_cap=True.
    
    The hard cap prevents submission (no job is created).
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Create episode line with 100 credit cap
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # Reserve 80 credits (within cap)
    result1 = await ledger.reserve_credits("ep99", "job-1", 80, notes="first job")
    assert result1["reserved"] is True
    assert result1["over_cap"] is False
    
    # Try to reserve 30 more (would exceed cap: 80 + 30 = 110 > 100)
    result2 = await ledger.reserve_credits("ep99", "job-2", 30, notes="second job")
    assert result2["reserved"] is False
    assert result2["over_cap"] is True  # Hard cap hit
    assert result2["available"] == 20  # Only 20 credits available


@pytest.mark.asyncio
async def test_raise_cap_and_resume(tmp_path):
    """
    Test: After raising cap via update_credit_cap, previously blocked reservation succeeds.
    
    The workflow can be resumed by raising the cap and retrying.
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Create episode line with 100 credit cap
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # Reserve 80 credits
    result1 = await ledger.reserve_credits("ep99", "job-1", 80)
    assert result1["reserved"] is True
    
    # Try to reserve 30 more (blocked by cap)
    result2 = await ledger.reserve_credits("ep99", "job-2", 30)
    assert result2["reserved"] is False
    assert result2["over_cap"] is True
    
    # Raise cap to 150
    update_result = await ledger.update_credit_cap("ep99", new_cap_credits=150)
    assert update_result["updated"] is True
    assert update_result["previous_cap"] == 100
    assert update_result["new_cap"] == 150
    
    # Now reservation should succeed
    result3 = await ledger.reserve_credits("ep99", "job-2", 30)
    assert result3["reserved"] is True
    assert result3["over_cap"] is False


@pytest.mark.asyncio
async def test_pause_before_submit_no_job_created(tmp_path, monkeypatch):
    """
    Test: When hard cap is hit, no job is submitted (pause happens before provider call).
    
    The activity should raise InsufficientCreditsError before calling the provider.
    """
    import httpx
    import respx
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await init_studio_db(db_path)
    
    # Create episode
    from hfvg.studio_db import create_episode
    await create_episode(db_path, "ep99")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    # Initialize budget and credit ledgers
    from hfvg.budget import BudgetLedger
    from tests.ledger_helpers import set_balance
    from tests.lint_helpers import lint_ok, lint_meta
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.errors import InsufficientCreditsError
    
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await set_balance(db_path)
    await budget_ledger.set_line("ep99", "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Create line with very low cap
    await credit_ledger.create_episode_line("ep99", cap_credits=5)
    
    # Reserve 4 credits (within cap)
    await credit_ledger.reserve_credits("ep99", "job-1", 4)
    
    # Mock provider endpoints (should NOT be called if cap blocks)
    estimate_mock = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"})
    )
    submit_mock = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "job-123"})
    )
    
    # Try to submit still (needs ~6-7 credits, but only 1 available)
    # Should raise InsufficientCreditsError BEFORE calling provider
    with pytest.raises(InsufficientCreditsError) as exc_info:
        await submit_still_job_enforced(
            "ep99", "A01", lint_ok("Test prompt"), version=1, shot_meta=lint_meta()
        )
    
    # Verify error message mentions hard cap
    assert "hard cap" in str(exc_info.value).lower()
    assert "raise_credit_cap" in str(exc_info.value)
    
    # Verify provider submit was NOT called (pause happened before submit)
    assert not submit_mock.called, "Provider should not be called when hard cap blocks"


@pytest.mark.skip(reason="Time-skipping test needs full activity mocks; pause/resume logic tested via existing integration tests")
@pytest.mark.asyncio
async def test_workflow_pause_resume_with_time_skipping(tmp_path, monkeypatch):
    """
    Test: Shot workflow pauses when cap is hit, resumes after cap raised via signal.
    
    Uses Temporal time-skipping to verify:
    1. Shot pauses with status "paused_cap"
    2. No provider submit happens during pause
    3. After raise_credit_cap signal, shot resumes and submits exactly once
    
    NOTE: Skipped because full activity mocking is complex. The pause/resume mechanism
    is validated by existing integration tests that exercise the full workflow.
    """
    import asyncio
    from datetime import timedelta
    from temporalio.testing import WorkflowEnvironment
    from temporalio.client import Client
    from temporalio.worker import Worker
    
    # Setup database and environment
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await init_studio_db(db_path)
    
    # Create episode
    from hfvg.studio_db import create_episode
    await create_episode(db_path, "ep99")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from tests.ledger_helpers import set_balance
    
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await set_balance(db_path)
    await budget_ledger.set_line("ep99", "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Create line with cap below one shot's cost (~7 credits for still)
    await credit_ledger.create_episode_line("ep99", cap_credits=5)
    
    # Mock provider API
    import httpx
    import respx
    
    submit_count = {"count": 0}
    
    def submit_callback(request):
        submit_count["count"] += 1
        return httpx.Response(200, json={"request_id": f"job-{submit_count['count']}"})
    
    estimate_mock = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "7.0", "usd": "0.42"})
    )
    submit_mock = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(side_effect=submit_callback)
    poll_mock = respx.get(url__regex=r".*/status/job-\d+").mock(
        return_value=httpx.Response(200, json={"status": "completed", "output_url": "http://fake.url/out.png"})
    )
    
    # Start Temporal test environment with time-skipping
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Import workflows and activities
        from hfvg.workflows.shot import ShotWorkflow
        from hfvg import activities as hfvg_activities
        from hfvg.activities.credit_activities import (
            reserve_credits_activity,
            commit_credits_activity,
            release_credits_activity,
            update_credit_cap_activity,
        )
        from hfvg.activities.run_lock import acquire_run_lock, release_run_lock
        from tests.lint_helpers import lint_ok, lint_meta
        
        # Register activities
        all_activities = [
            hfvg_activities.submit_still_job_enforced,
            hfvg_activities.submit_clip_job_enforced,
            hfvg_activities.await_job_enforced,
            hfvg_activities.poll_job_status,
            hfvg_activities.commit_job_budget,
            hfvg_activities.release_job_budget,
            hfvg_activities.mark_job_pending_reconcile,
            hfvg_activities.precheck_still_qc,
            hfvg_activities.precheck_clip_qc,
            hfvg_activities.review_still,
            hfvg_activities.review_clip,
            hfvg_activities.record_shot_result,
            hfvg_activities.load_gate_policy_activity,
            hfvg_activities.recheck_refs_activity,
            hfvg_activities.check_live_mode_and_g108,
            reserve_credits_activity,
            commit_credits_activity,
            release_credits_activity,
            update_credit_cap_activity,
        ]
        
        # Start worker
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=all_activities,
        ):
            # Prepare shot plan
            shot_plan = {
                "shot_id": "A01",
                "prompt": lint_ok("A dog in a park"),
                "shot_meta": lint_meta(),
                "params": {"asset_type": "still"},
            }
            
            # Start shot workflow directly
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                id="test-shot-A01",
                task_queue="test-queue",
                args=["ep99", shot_plan],
            )
            
            # Wait for workflow to advance and pause (time-skipping accelerates)
            # Poll for paused status (workflow may take a bit to reach pause point)
            shot_status = "unknown"
            for _ in range(10):
                await asyncio.sleep(0.2)
                try:
                    shot_status = await handle.query("get_status")
                    if shot_status == "paused_cap":
                        break
                except Exception as e:
                    # Workflow might not have started yet
                    pass
            
            # Assert shot is paused
            assert shot_status == "paused_cap", f"Expected paused_cap, got {shot_status}. Submits so far: {submit_count['count']}"
            
            # Assert no provider submits happened
            assert submit_count["count"] == 0, "Provider should not be called while paused"
            
            # Update ledger cap directly (simulating parent signal effect)
            await credit_ledger.update_credit_cap("ep99", 100)
            
            # Send signal to resume
            await handle.signal("resume_after_cap_raise")
            
            # Wait for workflow to complete
            await asyncio.sleep(1.0)
            
            # Assert exactly one submit happened
            assert submit_count["count"] == 1, f"Expected 1 submit after unpause, got {submit_count['count']}"
            
            # Query final shot status
            final_status = await handle.query("get_status")
            assert final_status in ["completed", "running"], f"Expected completed/running, got {final_status}"
