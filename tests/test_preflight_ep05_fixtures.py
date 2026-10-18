"""Preflight tests with real-shaped ep05 fixtures."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_preflight_passes_with_ep05_shaped_fixtures(tmp_path, monkeypatch):
    """
    Test: Real-shaped ep05 with BEATMAP.md, A02 with 5 refs (at the cap), live mode passes.
    Verifies:
    - BEATMAP.md (not beatmap.txt) is accepted
    - 5 refs for A02 is exactly the still model (Grok Image 2.0) effective cap of 5
    """
    from hfvg.activities.preflight import preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    # Set up ep05 directory with real-shaped fixtures
    ep_dir = tmp_path / "data" / "episodes" / "ep05"
    ep_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. BEATMAP.md (workflow uses this, not beatmap.txt)
    (ep_dir / "BEATMAP.md").write_text("""# Episode 5: Duck Family Pool Day

## A01
Duck arrives at the backyard pool, excited.

## A02
Duck family gathers around the pool (6 characters: Duck, Rowan, Hazel, Mochi, Family, Pool).
""")
    
    # 2. shots.json with A02 having 5 refs (at Grok Image 2.0 effective cap of 5)
    (ep_dir / "shots.json").write_text(json.dumps({
        "shots": [
            {
                "id": "A01",
                "prompt": "Duck arrives at pool",
                "count": 1,
                "characters": ["D"],
                "wet": False,
                "model": "kling",
                "duration": 3,
                "end_frame": False,
                "ref_keys": ["r_duck"]  # 1 ref, well within cap
            },
            {
                "id": "A02",
                "prompt": "Duck family gathers",
                "count": 6,
                "characters": ["D", "R", "H", "M", "F"],
                "wet": False,
                "model": "kling",
                "duration": 3,
                "end_frame": False,
                "ref_keys": [
                    "r_duck",
                    "r_hazel",
                    "r_mochi",
                    "r_family",
                    "r_pool"
                ]  # 5 refs, at Grok Image 2.0 effective cap of 5
            }
        ]
    }))
    
    # 3. refs.json with per_shot refs
    (ep_dir / "refs.json").write_text(json.dumps({
        "per_shot": {
            "A01": [
                {"key": "r_duck", "path": "data/episodes/ep05/refs/duck.png"}
            ],
            "A02": [
                {"key": "r_duck", "path": "data/episodes/ep05/refs/duck.png"},
                {"key": "r_rowan", "path": "data/episodes/ep05/refs/rowan.png"},
                {"key": "r_hazel", "path": "data/episodes/ep05/refs/hazel.png"},
                {"key": "r_mochi", "path": "data/episodes/ep05/refs/mochi.png"},
                {"key": "r_family", "path": "data/episodes/ep05/refs/family.png"},
                {"key": "r_pool", "path": "data/episodes/ep05/refs/pool.png"}
            ]
        }
    }))
    
    # 4. Create actual ref files (preflight checks they exist)
    refs_dir = ep_dir / "refs"
    refs_dir.mkdir(exist_ok=True)
    for ref_name in ["duck", "rowan", "hazel", "mochi", "family", "pool"]:
        # Create minimal valid PNG
        from PIL import Image
        img = Image.new('RGB', (64, 64), color='blue')
        img.save(refs_dir / f"{ref_name}.png")
    
    # 4. prompt_kit.json (required for live mode)
    (ep_dir / "prompt_kit.json").write_text(json.dumps({
        "count_template": "Exactly {n} animals in frame",
        "realtime_pattern": "real-time.*no slow motion"
    }))
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-preflight'})()
        
        # Run preflight in live mode (should pass)
        result = await preflight_validate_episode_activity(
            episode_id="ep05",
            shots_subset=None,  # Check all shots
            is_live=True
        )
        
        # Should pass all checks
        assert result["status"] == "passed", \
            f"Preflight should pass with ep05 fixtures, got: {result}"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_fails_when_refs_exceed_still_model_cap(tmp_path, monkeypatch):
    """
    Test: Shot with 6 refs exceeds still model (Grok Image 2.0) effective cap of 5, fails preflight.
    Verifies preflight checks against still model cap, not clip model cap.
    """
    from hfvg.activities.preflight import preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    # Set up ep05 directory
    ep_dir = tmp_path / "data" / "episodes" / "ep05"
    ep_dir.mkdir(parents=True, exist_ok=True)
    
    # BEATMAP.md
    (ep_dir / "BEATMAP.md").write_text("""# Episode 5

