"""Shared fixtures: a synthetic prompt-lint kit is always configured (kit presence only; each
test still has to supply a lint-clean prompt and shot_meta for live submits)."""

import os
import pytest
from pathlib import Path

from tests.lint_helpers import write_kit


@pytest.fixture(autouse=True)
def _restore_environ():
    """G-CI-2: a test that writes os.environ directly (not via monkeypatch) must not leak keys,
    DRY_RUN or cache dirs into later tests - restore the exact environment after every test."""
    saved = dict(os.environ)
    yield
    if dict(os.environ) != saved:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture(autouse=True)
def _isolate_test_dirs(tmp_path, monkeypatch):
    """
    Isolate every test: chdir to tmp_path and point data/DB paths there.
    
    This prevents tests from creating/modifying/deleting the real repo data/ dir.
    All episode dirs, DBs, and outputs go to the test's tmp_path.
    """
    # Change working directory to tmp_path
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    
    # Point all data/DB environment variables to tmp_path
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    
    yield
    
    # Restore original working directory
    os.chdir(original_cwd)


@pytest.fixture(autouse=True)
def _synthetic_lint_kit(tmp_path, monkeypatch):
    kit_path = write_kit(tmp_path / "synthetic_lint_kit.json")
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(kit_path))


@pytest.fixture(autouse=True)
def _restore_real_image_normalize():
    """Undo any passthrough_normalize() a test installed (fake-image tests only)."""
    from tests.fake_image_passthrough import restore_normalize
    restore_normalize()
    yield
    restore_normalize()
# CI validation run 1 for 3-green verification
# CI validation run 2 for 3-green verification
# Final CI validation run for 3-green verification
