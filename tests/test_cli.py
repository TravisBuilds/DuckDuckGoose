"""Tests for hfvg CLI commands."""

import pytest
import subprocess
import sys
from pathlib import Path


def test_lint_cli_exits_nonzero_on_missing_episode(tmp_path, monkeypatch):
    """Test: hfvg lint exits with non-zero code when episode directory is missing."""
    monkeypatch.chdir(tmp_path)
    
    # Run lint on non-existent episode
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "lint", "ep999"],
        capture_output=True,
        text=True
    )
    
    # Should exit non-zero on missing directory
    assert result.returncode != 0, "CLI should exit non-zero on missing episode"
    assert "not found" in result.stderr.lower()


def test_lint_cli_exits_nonzero_on_missing_beatmap(tmp_path, monkeypatch):
    """Test: hfvg lint exits with non-zero code when BEATMAP.md is missing."""
    monkeypatch.chdir(tmp_path)
    
    # Create episode directory but no BEATMAP.md
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True, exist_ok=True)
    
    # Run lint
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "lint", "ep99"],
        capture_output=True,
        text=True
    )
    
    # Should exit non-zero on missing BEATMAP
    assert result.returncode != 0, "CLI should exit non-zero on missing BEATMAP"
    assert "BEATMAP" in result.stderr or "issue" in result.stderr.lower()


def test_lint_cli_exits_zero_on_valid_episode(tmp_path, monkeypatch):
    """Test: hfvg lint exits with zero code when episode is valid."""
    monkeypatch.chdir(tmp_path)
    
    # Create valid episode structure
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True, exist_ok=True)
    
    # Create BEATMAP.md
    beatmap = episode_dir / "BEATMAP.md"
    beatmap.write_text("""
## A01 - Opening scene
Duration: 3 seconds
1 duck waddles in. Camera follows.
""")
    
    # Run lint
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "lint", "ep99"],
        capture_output=True,
        text=True
    )
    
    # Should exit zero on valid episode
    assert result.returncode == 0, f"CLI should exit zero on valid episode, stderr: {result.stderr}, stdout: {result.stdout}"


def test_balance_cli_dry_run(tmp_path, monkeypatch):
    """Test: hfvg balance in DRY_RUN mode."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    db_path = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize database tables
    import asyncio
    from hfvg.budget import BudgetLedger
    import aiosqlite
    from datetime import datetime, timezone
    
    async def init_db():
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        # Add a manual balance entry so the check passes
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (50_000_000, datetime.now(timezone.utc).isoformat(), "test", "test balance", 0)
            )
            await db.commit()
    
    asyncio.run(init_db())
    
    # Run balance command
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "balance"],
        capture_output=True,
        text=True
    )
    
    # Should exit zero with balance data available
    assert result.returncode == 0, f"CLI should exit zero in DRY_RUN, stderr: {result.stderr}"
    
    # Should indicate dry run (DRY_RUN message is in stderr)
    assert "DRY_RUN" in result.stderr.upper() or "dry" in result.stderr.lower(), \
        f"Output should indicate DRY_RUN mode in stderr: {result.stderr}"


@pytest.mark.asyncio
async def test_balance_check_with_mock(tmp_path):
    """Test: balance check returns balance data with mocked providers."""
    from hfvg.balance_monitor import BalanceMonitor, init_balance_tables
    from unittest.mock import AsyncMock, patch
    from decimal import Decimal
    
    db_path = str(tmp_path / "test.db")
    
    # Initialize tables
    await init_balance_tables(db_path)
    
    monitor = BalanceMonitor(db_path)
    
    # Mock the check methods to return test data
    with patch.object(monitor, '_check_elevenlabs_balance', new_callable=AsyncMock) as mock_el, \
         patch.object(monitor, '_check_higgsfield_balance', new_callable=AsyncMock) as mock_hf:
        
        # Mock ElevenLabs response
        from hfvg.balance_monitor import ProviderBalance
        mock_el.return_value = ProviderBalance(
            provider="elevenlabs",
            balance_usd=Decimal("50.00"),
            source="live",
            age_seconds=0.0,
            details={}
        )
        
        # Mock Higgsfield response
        mock_hf.return_value = ProviderBalance(
            provider="higgsfield",
            balance_usd=Decimal("100.00"),
            source="derived",
            age_seconds=3600.0,
            details={}
        )
        
        # Check balances
        result = await monitor.check_all_balances()
        
        # Verify
        assert "elevenlabs" in result.providers
        assert "higgsfield" in result.providers
        assert result.providers["elevenlabs"].balance_usd == Decimal("50.00")
        assert result.providers["higgsfield"].balance_usd == Decimal("100.00")
        assert len(result.errors) == 0


def test_balance_cli_fresh_db(tmp_path, monkeypatch):
    """Task 14: hfvg balance works on fresh DB without manual init."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    db_path = str(tmp_path / "fresh.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Don't initialize DB - the command should handle it
    # Run balance command
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "balance"],
        capture_output=True,
        text=True
    )
    
    # Should exit zero (no "no such table" error)
    assert result.returncode == 0, f"CLI should handle fresh DB, stderr: {result.stderr}"
    assert "no such table" not in result.stderr.lower()


def test_balance_cli_dry_run_with_1(tmp_path, monkeypatch):
    """Task 14: DRY_RUN=1 works the same as DRY_RUN=true."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "1")  # Use "1" instead of "true"
    db_path = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Run balance command
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "balance"],
        capture_output=True,
        text=True
    )
    
    # Should exit zero and indicate dry run
    assert result.returncode == 0, f"CLI should work with DRY_RUN=1, stderr: {result.stderr}"
    assert "DRY_RUN" in result.stderr.upper() or "dry" in result.stderr.lower()


@pytest.mark.asyncio
async def test_balance_check_no_network_calls_in_dry_run(tmp_path, respx_mock):
    """Task 14: Balance check makes no network calls in DRY_RUN mode."""
    from hfvg.balance_monitor import BalanceMonitor, init_balance_tables
    
    db_path = str(tmp_path / "test.db")
    await init_balance_tables(db_path)
    
    # Set DRY_RUN via monkeypatch
    import os
    original_dry_run = os.environ.get("DRY_RUN")
    os.environ["DRY_RUN"] = "true"
    
    try:
        monitor = BalanceMonitor(db_path)
        
        # respx_mock will fail if any HTTP requests are made
        result = await monitor.check_all_balances()
        
        # Verify result is returned but marked as DRY_RUN
        assert "elevenlabs" in result.providers
        assert "higgsfield" in result.providers
        assert len(result.warnings) > 0
        assert any("DRY_RUN" in w for w in result.warnings)
    finally:
        if original_dry_run is None:
            os.environ.pop("DRY_RUN", None)
        else:
            os.environ["DRY_RUN"] = original_dry_run
