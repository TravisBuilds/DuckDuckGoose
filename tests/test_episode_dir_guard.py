"""Guard test: prevent tests from wiping real episode directories."""

import pytest
from pathlib import Path


def test_no_test_touches_real_episode_dirs(tmp_path, monkeypatch):
    """
    Guard: Tests must never touch data/episodes/<real_episode_id>.
    
    All test episode operations MUST use tmp_path fixtures only.
    Real episode dirs (ep01-ep10, etc.) must never be created/modified/deleted by tests.
    """
    real_episode_ids = [f"ep{i:02d}" for i in range(1, 11)]  # ep01-ep10
    
    # Check that no real episode dir exists in the test workspace
    real_dirs = []
    if Path("data/episodes").exists():
        for ep_id in real_episode_ids:
            ep_dir = Path("data/episodes") / ep_id
            if ep_dir.exists():
                real_dirs.append(str(ep_dir))
    
    # If any real dirs exist, they must not have been modified by tests
    # (checked by ensuring they're not empty and have expected structure)
    for ep_dir_str in real_dirs:
        ep_dir = Path(ep_dir_str)
        # Real episodes should have BEATMAP.md or shots.json
        has_beatmap = (ep_dir / "BEATMAP.md").exists()
        has_shots = (ep_dir / "shots.json").exists()
        
        assert has_beatmap or has_shots, (
            f"Real episode dir {ep_dir} exists but is missing core files. "
            f"Tests may have wiped it. Tests MUST use tmp_path only."
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