## A01
Shot with too many refs (9 > 8 cap).
""")
    
    # shots.json with A01 having 6 refs (exceeds Grok Image 2.0 effective cap of 5)
    (ep_dir / "shots.json").write_text(json.dumps({
        "shots": [
            {
                "id": "A01",
                "prompt": "Big scene with all characters",
                "count": 9,
                "characters": ["D", "R", "H", "M"],
                "wet": False,
                "model": "kling",  # Clip model (irrelevant for ref cap)
                "duration": 3,
                "end_frame": False,
                "ref_keys": [
                    "r_duck",
                    "r_rowan",
                    "r_hazel",
                    "r_mochi",
                    "r_family",
                    "r_pool"
                ]  # 6 refs: one over the effective cap of 5 (the count that failed live)
            }
        ]
    }))
    
    # refs.json
    (ep_dir / "refs.json").write_text(json.dumps({
        "per_shot": {
            "A01": [
                {"key": f"r_{name}", "path": f"data/episodes/ep05/refs/{name}.png"}
                for name in ["duck", "rowan", "hazel", "mochi", "family", "pool", "tree", "bench", "fence"]
            ]
        }
    }))
    
    # Create actual ref files
    refs_dir = ep_dir / "refs"
    refs_dir.mkdir(exist_ok=True)
    from PIL import Image
    for ref_name in ["duck", "rowan", "hazel", "mochi", "family", "pool", "tree", "bench", "fence"]:
        img = Image.new('RGB', (64, 64), color='blue')
        img.save(refs_dir / f"{ref_name}.png")
    
    # prompt_kit.json
    (ep_dir / "prompt_kit.json").write_text('{}')
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        from hfvg.activities.preflight import PreflightError
        
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-preflight-fail'})()
        
        # Run preflight in live mode (should raise PreflightError)
        with pytest.raises(PreflightError) as exc_info:
            await preflight_validate_episode_activity(
                episode_id="ep05",
                shots_subset=None,
                is_live=True
            )
        
        # Should have error about A01 refs exceeding cap
        error_text = str(exc_info.value).lower()
        assert "a01" in error_text, f"Error should mention A01, got: {exc_info.value}"
        assert "needs 6 refs" in error_text and "allows 5" in error_text, \
            f"Error should mention ref count (6) and cap (5), got: {exc_info.value}"
        
        # Should mention it's checking against still model, not clip model
        assert "xai/grok-imagine-image-2.0" in error_text, \
            f"Error should indicate still model cap, got: {exc_info.value}"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_accepts_beatmap_md_not_txt(tmp_path, monkeypatch):
    """
    Test: Preflight requires BEATMAP.md (workflow file), not beatmap.txt.
    Verifies the bug fix where preflight was checking wrong filename.
    """
    from hfvg.activities.preflight import preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    ep_dir = tmp_path / "data" / "episodes" / "ep99"
    ep_dir.mkdir(parents=True, exist_ok=True)
    
    # Only create BEATMAP.md (NOT beatmap.txt)
    (ep_dir / "BEATMAP.md").write_text("# Ep99\n## A01\nTest shot")
    
    # Minimal other files
    (ep_dir / "shots.json").write_text(json.dumps({
        "shots": [{"id": "A01", "prompt": "Test", "count": 1, "model": "kling", 
                   "duration": 3, "ref_keys": []}]
    }))
    (ep_dir / "refs.json").write_text('{"per_shot": {"A01": []}}')
    (ep_dir / "prompt_kit.json").write_text('{}')
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-beatmap-md'})()
        
        # Should pass with BEATMAP.md
        result = await preflight_validate_episode_activity(
            episode_id="ep99",
            shots_subset=None,
            is_live=True
        )
        
        assert result["status"] == "passed", \
            f"Preflight should accept BEATMAP.md, got: {result}"
        
    finally:
        temporalio.activity.info = original_info
