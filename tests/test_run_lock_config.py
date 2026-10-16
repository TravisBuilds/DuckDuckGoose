"""Tests for configurable run lock with stale-holder expiry."""

import pytest
from datetime import datetime, timezone, timedelta

from hfvg.credit_ledger import CreditLedger
from hfvg.studio_db import init_studio_db


@pytest.mark.asyncio
async def test_lock_uses_configured_account_identifier(tmp_path):
    """
    Test: Lock key uses configured account identifier, not literal "ACCOUNT".
    
    The lock should be scoped to the provided account_identifier parameter.
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Acquire lock with custom account identifier
    result1 = await ledger.acquire_run_lock(
        "ep99", "wf-1", account_identifier="customer-acme-corp"
    )
    
    assert result1["acquired"] is True
    assert result1["existing_episode"] is None
    assert result1["reclaimed"] is False
    
    # Try to acquire another lock with same account identifier (should fail)
    result2 = await ledger.acquire_run_lock(
        "ep100", "wf-2", account_identifier="customer-acme-corp"
    )
    
    assert result2["acquired"] is False
    assert result2["existing_episode"] == "ep99"
    assert result2["reclaimed"] is False
    
    # Acquire lock with different account identifier (should succeed)
    result3 = await ledger.acquire_run_lock(
        "ep100", "wf-2", account_identifier="customer-widgetco"
    )
    
    assert result3["acquired"] is True
    assert result3["existing_episode"] is None
    assert result3["reclaimed"] is False


@pytest.mark.asyncio
async def test_stale_lock_reclaimed_after_timeout(tmp_path):
    """
    Test: Stale lock (older than timeout) is reclaimed by new workflow.
    
    If a lock is older than stale_timeout_seconds, it can be reclaimed.
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Acquire initial lock
    result1 = await ledger.acquire_run_lock(
        "ep99", "wf-1", account_identifier="test-account"
    )
    
    assert result1["acquired"] is True
    
    # Manually backdate the lock to make it stale
    import aiosqlite
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE run_locks SET locked_at = ? WHERE episode_id = ?",
            (stale_time, "ep99")
        )
        await db.commit()
    
    # Try to acquire lock with same account (stale timeout = 1 hour)
    # The lock is 2 hours old, so it should be reclaimed
    result2 = await ledger.acquire_run_lock(
        "ep100", "wf-2", 
        account_identifier="test-account",
        stale_timeout_seconds=3600  # 1 hour
    )
    
    assert result2["acquired"] is True
    assert result2["existing_episode"] == "ep99"  # Shows which episode was evicted
    assert result2["reclaimed"] is True
    
    # Verify the new lock is in place
    result3 = await ledger.acquire_run_lock(
        "ep101", "wf-3", 
        account_identifier="test-account",
        stale_timeout_seconds=3600
    )
    
    assert result3["acquired"] is False
    assert result3["existing_episode"] == "ep100"
    assert result3["reclaimed"] is False


@pytest.mark.asyncio
async def test_fresh_lock_blocks_acquisition(tmp_path):
    """
    Test: Fresh lock (within timeout) blocks new acquisition.
    
    If a lock is fresh (within stale_timeout_seconds), it cannot be reclaimed.
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Acquire initial lock
    result1 = await ledger.acquire_run_lock(
        "ep99", "wf-1", 
        account_identifier="test-account",
        stale_timeout_seconds=3600  # 1 hour
    )
    
    assert result1["acquired"] is True
    
    # Immediately try to acquire another lock (should fail - lock is fresh)
    result2 = await ledger.acquire_run_lock(
        "ep100", "wf-2", 
        account_identifier="test-account",
        stale_timeout_seconds=3600
    )
    
    assert result2["acquired"] is False
    assert result2["existing_episode"] == "ep99"
    assert result2["reclaimed"] is False


@pytest.mark.asyncio
async def test_configurable_stale_timeout(tmp_path):
    """
    Test: Stale timeout is configurable per call.
    
    Different timeout values determine when a lock is considered stale.
    """
    db_path = str(tmp_path / "test.db")
    
    await init_studio_db(db_path)
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Acquire initial lock
    result1 = await ledger.acquire_run_lock(
        "ep99", "wf-1", account_identifier="test-account"
    )
    assert result1["acquired"] is True
    
    # Backdate lock by 30 minutes
    import aiosqlite
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat().replace('+00:00', 'Z')
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE run_locks SET locked_at = ? WHERE episode_id = ?",
            (stale_time, "ep99")
        )
        await db.commit()
    
    # Try with 1 hour timeout (30 min old lock is fresh)
    result2 = await ledger.acquire_run_lock(
        "ep100", "wf-2", 
        account_identifier="test-account",
        stale_timeout_seconds=3600  # 1 hour
    )
    assert result2["acquired"] is False
    assert result2["existing_episode"] == "ep99"
    
    # Try with 15 minute timeout (30 min old lock is stale)
    result3 = await ledger.acquire_run_lock(
        "ep100", "wf-2", 
        account_identifier="test-account",
        stale_timeout_seconds=900  # 15 minutes
    )
    assert result3["acquired"] is True
    assert result3["existing_episode"] == "ep99"
    assert result3["reclaimed"] is True
