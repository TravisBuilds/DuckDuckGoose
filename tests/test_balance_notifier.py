"""Tests for balance alert notification system (mutation coverage)."""

import pytest
import aiosqlite

from hfvg.balance_notifier import BalanceNotifier, init_notifier_tables


@pytest.mark.asyncio
async def test_alert_deduplication_suppresses_recent_duplicates(tmp_path):
    """Mutation #395: Alert deduplication should suppress duplicate alerts within the window."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize tables
    await init_notifier_tables(db_path)
    
    notifier = BalanceNotifier(db_path)
    
    # Send first alert
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Get alert count after first send
    async with aiosqlite.connect(db_path) as db:
        count1 = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Send duplicate alert immediately (should be deduplicated)
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Get alert count after second send
    async with aiosqlite.connect(db_path) as db:
        count2 = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should still be 1 (deduplicated)
    assert count1[0] == 1, "First alert should be recorded"
    assert count2[0] == 1, "Duplicate alert should be suppressed (count should remain 1)"


@pytest.mark.asyncio
async def test_alert_deduplication_allows_after_window(tmp_path):
    """Test that alerts are allowed after the deduplication window expires."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize tables
    await init_notifier_tables(db_path)
    
    notifier = BalanceNotifier(db_path)
    
    # Send first alert
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Manually age the alert to simulate window expiration
    from datetime import datetime, timezone
    cutoff = datetime.now(timezone.utc).timestamp() - 3700  # 1 hour + 100 seconds ago
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE balance_alerts SET sent_at_unix = ?", (cutoff,))
        await db.commit()
    
    # Send same alert again (should not be deduplicated since window expired)
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Get alert count
    async with aiosqlite.connect(db_path) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should now be 2 (not deduplicated after window expiration)
    assert count[0] == 2, "Alert after window expiration should be allowed"


@pytest.mark.asyncio
async def test_alert_deduplication_scoped_to_episode_and_shot(tmp_path):
    """Mutation #399: Dedupe key must include episode_id and job_id to scope deduplication."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize tables
    await init_notifier_tables(db_path)
    
    notifier = BalanceNotifier(db_path)
    
    # Send 403 stop alert for episode ep01, job A01
    await notifier.send_403_stop_alert(
        provider="higgsfield",
        episode_id="ep01",
        job_id="still-ep01-A01-v1",
        error_message="403 Insufficient balance"
    )
    
    # Send 403 stop alert for episode ep01, DIFFERENT job A02
    # Should NOT be deduplicated because job_id is different
    await notifier.send_403_stop_alert(
        provider="higgsfield",
        episode_id="ep01",
        job_id="still-ep01-A02-v1",
        error_message="403 Insufficient balance"
    )
    
    # Send 403 stop alert for DIFFERENT episode ep02, job A01
    # Should NOT be deduplicated because episode_id is different
    await notifier.send_403_stop_alert(
        provider="higgsfield",
        episode_id="ep02",
        job_id="still-ep02-A01-v1",
        error_message="403 Insufficient balance"
    )
    
    # Get alert count
    async with aiosqlite.connect(db_path) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should be 3 distinct alerts (not deduplicated because episode/job differ)
    assert count[0] == 3, \
        "Alerts with different episode_id or job_id should not be deduplicated"
    
    # Now send a duplicate of the first alert (same episode and job)
    await notifier.send_403_stop_alert(
        provider="higgsfield",
        episode_id="ep01",
        job_id="still-ep01-A01-v1",
        error_message="403 Insufficient balance (duplicate)"
    )
    
    # Get alert count again
    async with aiosqlite.connect(db_path) as db:
        count2 = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should still be 3 (this duplicate was suppressed)
    assert count2[0] == 3, \
        "Duplicate alert for same episode_id and job_id should be suppressed"
