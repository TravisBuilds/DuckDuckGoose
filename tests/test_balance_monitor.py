"""Tests for provider balance monitoring and preflight gates."""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import aiosqlite
import pytest
import httpx

from hfvg.balance_monitor import BalanceMonitor, ProviderBalance
from hfvg.balance_notifier import BalanceNotifier
from hfvg.budget import BudgetLedger
from hfvg.pricing import usd_to_micros


@pytest.fixture
async def test_db(tmp_path):
    """Create a test database with budget tables."""
    db_path = str(tmp_path / "test.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    return db_path


@pytest.mark.asyncio
async def test_elevenlabs_balance_check_success(test_db):
    """Test ElevenLabs live balance check with successful API response."""
    monitor = BalanceMonitor(test_db)
    
    # Mock successful API response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "character_limit": 500000,
        "character_count": 250000,
        "status": "active",
        "current_overage": 0,
    }
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        balance = await monitor._check_elevenlabs_balance()
        
        assert balance.provider == "elevenlabs"
        assert balance.source == "live"
        assert balance.age_seconds == 0.0
        assert float(balance.balance_usd) > 0.0
        assert balance.details["remaining_characters"] == 250000


@pytest.mark.asyncio
async def test_elevenlabs_balance_check_401_graceful(test_db):
    """Test ElevenLabs balance check handles 401 gracefully - status unknown, no numeric balance."""
    monitor = BalanceMonitor(test_db)
    
    mock_response = MagicMock()
    mock_response.status_code = 401
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_response
        
        balance = await monitor._check_elevenlabs_balance()
        
        assert balance.provider == "elevenlabs"
        assert balance.source == "live"
        # Defect 12: 401 must NOT report a $0 balance - it's status 'unknown' with no numeric balance
        assert balance.balance_usd is None, "401 must return None balance, not $0"
        assert "401 Unauthorized" in balance.details["error"]


@pytest.mark.asyncio
async def test_higgsfield_balance_derived_from_manual(test_db):
    """Test Higgsfield derived balance = manual - committed spend."""
    monitor = BalanceMonitor(test_db)
    ledger = BudgetLedger(test_db)
    
    # Set manual balance
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (100_000_000, now_iso, "test", "test balance", 0)
        )
        await db.commit()
    
    # Add some committed spend
    await ledger.init_episode_budget("test_ep")
    # Increase L1_refs cap for this test
    async with aiosqlite.connect(test_db) as db:
        await db.execute("UPDATE budget_lines SET cap_usd_micros = ? WHERE episode_id = ? AND line_name = ?",
                         (10_000_000, "test_ep", "L1_refs"))
        await db.commit()
    await ledger.reserve("test_ep", "L1_refs", 5_000_000, "test reservation", job_id="job1")
    await ledger.commit("test_ep", "L1_refs", 5_000_000, 5_000_000, "job1", "test commit")
    
    balance = await monitor._check_higgsfield_balance()
    
    assert balance.provider == "higgsfield"
    assert balance.source == "derived"
    # Manual was $100, spent $5, so remaining should be $95
    assert float(balance.balance_usd) == 95.0
    assert "ledger_spend" in balance.details


@pytest.mark.asyncio
async def test_higgsfield_balance_stale_warning(test_db):
    """Test Higgsfield stale balance detection."""
    monitor = BalanceMonitor(test_db)
    
    # Set manual balance 25 hours ago (> 24h default max age)
    old_time = datetime.now(timezone.utc) - timedelta(hours=25)
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (50_000_000, old_time.isoformat(), "test", "old balance", 0)
        )
        await db.commit()
    
    result = await monitor.check_all_balances()
    
    # Should have a warning about stale balance
    assert any("stale" in w.lower() for w in result.warnings)


@pytest.mark.asyncio
async def test_preflight_gate_blocks_low_balance(test_db):
    """Test preflight gate blocks when balance < estimate + reserve."""
    monitor = BalanceMonitor(test_db)
    
    # Set low manual balance
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (2_000_000, now_iso, "test", "low balance", 0)  # $2.00
        )
        await db.commit()
    
    # Try to reserve $5.00 with 20% reserve = $6.00 required
    estimate_micros = 5_000_000
    
    passed, reason = await monitor.check_preflight_balance("higgsfield", estimate_micros, auto_topup_enabled=False)
    
    assert not passed
    assert "insufficient" in reason.lower()
    assert "$2.00" in reason


@pytest.mark.asyncio
async def test_preflight_gate_warns_with_auto_topup(test_db):
    """Test preflight gate warns but allows when auto top-up is enabled."""
    monitor = BalanceMonitor(test_db)
    
    # Set low manual balance
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (2_000_000, now_iso, "test", "low balance", 0)
        )
        await db.commit()
    
    estimate_micros = 5_000_000
    
    passed, reason = await monitor.check_preflight_balance("higgsfield", estimate_micros, auto_topup_enabled=True)
    
    assert passed
    assert "WARNING" in reason
    assert "auto top-up is enabled" in reason


@pytest.mark.asyncio
async def test_preflight_gate_passes_sufficient_balance(test_db):
    """Test preflight gate passes when balance is sufficient."""
    monitor = BalanceMonitor(test_db)
    
    # Set high manual balance
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (100_000_000, now_iso, "test", "high balance", 0)  # $100.00
        )
        await db.commit()
    
    estimate_micros = 5_000_000  # $5.00 + 20% = $6.00 required
    
    passed, reason = await monitor.check_preflight_balance("higgsfield", estimate_micros, auto_topup_enabled=False)
    
    assert passed
    assert "OK" in reason


