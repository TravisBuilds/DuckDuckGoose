"""Test account-level run lock prevents concurrent episodes."""

import pytest

from hfvg.credit_ledger import CreditLedger


@pytest.mark.asyncio
async def test_run_lock_prevents_concurrent_episodes(tmp_path):
    """Test that acquiring a run lock prevents a second episode from starting."""
    db_path = tmp_path / "lock_test.db"
    ledger = CreditLedger(str(db_path))
    await ledger.init_db()
    
    # First episode acquires lock
    result1 = await ledger.acquire_run_lock("ep01", "wf-ep01", "test-account")
    assert result1["acquired"] is True
    assert result1["existing_episode"] is None
    
    # Second episode on same account is blocked
    result2 = await ledger.acquire_run_lock("ep02", "wf-ep02", "test-account")
    assert result2["acquired"] is False
    assert result2["existing_episode"] == "ep01"
    
    # Release first lock
    await ledger.release_run_lock("ep01")
    
    # Now second episode can acquire
    result3 = await ledger.acquire_run_lock("ep02", "wf-ep02", "test-account")
    assert result3["acquired"] is True
    assert result3["existing_episode"] is None


@pytest.mark.asyncio
async def test_run_lock_different_accounts_independent(tmp_path):
    """Test that different accounts can run concurrently."""
    db_path = tmp_path / "lock_test.db"
    ledger = CreditLedger(str(db_path))
    await ledger.init_db()
    
    # Account A acquires lock
    result1 = await ledger.acquire_run_lock("ep01", "wf-ep01", "account-a")
    assert result1["acquired"] is True
    
    # Account B can also acquire (different account)
    result2 = await ledger.acquire_run_lock("ep02", "wf-ep02", "account-b")
    assert result2["acquired"] is True
    
    # Clean up
    await ledger.release_run_lock("ep01")
    await ledger.release_run_lock("ep02")


@pytest.mark.asyncio
async def test_run_lock_release_idempotent(tmp_path):
    """Test that releasing a non-existent lock is safe."""
    db_path = tmp_path / "lock_test.db"
    ledger = CreditLedger(str(db_path))
    await ledger.init_db()
    
    # Release non-existent lock (should not error)
    await ledger.release_run_lock("ep99")
    
    # Acquire and release normally
    await ledger.acquire_run_lock("ep01", "wf-ep01", "test")
    await ledger.release_run_lock("ep01")
    
    # Release again (idempotent)
    await ledger.release_run_lock("ep01")
