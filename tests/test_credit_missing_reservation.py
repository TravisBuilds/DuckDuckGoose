"""Tests for credit commit/release with missing reservations."""

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.credit_activities import (
    commit_credits_activity,
    release_credits_activity,
)
from hfvg.credit_ledger import CreditLedger
from hfvg.studio_db import init_studio_db, create_episode


@pytest.mark.asyncio
async def test_commit_credits_skips_dry_run_missing_reservation(tmp_path, monkeypatch):
    """
    Test: commit_credits_activity skips silently for DRY_RUN jobs with no reservation.
    
    DRY_RUN jobs (job_id contains -dry-) never reserve credits, so commit should
    be a no-op rather than an error.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB and credit ledger
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Call commit_credits with a DRY_RUN job_id (no reservation exists)
    # Should skip silently without raising
    await commit_credits_activity("ep99", "still-dry-abc123", 10)


@pytest.mark.asyncio
async def test_commit_credits_raises_live_missing_reservation(tmp_path, monkeypatch):
    """
    Test: commit_credits_activity raises non-retryable error for live jobs with no reservation.
    
    Live jobs (job_id does not contain -dry-) must have a reservation before commit.
    Missing reservation indicates a serious bug and should pause the run.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB and credit ledger
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Call commit_credits with a live job_id (no reservation exists)
    # Should raise non-retryable ApplicationError
    with pytest.raises(ApplicationError) as exc_info:
        await commit_credits_activity("ep99", "still-ep99-A01-v1", 10)
    
    assert exc_info.value.non_retryable
    assert "No credit reservation found for live job" in str(exc_info.value)


@pytest.mark.asyncio
async def test_release_credits_skips_dry_run_missing_reservation(tmp_path, monkeypatch):
    """
    Test: release_credits_activity skips silently for DRY_RUN jobs with no reservation.
    
    DRY_RUN jobs never reserve credits, so release should be a no-op.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB and credit ledger
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Call release_credits with a DRY_RUN job_id (no reservation exists)
    # Should skip silently without raising
    await release_credits_activity("ep99", "clip-dry-xyz789", "test release")


@pytest.mark.asyncio
async def test_release_credits_raises_live_missing_reservation(tmp_path, monkeypatch):
    """
    Test: release_credits_activity raises non-retryable error for live jobs with no reservation.
    
    Live jobs must have a reservation before release. Missing reservation indicates
    a serious bug and should pause the run.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB and credit ledger
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Call release_credits with a live job_id (no reservation exists)
    # Should raise non-retryable ApplicationError
    with pytest.raises(ApplicationError) as exc_info:
        await release_credits_activity("ep99", "clip-ep99-B01-v2", "test release")
    
    assert exc_info.value.non_retryable
    assert "No credit reservation found for live job" in str(exc_info.value)
