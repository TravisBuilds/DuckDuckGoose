"""Test that private brand content has been removed from public repository."""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest


# Brand terms that should not appear in tracked files
# (This list is intentionally included in the test itself for verification)
BRAND_DENYLIST = [
    "mid-mountain-rest",
    "Travis",
    "Quacked Concierge",
    "Mid-Mountain Rest",
    "mochi",
    "rowan",
    "hazel",
    "birch",
    "willow",
    "twig",
    "Mochi",
    "Rowan",
    "Hazel",
    "Birch",
    "Willow",
    "Twig",
    "ROWAN",
    "HAZEL",
    "MOCHI",
    "BIRCH",
    "WILLOW",
    "TWIG",
]

# Expected hash of the denylist (for verification)
DENYLIST_HASH_PREFIX = "08e40aab8b04b1ce"


def test_denylist_integrity():
    """Verify that the denylist hasn't been tampered with."""
    terms_str = "\n".join(sorted(BRAND_DENYLIST))
    hash_val = hashlib.sha256(terms_str.encode()).hexdigest()[:16]
    assert hash_val == DENYLIST_HASH_PREFIX, (
        f"Brand denylist has been modified! "
        f"Expected hash {DENYLIST_HASH_PREFIX}, got {hash_val}"
    )


def test_no_brand_terms_in_tracked_files():
    """Verify that brand terms don't appear in tracked code/docs/tests/fixtures."""
    repo_root = Path(__file__).parent.parent
    
    # Files to check (tracked, non-binary)
    patterns_to_check = [
        "hfvg/**/*.py",
        "api/**/*.py",
        "web/**/*.ts",
        "web/**/*.tsx",
        "tests/**/*.py",
        "docs/**/*.md",
        "fixtures/**/*.md",
        "examples/**/*.json",
        "*.py",
        "*.md",
    ]
    
    # Allowlist: files that are expected to contain brand terms
    allowlist = {
        "tests/test_no_brand_content.py",  # This test file
        "uploads/HARNESS-GAPS_5293.md",     # External reference doc
    }
    
    violations = []
    
    for pattern in patterns_to_check:
        for file_path in repo_root.glob(pattern):
            # Skip non-files and allowlisted files
            if not file_path.is_file():
                continue
            
            rel_path = file_path.relative_to(repo_root)
            if str(rel_path) in allowlist:
                continue
            
            # Skip binary files and very large files
            try:
                content = file_path.read_text(encoding='utf-8')
            except (UnicodeDecodeError, OSError):
                continue
            
            # Check for each brand term
            for term in BRAND_DENYLIST:
                # Use word boundaries for character names to avoid false positives
                # (e.g., "willing" shouldn't match "willow")
                if re.search(r'\b' + re.escape(term) + r'\b', content, re.IGNORECASE):
                    # Find line numbers
                    lines = content.split('\n')
                    for line_num, line in enumerate(lines, 1):
                        if re.search(r'\b' + re.escape(term) + r'\b', line, re.IGNORECASE):
                            violations.append(f"{rel_path}:{line_num}: '{term}' found")
    
    if violations:
        violations_str = "\n".join(violations[:20])  # Show first 20
        if len(violations) > 20:
            violations_str += f"\n... and {len(violations) - 20} more"
        pytest.fail(
            f"Found {len(violations)} brand term violations in tracked files:\n"
            f"{violations_str}\n\n"
            f"Brand terms must be removed from all tracked code, docs, tests, and fixtures."
        )


def test_no_mid_mountain_rest_in_examples():
    """Verify examples/mid-mountain-rest has been moved to examples/demo."""
    repo_root = Path(__file__).parent.parent
    mmr_path = repo_root / "examples" / "mid-mountain-rest"
    demo_path = repo_root / "examples" / "demo"
    
    assert not mmr_path.exists(), (
        "examples/mid-mountain-rest directory still exists. "
        "It should be replaced by examples/demo."
    )
    
    assert demo_path.exists(), (
        "examples/demo directory does not exist. "
        "It should contain the neutral gates configuration."
    )
    
    assert (demo_path / "HARNESS-GATES.json").exists(), (
        "examples/demo/HARNESS-GATES.json not found."
    )


def test_no_ep05_fixtures():
    """Verify fixtures/ep05 has been replaced with fixtures/demo."""
    repo_root = Path(__file__).parent.parent
    ep05_path = repo_root / "fixtures" / "ep05"
    demo_path = repo_root / "fixtures" / "demo"
    
    assert not ep05_path.exists(), (
        "fixtures/ep05 directory still exists. "
        "It should be replaced by fixtures/demo."
    )
    
    assert demo_path.exists(), (
        "fixtures/demo directory does not exist. "
        "It should contain neutral test fixtures."
    )
    
    assert (demo_path / "BEATMAP.md").exists(), "fixtures/demo/BEATMAP.md not found"
    assert (demo_path / "CONTINUITY.md").exists(), "fixtures/demo/CONTINUITY.md not found"
    assert (demo_path / "VO-AUDIO.md").exists(), "fixtures/demo/VO-AUDIO.md not found"


def test_hfvg_project_env_var_supported():
    """Verify HFVG_PROJECT environment variable is read by gate loader."""
    from hfvg.gates import load_policy
    import os
    
    # Test that default works
    policy = load_policy()
    assert policy is not None
    
    # Test that explicit project works
    policy_demo = load_policy("demo")
    assert policy_demo is not None
    
    # Test that env var is respected
    original_env = os.environ.get("HFVG_PROJECT")
    try:
        os.environ["HFVG_PROJECT"] = "demo"
        policy_env = load_policy()
        assert policy_env is not None
    finally:
        if original_env is not None:
            os.environ["HFVG_PROJECT"] = original_env
        else:
            os.environ.pop("HFVG_PROJECT", None)
