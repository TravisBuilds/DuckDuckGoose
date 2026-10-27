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
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    
    # Run balance command
    result = subprocess.run(
        [sys.executable, "-m", "hfvg.cli", "balance"],
        capture_output=True,
        text=True
    )
    
    # Should exit zero
    assert result.returncode == 0, f"CLI should exit zero in DRY_RUN, stderr: {result.stderr}"
    
    # Should indicate dry run
    assert "DRY_RUN" in result.stdout.upper() or "dry" in result.stdout.lower(), \
        f"Output should indicate DRY_RUN mode: {result.stdout}"


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
