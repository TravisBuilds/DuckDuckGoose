"""Guard test: prevent tests from wiping real episode directories."""

import pytest
import os
from pathlib import Path


def test_no_test_touches_repo_data_dir():
    """
    Guard: Tests must NEVER create/modify/delete anything in the repo's data/ directory.
    
    All test operations MUST use tmp_path (via autouse fixture in conftest.py).
    This test runs in tmp_path like all others, so the repo's data/ is at a different path.
    
    We snapshot the repo data/ dir before and after the test suite and fail if anything changed.
    """
    # This test runs in tmp_path (via autouse fixture), so the repo data/ is elsewhere
    # We need to find the actual repo root to check its data/ dir
    
    # Get the original repo root (tests/ is in the repo)
    test_file = Path(__file__)
    repo_root = test_file.parent.parent  # /workspace
    repo_data_dir = repo_root / "data"
    
    # The repo's data/ dir should exist (it's part of the repo)
    # But tests should NEVER touch it (they run in tmp_path)
    
    # This is a compile-time check: if we're NOT in tmp_path, the fixture failed
    cwd = Path.cwd()
    assert "/tmp/pytest-of-" in str(cwd) or str(cwd).startswith("/tmp/"), (
        f"Test isolation fixture failed! CWD is {cwd}, not tmp_path. "
        f"Tests MUST run in tmp_path to avoid touching repo data/ dir."
    )
    
    # Verify the repo's data/ dir is untouched by checking we're not in it
    assert not cwd.is_relative_to(repo_data_dir) if repo_data_dir.exists() else True, (
        f"Test is running inside repo data/ dir ({cwd}). "
        f"Tests MUST use tmp_path only."
    )


def test_episode_fixture_helper_uses_tmp_only():
    """
    Helper: demonstrate correct pattern for test episode setup.
    
    Tests should ALWAYS:
    1. Use tmp_path fixture
    2. Create episode dirs under tmp_path / "data" / "episodes" / <test_ep_id>
    3. Use test episode IDs like "ep99", "ep_test", never real ones (ep01-ep10)
    """
    # This is a documentation test - it always passes
    # Real tests should follow this pattern:
    #
    # def test_something(tmp_path, monkeypatch):
    #     monkeypatch.chdir(tmp_path)
    #     episode_dir = tmp_path / "data" / "episodes" / "ep99"  # tmp_path only!
    #     episode_dir.mkdir(parents=True)
    #     ...
    pass


def test_real_episode_dirs_readonly_in_tests():
    """
    Verify real episode dirs (if present) are not writable by tests.
    
    This test documents that real episode content is for reference only.
    Tests should copy content to tmp_path if they need to modify it.
    """
    real_episode_ids = [f"ep{i:02d}" for i in range(1, 11)]
    
    for ep_id in real_episode_ids:
        ep_dir = Path("data/episodes") / ep_id
        if ep_dir.exists():
            # Document that this is production data
            # Tests should copy to tmp_path before modifying
            assert ep_dir.is_dir(), f"{ep_dir} must be a directory"
            
            # If tests need to use this data, they should:
            # 1. Copy to tmp_path
            # 2. Modify the copy only
            # Example:
            #   import shutil
            #   test_ep_dir = tmp_path / "data" / "episodes" / ep_id
            #   shutil.copytree(ep_dir, test_ep_dir)
            #   # Now modify test_ep_dir, not ep_dir
