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
