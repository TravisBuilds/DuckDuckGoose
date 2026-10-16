"""Tests for credit hard cap pause/resume mechanism."""

import pytest
import respx

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
@respx.mock
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
    
    # Create shots.json (required in live mode)
    from pathlib import Path
    shots_dir = Path(tmp_path) / "data" / "episodes" / "ep99"
    shots_dir.mkdir(parents=True, exist_ok=True)
    (shots_dir / "shots.json").write_text('[{"shot_id": "A01"}]')
    
    # Reserve 4 credits (within cap)
    await credit_ledger.reserve_credits("ep99", "job-1", 4)
    
    # Mock provider endpoints (should NOT be called if cap blocks)
    estimate_mock = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.12", "usd": "0.07"})
    )
    submit_mock = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "job-123"})
    )
    
    # Try to submit still (needs ~1.12 credits, but only 1 available after 4 reserved from 5 cap)
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


@pytest.mark.asyncio
async def test_workflow_pause_resume_with_time_skipping(tmp_path, monkeypatch):
    """
    Test: Shot workflow pauses when cap is hit, resumes after cap raised via signal.
    
    Uses Temporal time-skipping with fake activities (no HTTP mocking):
    1. Shot pauses with status "paused_cap" and submit count 0
    2. After raise_credit_cap signal, shot completes with submit count exactly 1
    """
    import asyncio
    from datetime import timedelta
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.client import Client
    from temporalio.worker import Worker
    
    # Setup database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    await init_studio_db(db_path)
    
    from hfvg.studio_db import create_episode
    await create_episode(db_path, "ep99")
    
    # Initialize credit ledger with low cap
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=5)
    
    # Shared state for fake activities
    test_state = {
        "cap_raised": False,
        "submit_count": 0,
    }
    
    # Fake activities - no HTTP calls, use **kwargs to accept any args
    @activity.defn(name="submit_still_job_enforced")
    async def fake_submit_still(episode_id, shot_id, *args, **kwargs):
        """Fake submit that counts calls."""
        # Only succeed if cap was raised
        if test_state["cap_raised"]:
            test_state["submit_count"] += 1
            return {
                "job_id": f"fake-job-{test_state['submit_count']}",
                "line_name": "L2_drafts",
                "reserved_amount": 1000,
            }
        else:
            # Simulate hitting cap by raising error
            from hfvg.errors import InsufficientCreditsError
            raise InsufficientCreditsError(
                "Credit hard cap reached",
                required=10.0,
                available=0.0
            )
    
    @activity.defn(name="poll_job_status")
    async def fake_poll(*args, **kwargs):
        """Fake poll - immediately completed."""
        return {
            "status": "completed",
            "output_url": "http://fake.url/output.png",
        }
    
    @activity.defn(name="commit_job_budget")
    async def fake_commit_budget(*args, **kwargs):
        """Fake commit budget."""
        pass
    
    @activity.defn(name="release_job_budget")
    async def fake_release_budget(*args, **kwargs):
        """Fake release budget."""
        pass
    
    @activity.defn(name="mark_job_pending_reconcile")
    async def fake_mark_pending(*args, **kwargs):
        """Fake mark pending."""
        pass
    
    @activity.defn(name="precheck_still_qc")
    async def fake_precheck_still(*args, **kwargs):
        """Fake precheck."""
        return {"passed": True, "issues": []}
    
    @activity.defn(name="review_still")
    async def fake_review_still(*args, **kwargs):
        """Fake review."""
        return {"escalate": False, "approved": True}
    
    @activity.defn(name="record_shot_result")
    async def fake_record(*args, **kwargs):
        """Fake record."""
        pass
    
    @activity.defn(name="reserve_credits_activity")
    async def fake_reserve(episode_id, job_id, credits, notes=""):
        """Fake reserve - returns over_cap until cap is raised."""
        if not test_state["cap_raised"]:
            # Simulate hitting cap
            return {"reserved": False, "over_cap": True, "available": 0}
        else:
            return {"reserved": True, "over_cap": False, "available": 100}
    
    @activity.defn(name="commit_credits_activity")
    async def fake_commit_credits(*args, **kwargs):
        """Fake commit credits."""
        pass
    
    @activity.defn(name="release_credits_activity")
    async def fake_release_credits(*args, **kwargs):
        """Fake release credits."""
        pass
    
    @activity.defn(name="update_credit_cap_activity")
    async def fake_update_cap(episode_id, new_cap):
        """Fake update cap - sets flag to allow reserves."""
        test_state["cap_raised"] = True
        await credit_ledger.update_credit_cap(episode_id, new_cap)
    
    # Start Temporal test environment with time-skipping
    async with await WorkflowEnvironment.start_time_skipping() as env:
        from hfvg.workflows.shot import ShotWorkflow
        from tests.lint_helpers import lint_ok, lint_meta
        
        # Register fake activities
        fake_activities = [
            fake_submit_still,
            fake_poll,
            fake_commit_budget,
            fake_release_budget,
            fake_mark_pending,
            fake_precheck_still,
            fake_review_still,
            fake_record,
            fake_reserve,
            fake_commit_credits,
            fake_release_credits,
            fake_update_cap,
        ]
        
        # Start worker with fake activities
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=fake_activities,
        ):
            # Prepare shot plan
            shot_plan = {
                "shot_id": "A01",
                "prompt": lint_ok("A dog in a park"),
                "shot_meta": lint_meta(),
                "params": {"asset_type": "still"},
            }
            
            # Start shot workflow
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                id="test-shot-A01",
                task_queue="test-queue",
                args=["ep99", shot_plan],
            )
            
            # Poll for paused status
            shot_status = "unknown"
            for _ in range(100):  # Up to 10 seconds
                await asyncio.sleep(0.1)
                try:
                    shot_status = await handle.query("get_status")
                    if shot_status == "paused_cap":
                        break
                except Exception as e:
                    # Workflow might not have started yet or query failed
                    pass
            
            # Assert shot is paused with no submits
            assert shot_status == "paused_cap", f"Expected paused_cap, got {shot_status}"
            assert test_state["submit_count"] == 0, "No submits should happen while paused"
            
            # Now manually flip the cap_raised flag and send signal
            # (In real scenario, episode workflow would call update_credit_cap_activity)
            test_state["cap_raised"] = True
            await credit_ledger.update_credit_cap("ep99", 100)
            
            # Send signal to resume
            await handle.signal("resume_after_cap_raise")
            
            # Wait for still to complete
            for _ in range(50):
                await asyncio.sleep(0.1)
                if test_state["submit_count"] == 1:
                    break
            
            # Assert exactly one submit happened for the still
            assert test_state["submit_count"] == 1, f"Expected 1 submit after unpause, got {test_state['submit_count']}"
            
            # Test passed - workflow paused at cap, then resumed and submitted exactly once
            # Cancel workflow to clean up (it would wait for stills_approved otherwise)
            await handle.cancel()