@pytest.mark.asyncio
async def test_403_stop_recorded(test_db):
    """Test 403 Insufficient Balance stop is recorded."""
    monitor = BalanceMonitor(test_db)
    
    await monitor.record_403_stop(
        provider="higgsfield",
        episode_id="ep01",
        job_id="job123",
        error_message="403 Insufficient balance"
    )
    
    # Verify recorded
    async with aiosqlite.connect(test_db) as db:
        row = await (await db.execute(
            "SELECT provider, episode_id, job_id, error_message FROM balance_stops"
        )).fetchone()
    
    assert row is not None
    assert row[0] == "higgsfield"
    assert row[1] == "ep01"
    assert row[2] == "job123"
    assert "403" in row[3]


@pytest.mark.asyncio
async def test_alert_deduplication(test_db):
    """Test alert deduplication within window."""
    notifier = BalanceNotifier(test_db)
    
    # Initialize notifier tables
    from hfvg.balance_notifier import init_notifier_tables
    await init_notifier_tables(test_db)
    
    # Send first alert
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Get alert count
    async with aiosqlite.connect(test_db) as db:
        count1 = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Send duplicate alert immediately
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    async with aiosqlite.connect(test_db) as db:
        count2 = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should still be 1 (deduplicated)
    assert count1[0] == 1
    assert count2[0] == 1


@pytest.mark.asyncio
async def test_alert_different_types_not_deduped(test_db):
    """Test different alert types are not deduplicated."""
    notifier = BalanceNotifier(test_db)
    
    from hfvg.balance_notifier import init_notifier_tables
    await init_notifier_tables(test_db)
    
    # Send low balance alert
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    
    # Send critical balance alert (different type)
    await notifier.send_critical_balance_alert("higgsfield", 3.0, 5.0)
    
    async with aiosqlite.connect(test_db) as db:
        count = await (await db.execute("SELECT COUNT(*) FROM balance_alerts")).fetchone()
    
    # Should have 2 alerts
    assert count[0] == 2


@pytest.mark.asyncio
async def test_check_all_balances_integration(test_db):
    """Integration test: check all balances with mocked APIs."""
    monitor = BalanceMonitor(test_db)
    
    # Set up Higgsfield manual balance
    now_iso = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (75_000_000, now_iso, "test", "test balance", 0)
        )
        await db.commit()
    
    # Mock ElevenLabs API
    mock_el_response = MagicMock()
    mock_el_response.status_code = 200
    mock_el_response.json.return_value = {
        "character_limit": 300000,
        "character_count": 100000,
        "status": "active",
    }
    
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_el_response
        
        result = await monitor.check_all_balances()
        
        assert "elevenlabs" in result.providers
        assert "higgsfield" in result.providers
        assert result.providers["higgsfield"].provider == "higgsfield"
        assert result.providers["elevenlabs"].provider == "elevenlabs"
        assert len(result.errors) == 0  # No errors in successful check


def test_balance_to_dict_serialization(test_db):
    """Test ProviderBalance to_dict serialization."""
    balance = ProviderBalance(
        provider="test",
        balance_usd=Decimal("42.50"),
        source="live",
        age_seconds=123.45,
        details={"key": "value"},
    )
    
    d = balance.to_dict()
    
    assert d["provider"] == "test"
    assert d["balance_usd"] == "42.50"
    assert d["balance_usd_micros"] == 42_500_000
    assert d["source"] == "live"


@pytest.mark.asyncio
async def test_preflight_balance_blocks_insufficient_known_balance(test_db):
    """Mutation #392: Preflight balance gate should block when balance < estimate + reserve."""
    monitor = BalanceMonitor(test_db)
    
    # Mock balance check to return low balance ($0.50)
    with patch.object(monitor, 'check_all_balances', new_callable=AsyncMock) as mock_check:
        from hfvg.balance_monitor import BalanceCheckResult
        
        mock_check.return_value = BalanceCheckResult(
            timestamp=datetime.now(timezone.utc),
            providers={
                "higgsfield": ProviderBalance(
                    provider="higgsfield",
                    balance_usd=Decimal("0.50"),
                    source="derived",
                    age_seconds=0.0,
                    details={},
                )
            },
            warnings=[],
            errors=[],
        )
        
        # Check preflight with estimate of $1.00 + 20% reserve = $1.20 required
        passed, reason = await monitor.check_preflight_balance(
            provider="higgsfield",
            estimate_usd_micros=1_000_000,  # $1.00
            auto_topup_enabled=False,
        )
        
        # Should block (not pass)
        assert not passed, "Preflight gate should block when balance < required"
        assert "insufficient" in reason.lower()


@pytest.mark.asyncio
async def test_preflight_balance_blocks_stale_higgsfield_balance(test_db):
    """Mutation #394: Stale balance check should block when age > max_age_hours."""
    monitor = BalanceMonitor(test_db)
    
    # Mock balance check to return stale balance (25 hours old, max is 24)
    with patch.object(monitor, 'check_all_balances', new_callable=AsyncMock) as mock_check:
        from hfvg.balance_monitor import BalanceCheckResult
        
        mock_check.return_value = BalanceCheckResult(
            timestamp=datetime.now(timezone.utc),
            providers={
                "higgsfield": ProviderBalance(
                    provider="higgsfield",
                    balance_usd=Decimal("100.00"),  # Sufficient amount
                    source="derived",
                    age_seconds=25 * 3600,  # 25 hours (stale)
                    details={},
                )
            },
            warnings=[],
            errors=[],
        )
        
        # Check preflight with a small estimate
        passed, reason = await monitor.check_preflight_balance(
            provider="higgsfield",
            estimate_usd_micros=10_000,  # $0.01
            auto_topup_enabled=False,
        )
        
        # Should block due to staleness
        assert not passed, "Preflight gate should block when balance is stale"
        assert "stale" in reason.lower()
