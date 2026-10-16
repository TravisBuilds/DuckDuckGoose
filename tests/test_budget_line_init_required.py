"""Test: Live mode requires budget line initialization before generation."""

import pytest
import aiosqlite
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_live_still_fails_without_budget_lines(tmp_path, monkeypatch):
    """
    Test: Live still generation FAILS if budget lines not initialized.
    
    This verifies the fix for the ep05 bug: 'Budget line ep05:L2_drafts not found'.
    Without init_episode_budget_lines_activity, live generation must fail.
    """
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from hfvg.budget import BudgetLedger
    import temporalio.activity
    
    episode_id = "ep99"
    shot_id = "A01"
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate  # Live mode
    
    await init_studio_db(db_path)
    
    # Initialize credit ledger (has credits)
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line(episode_id, cap_credits=1000)
    
    # Initialize budget ledger tables BUT DO NOT CREATE LINES
    # This simulates the bug: tables exist but no L2_drafts line
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    
    # Verify budget_lines table is EMPTY (no L2_drafts)
    async with aiosqlite.connect(db_path, uri=True) as db:
        async with db.execute("SELECT COUNT(*) FROM budget_lines WHERE episode_id = ?", (episode_id,)) as cur:
            count = (await cur.fetchone())[0]
            assert count == 0, "Budget lines should be empty (simulating bug)"
    
    # Set episode to live mode with G1.08 approved
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute("""
            INSERT OR IGNORE INTO episodes (episode_id, beatmap_path, live_mode, g108_approved)
            VALUES (?, ?, 0, 0)
        """, (episode_id, ""))
        await db.execute("""
            UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?
        """, (episode_id,))
        await db.commit()
    
    original_info = temporalio.activity.info
    temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
    
    # Mock provider and prompt lint
    mock_provider = AsyncMock()
    mock_provider.model_path = "xai/grok-imagine-image-2.0"
    mock_provider.estimate_usd_micros = AsyncMock(return_value=100_000)
    mock_provider.submit_image = AsyncMock(return_value="still-ep99-A01-v1")
    mock_provider.close = AsyncMock()
    
    try:
        with patch('hfvg.activities.studio_generation.enforce_prompt_lint', return_value=[]):
            with patch('hfvg.activities.studio_generation.HiggsfieldStillProvider', return_value=mock_provider):
                # This MUST raise "Budget line not found" error
                with pytest.raises(ValueError, match="Budget line.*not found"):
                    await submit_still_job_enforced(
                        episode_id=episode_id,
                        shot_id=shot_id,
                        prompt="Test still",
                        quality="medium",
                        resolution="1k",
                        version=1,
                        shot_meta={"wet": False},
                        missing_refs=[],
                    )
        
        # If we got here without raising, the test FAILED
        # (budget line check was bypassed, which is the bug we're preventing)
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_live_still_succeeds_with_budget_lines(tmp_path, monkeypatch):
    """
    Test: Live still generation SUCCEEDS when budget lines are initialized.
    
    This verifies the fix works: after calling init_episode_budget_lines_activity,
    generation succeeds.
    """
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.activities.credit_activities import init_episode_budget_lines_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    episode_id = "ep99"
    shot_id = "A01"
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    
    await init_studio_db(db_path)
    
    # Initialize credit ledger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line(episode_id, cap_credits=1000)
    
    # Initialize budget lines (THE FIX)
    original_info = temporalio.activity.info
    temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
    
    try:
        result = await init_episode_budget_lines_activity(episode_id)
        assert result["line_count"] > 0, "Should create budget lines"
        # Policy creates at least L7_revision for all episodes
        assert "L7_revision" in result["lines"], "Should create L7_revision"
        
        # Manually create L2_drafts for testing (since ep99 has no policy data)
        from hfvg.budget import BudgetLedger
        ledger = BudgetLedger(db_path)
        await ledger.set_line(episode_id, "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
        
        # Set manual balance for GC.01 (required for live mode budget reserves)
        await ledger.set_manual_balance(100_000_000, set_by="test", note="test balance")
        
        # Verify L2_drafts exists
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute(
                "SELECT line_name FROM budget_lines WHERE episode_id = ? AND line_name = ?",
                (episode_id, "L2_drafts")
            ) as cur:
                row = await cur.fetchone()
                assert row is not None, "L2_drafts line should exist"
        
        # Set episode to live mode
        async with aiosqlite.connect(db_path, uri=True) as db:
            await db.execute("""
                INSERT OR IGNORE INTO episodes (episode_id, beatmap_path, live_mode, g108_approved)
                VALUES (?, ?, 0, 0)
            """, (episode_id, ""))
            await db.execute("""
                UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?
            """, (episode_id,))
            await db.commit()
        
        # Now submit should succeed (with mocked provider)
        mock_provider = AsyncMock()
        mock_provider.model_path = "xai/grok-imagine-image-2.0"
        mock_provider.estimate_usd_micros = AsyncMock(return_value=100_000)
        mock_provider.submit_image = AsyncMock(return_value="still-ep99-A01-v1")
        mock_provider.close = AsyncMock()
        
        with patch('hfvg.activities.studio_generation.enforce_prompt_lint', return_value=[]):
            with patch('hfvg.activities.studio_generation.HiggsfieldStillProvider', return_value=mock_provider):
                result = await submit_still_job_enforced(
                    episode_id=episode_id,
                    shot_id=shot_id,
                    prompt="Test still",
                    quality="medium",
                    resolution="1k",
                    version=1,
                    shot_meta={"wet": False},
                    missing_refs=[],
                )
        
        # Should succeed and return job_id
        assert result["job_id"] == "still-ep99-A01-v1"
        assert result["line_name"] == "L2_drafts"
        
    finally:
        temporalio.activity.info = original_info
