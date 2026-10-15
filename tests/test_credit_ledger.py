"""Tests for Higgsfield credit ledger with cap-pause."""

import pytest
import tempfile
import os

from hfvg.credit_ledger import CreditLedger, get_credit_settings


@pytest.fixture
async def credit_ledger():
    """Create temporary credit ledger for testing."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
        db_path = f.name
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    yield ledger
    
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass


@pytest.mark.asyncio
async def test_create_episode_line(credit_ledger):
    """Test creating an episode credit line."""
    await credit_ledger.create_episode_line("ep05", cap_credits=500)
    
    status = await credit_ledger.get_episode_status("ep05")
    assert status["cap_credits"] == 500
    assert status["spent_credits"] == 0
    assert status["reserved_credits"] == 0


@pytest.mark.asyncio
async def test_reserve_credits_idempotent(credit_ledger):
    """Test that credit reservation is idempotent."""
    await credit_ledger.create_episode_line("ep05", cap_credits=500)
    
    # First reserve
    result1 = await credit_ledger.reserve_credits("ep05", "job1", 50, "test job")
    assert result1["reserved"] is True
    assert result1["available"] == 450
    
    # Second reserve (idempotent)
    result2 = await credit_ledger.reserve_credits("ep05", "job1", 50, "test job")
    assert result2["reserved"] is True
    assert result2.get("idempotent") is True
    
    # Check status
    status = await credit_ledger.get_episode_status("ep05")
    assert status["reserved_credits"] == 50  # Only reserved once


@pytest.mark.asyncio
async def test_pause_threshold(credit_ledger):
    """Test pause threshold detection."""
    await credit_ledger.create_episode_line("ep05", cap_credits=100)
    
    # Reserve 85 credits (below 90% pause threshold of 90)
    result = await credit_ledger.reserve_credits("ep05", "job1", 85, "")
    assert result["pause_needed"] is False
    
    # Reserve 10 more credits (total 95, above 90% threshold)
    result = await credit_ledger.reserve_credits("ep05", "job2", 10, "")
    assert result["pause_needed"] is True


@pytest.mark.asyncio
async def test_over_cap_rejection(credit_ledger):
    """Test that reservations over cap are rejected."""
    await credit_ledger.create_episode_line("ep05", cap_credits=100)
    
    # Try to reserve 110 credits (over cap)
    result = await credit_ledger.reserve_credits("ep05", "job1", 110, "")
    assert result["reserved"] is False
    assert result["over_cap"] is True


@pytest.mark.asyncio
async def test_commit_credits(credit_ledger):
    """Test committing reserved credits with actual cost."""
    await credit_ledger.create_episode_line("ep05", cap_credits=500)
    
    # Reserve 50 credits
    await credit_ledger.reserve_credits("ep05", "job1", 50, "")
    
    # Commit with actual cost of 45 (under reserve)
    await credit_ledger.commit_credits("ep05", "job1", 45)
    
    status = await credit_ledger.get_episode_status("ep05")
    assert status["spent_credits"] == 45
    assert status["reserved_credits"] == 0  # Reservation released
    
    # Second commit is idempotent
    await credit_ledger.commit_credits("ep05", "job1", 45)
    status = await credit_ledger.get_episode_status("ep05")
    assert status["spent_credits"] == 45  # No double-counting


@pytest.mark.asyncio
async def test_release_credits(credit_ledger):
    """Test releasing reserved credits without committing."""
    await credit_ledger.create_episode_line("ep05", cap_credits=500)
    
    # Reserve 50 credits
    await credit_ledger.reserve_credits("ep05", "job1", 50, "")
    
    # Release (job canceled)
    await credit_ledger.release_credits("ep05", "job1")
    
    status = await credit_ledger.get_episode_status("ep05")
    assert status["spent_credits"] == 0
    assert status["reserved_credits"] == 0


@pytest.mark.asyncio
async def test_run_lock_acquisition(credit_ledger):
    """Test single-run lock prevents concurrent episodes."""
    # First episode acquires lock
    result1 = await credit_ledger.acquire_run_lock("ep05", "wf1", "account1")
    assert result1["acquired"] is True
    assert result1["existing_episode"] is None
    
    # Second episode cannot acquire lock
    result2 = await credit_ledger.acquire_run_lock("ep06", "wf2", "account1")
    assert result2["acquired"] is False
    assert result2["existing_episode"] == "ep05"
    
    # Release lock
    await credit_ledger.release_run_lock("ep05")
    
    # Now second episode can acquire
    result3 = await credit_ledger.acquire_run_lock("ep06", "wf2", "account1")
    assert result3["acquired"] is True


@pytest.mark.asyncio
async def test_multiple_reserves_and_commits(credit_ledger):
    """Test multiple jobs reserving and committing."""
    await credit_ledger.create_episode_line("ep05", cap_credits=500)
    
    # Reserve for 3 jobs
    await credit_ledger.reserve_credits("ep05", "job1", 50, "")
    await credit_ledger.reserve_credits("ep05", "job2", 60, "")
    await credit_ledger.reserve_credits("ep05", "job3", 70, "")
    
    status = await credit_ledger.get_episode_status("ep05")
    assert status["reserved_credits"] == 180
    
    # Commit jobs
    await credit_ledger.commit_credits("ep05", "job1", 48)
    await credit_ledger.commit_credits("ep05", "job2", 65)
    await credit_ledger.commit_credits("ep05", "job3", 70)
    
    status = await credit_ledger.get_episode_status("ep05")
    assert status["spent_credits"] == 183
    assert status["reserved_credits"] == 0
