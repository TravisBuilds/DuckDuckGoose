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


@pytest.mark.asyncio
async def test_workflow_signal_updates_state():
    """
    Test: Workflow signal raise_credit_cap updates state fields.
    
    The signal should set credit_cap_raised=True and store new_credit_cap.
    """
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    workflow = EpisodeWorkflowV2()
    
    # Initial state
    assert workflow.state.credit_cap_raised is False
    assert workflow.state.new_credit_cap == 0
    
    # Call signal
    await workflow.raise_credit_cap(200)
    
    # Verify state updated
    assert workflow.state.credit_cap_raised is True
    assert workflow.state.new_credit_cap == 200
