"""Tests for Higgsfield 403 'Insufficient balance' handling (Defect 9)."""

import pytest
import httpx
from unittest.mock import AsyncMock, patch, MagicMock
from temporalio.exceptions import ApplicationError

from hfvg.activities.studio_generation import submit_still_job_enforced
from hfvg.balance_monitor import BalanceMonitor, init_balance_tables
from hfvg.balance_notifier import BalanceNotifier, init_notifier_tables
from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger


@pytest.mark.asyncio
async def test_403_releases_reservations_and_records_stop(tmp_path, monkeypatch):
    """Test that Higgsfield 403 releases budget/credit reservations and records the stop."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    
    # Disable network alerts
    monkeypatch.delenv("BALANCE_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BALANCE_ALERT_SMTP_HOST", raising=False)
    
    # Initialize tables
    from hfvg.studio_db import init_studio_db
    await init_studio_db(db_path)
    await init_balance_tables(db_path)
    await init_notifier_tables(db_path)
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Initialize episode and lines
    await ledger.init_episode_budget("ep01")
    # Manually increase L2_drafts cap and mark episode as live
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE budget_lines SET cap_usd_micros = ? WHERE episode_id = ? AND line_name = ?",
            (1_000_000, "ep01", "L2_drafts")
        )
        # Mark episode as live mode with gate status approved
        await db.execute(
            "INSERT OR REPLACE INTO episodes (episode_id, live_mode, g108_approved) VALUES (?, ?, ?)",
            ("ep01", 1, 1)
        )
        await db.commit()
    
    episode_id = "ep01"
    shot_id = "A01"
    
    # Mock the provider to raise 403
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    
    with patch.object(HiggsfieldStillProvider, 'submit_image', new_callable=AsyncMock) as mock_submit, \
         patch.object(HiggsfieldStillProvider, 'estimate_usd_micros', new_callable=AsyncMock) as mock_estimate_usd, \
         patch.object(HiggsfieldStillProvider, 'estimate_cost', new_callable=AsyncMock) as mock_estimate_credits, \
         patch.object(HiggsfieldStillProvider, 'estimate_usd_and_credits', new_callable=AsyncMock) as mock_estimate_both, \
         patch.object(HiggsfieldStillProvider, 'upload_reference', new_callable=AsyncMock) as mock_upload, \
         patch('hfvg.studio_db.enforce_smoke_test_gate', new_callable=AsyncMock), \
         patch('hfvg.balance_monitor.BalanceMonitor.check_preflight_balance', new_callable=AsyncMock) as mock_preflight, \
         patch('hfvg.activities.studio_generation.enforce_prompt_lint'):
        
        # Mock successful preflight
        mock_preflight.return_value = (True, "Balance OK")
        
        # Mock estimates
        mock_estimate_usd.return_value = 50_000  # $0.05
        mock_estimate_credits.return_value = 0.5
        mock_estimate_both.return_value = (50_000, 0.5)
        
        # Mock upload (returns empty list for uploaded refs)
        mock_upload.return_value = []
        
        # Mock 403 error
        mock_response = AsyncMock()
        mock_response.status_code = 403
        mock_response.text = "Insufficient balance"
        mock_response.request = AsyncMock()
        
        error_403 = httpx.HTTPStatusError(
            "403 Insufficient balance",
            request=mock_response.request,
            response=mock_response
        )
        mock_submit.side_effect = error_403
        
        # Try to generate a still - should raise ApplicationError with InsufficientBalance type
        with pytest.raises(ApplicationError) as exc_info:
            await submit_still_job_enforced(
                episode_id=episode_id,
                shot_id=shot_id,
                prompt="test prompt",
                version=1,
                attempt=1,
                resolution="1k",
                quality="medium",
                aspect_ratio="16:9",
                refs=[],
                lead_ref=None,
                moderation_retry=None,
                review_round=None,
                tier=None,
            )
        
        # Verify it's a non-retryable InsufficientBalance error
        assert exc_info.value.type == "InsufficientBalance"
        assert not exc_info.value.non_retryable == False  # Should be non-retryable
        assert "403" in str(exc_info.value)
        
        # Verify reservations were released (balance should be back to initial)
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            # Check L2_drafts balance
            row = await (await db.execute(
                "SELECT reserved FROM budget_lines WHERE episode_id = ? AND line_name = ?",
                (episode_id, "L2_drafts")
            )).fetchone()
            
            # No reservations should remain
            assert row[0] == 0, f"Expected 0 reserved, got {row[0]}"
        
        # Verify the stop was recorded
        monitor = BalanceMonitor(db_path)
        async with aiosqlite.connect(db_path) as db:
            stops = await (await db.execute(
                "SELECT provider, episode_id, job_id FROM balance_stops"
            )).fetchall()
            
            assert len(stops) == 1
            assert stops[0][0] == "higgsfield"
            assert stops[0][1] == episode_id


@pytest.mark.asyncio
async def test_resume_after_topup_no_double_charge(tmp_path, monkeypatch):
    """Test that resuming after a 403 and top-up doesn't double-charge."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    
    # Disable network alerts
    monkeypatch.delenv("BALANCE_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("BALANCE_ALERT_SMTP_HOST", raising=False)
    
    # Initialize tables
    from hfvg.studio_db import init_studio_db
    await init_studio_db(db_path)
    await init_balance_tables(db_path)
    await init_notifier_tables(db_path)
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Initialize episode and lines
    await ledger.init_episode_budget("ep02")
    # Manually increase L2_drafts cap and mark episode as live
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE budget_lines SET cap_usd_micros = ? WHERE episode_id = ? AND line_name = ?",
            (1_000_000, "ep02", "L2_drafts")
        )
        # Mark episode as live mode with gate status approved
        await db.execute(
            "INSERT OR REPLACE INTO episodes (episode_id, live_mode, g108_approved) VALUES (?, ?, ?)",
            ("ep02", 1, 1)
        )
        await db.commit()
    
    episode_id = "ep02"
    shot_id = "B01"
    
    # Mock the provider
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    
    submit_call_count = 0
    
    async def mock_submit_fn(*args, **kwargs):
        nonlocal submit_call_count
        submit_call_count += 1
        
        # First call: 403
        if submit_call_count == 1:
            mock_response = AsyncMock()
            mock_response.status_code = 403
            mock_response.text = "Insufficient balance"
            mock_response.request = AsyncMock()
            
            raise httpx.HTTPStatusError(
                "403 Insufficient balance",
                request=mock_response.request,
                response=mock_response
            )
        
        # Second call (after "top-up"): succeed
        return "job-456"
    
    with patch.object(HiggsfieldStillProvider, 'submit_image', new_callable=AsyncMock) as mock_submit, \
         patch.object(HiggsfieldStillProvider, 'estimate_usd_micros', new_callable=AsyncMock) as mock_estimate_usd, \
         patch.object(HiggsfieldStillProvider, 'estimate_cost', new_callable=AsyncMock) as mock_estimate_credits, \
         patch.object(HiggsfieldStillProvider, 'estimate_usd_and_credits', new_callable=AsyncMock) as mock_estimate_both, \
         patch.object(HiggsfieldStillProvider, 'upload_reference', new_callable=AsyncMock) as mock_upload, \
         patch('hfvg.studio_db.enforce_smoke_test_gate', new_callable=AsyncMock), \
         patch('hfvg.balance_monitor.BalanceMonitor.check_preflight_balance', new_callable=AsyncMock) as mock_preflight, \
         patch('hfvg.credit_ledger.CreditLedger.persist_provider_job', new_callable=AsyncMock), \
         patch('hfvg.activities.studio_generation.enforce_prompt_lint'):
        
        # Setup mocks
        mock_submit.side_effect = mock_submit_fn
        mock_estimate_usd.return_value = 50_000  # $0.05
        mock_estimate_credits.return_value = 0.5
        mock_estimate_both.return_value = (50_000, 0.5)
        mock_preflight.return_value = (True, "Balance OK")
        
        # Mock upload (returns empty list for uploaded refs)
        mock_upload.return_value = []
        
        # First attempt: should fail with 403
        with pytest.raises(ApplicationError) as exc_info:
            await submit_still_job_enforced(
                episode_id=episode_id,
                shot_id=shot_id,
                prompt="test prompt",
                version=1,
                attempt=1,
                resolution="1k",
                quality="medium",
                aspect_ratio="16:9",
                refs=[],
                lead_ref=None,
                moderation_retry=None,
                review_round=None,
                tier=None,
            )
        
        assert exc_info.value.type == "InsufficientBalance"
        
        # Simulate "top-up" - just verify balance was released
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            row = await (await db.execute(
                "SELECT reserved, committed FROM budget_lines WHERE episode_id = ? AND line_name = ?",
                (episode_id, "L2_drafts")
            )).fetchone()
            
            reserved_after_403 = row[0]
            committed_after_403 = row[1]
            
            # No reservations or commits should remain after 403 release
            assert reserved_after_403 == 0, f"Expected 0 reserved after 403, got {reserved_after_403}"
            assert committed_after_403 == 0, f"Expected 0 committed after 403, got {committed_after_403}"
        
        # Second attempt (after "top-up"): should succeed
        result = await submit_still_job_enforced(
            episode_id=episode_id,
            shot_id=shot_id,
            prompt="test prompt",
            version=1,
            attempt=1,
            resolution="1k",
            quality="medium",
            aspect_ratio="16:9",
            refs=[],
            lead_ref=None,
            moderation_retry=None,
            review_round=None,
            tier=None,
        )
        
        # Should have succeeded
        assert result["provider_job_id"] == "job-456"
        
        # Verify ledger now shows only ONE reservation + commit (not double)
        async with aiosqlite.connect(db_path) as db:
            row = await (await db.execute(
                "SELECT reserved, committed FROM budget_lines WHERE episode_id = ? AND line_name = ?",
                (episode_id, "L2_drafts")
            )).fetchone()
            
            reserved_after_success = row[0]
            committed_after_success = row[1]
            
            # Should have ONE reservation (50k micros = $0.05)
            assert reserved_after_success == 50_000, f"Expected 50000 reserved after success, got {reserved_after_success}"
            
            # Committed should still be 0 until polling completes (not tested here)
            # This verifies we didn't double-reserve
