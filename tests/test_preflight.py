"""Tests for preflight validation."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_preflight_catches_missing_prompt_kit(tmp_path, monkeypatch):
    """Test: Preflight catches missing prompt_kit.json before any spend."""
    from hfvg.activities.preflight import preflight_validate_episode_activity, PreflightError
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # Create minimal valid inputs except prompt_kit.json
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "A01": {
            "prompt": "Test shot",
            "params": {"model": "kling", "duration": 3, "aspect_ratio": "9:16"}
        }
    }))
    (base_path / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    
    # Missing prompt_kit.json
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Should raise PreflightError with missing prompt_kit.json
        with pytest.raises(PreflightError) as exc_info:
            await preflight_validate_episode_activity(episode_id, None, False)
        
        assert "prompt_kit.json" in str(exc_info.value)
        assert len(exc_info.value.problems) >= 1
        assert any("prompt_kit.json" in p for p in exc_info.value.problems)
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_catches_missing_refs(tmp_path, monkeypatch):
    """Test: Preflight catches refs used in shots but not available or to_generate."""
    from hfvg.activities.preflight import preflight_validate_episode_activity, PreflightError
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # Shot uses r_duck but refs.json doesn't have it
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "A01": {
            "prompt": "Show r_duck in the park",
            "params": {"model": "kling", "duration": 3, "aspect_ratio": "9:16"}
        }
    }))
    (base_path / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    (base_path / "prompt_kit.json").write_text("{}")
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        with pytest.raises(PreflightError) as exc_info:
            await preflight_validate_episode_activity(episode_id, None, False)
        
        assert "r_duck" in str(exc_info.value)
        assert any("r_duck" in p for p in exc_info.value.problems)
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_passes_with_all_valid_inputs(tmp_path, monkeypatch):
    """Test: Preflight passes when all inputs are valid."""
    from hfvg.activities.preflight import preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # Create all valid inputs
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "shots": [{
            "id": "A01",
            "prompt": "Show r_duck in the park",
            "count": 1,
            "characters": ["D"],
            "wet": False,
            "model": "kling",
            "duration": 3,
            "end_frame": False,
            "ref_keys": ["r_duck"]
        }]
    }))
    
    # Create a valid ref file
    refs_dir = base_path / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    ref_path = refs_dir / "r_duck.png"
    # Create a minimal valid PNG (1x1 red pixel)
    ref_path.write_bytes(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0'
        b'\x00\x00\x00\x03\x00\x01\x00\x18\xdd\x8d\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    
    (base_path / "refs.json").write_text(json.dumps({
        "per_shot": {
            "A01": [{"key": "r_duck", "path": str(ref_path)}]
        }
    }))
    (base_path / "prompt_kit.json").write_text("{}")
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await preflight_validate_episode_activity(episode_id, None, False)
        
        assert result["status"] == "passed"
        assert result["validated_shots"] == 1
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_catches_missing_api_key_in_live_mode(tmp_path, monkeypatch):
    """Test: Preflight catches missing API key in live mode."""
    from hfvg.activities.preflight import preflight_validate_episode_activity, PreflightError
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # Create minimal valid inputs
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "A01": {
            "prompt": "Test shot",
            "params": {"model": "kling", "duration": 3, "aspect_ratio": "9:16"}
        }
    }))
    (base_path / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    (base_path / "prompt_kit.json").write_text("{}")
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)  # Remove API key
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Live mode should fail without API key
        with pytest.raises(PreflightError) as exc_info:
            await preflight_validate_episode_activity(episode_id, None, is_live=True)
        
        assert "HIGGSFIELD_API_KEY" in str(exc_info.value)
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_catches_too_many_refs_for_model(tmp_path, monkeypatch):
    """Test: Preflight fails if shot needs more refs than model allows."""
    from hfvg.activities.preflight import preflight_validate_episode_activity, PreflightError
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    import json
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # Shot needs 9 refs, exceeds still model cap of 8
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "shots": [{
            "id": "A01",
            "prompt": "Test shot with too many characters",
            "count": 9,
            "characters": ["D", "F", "R", "H", "M", "P", "T", "B", "S"],  # 9 characters
            "wet": False,
            "model": "kling",
            "duration": 3,
            "end_frame": False,
            "ref_keys": ["r_duck", "r_family", "r_rowan", "r_hazel", "r_mochi", "r_pool", "r_tree", "r_bench", "r_scene"]  # 9 refs
        }]
    }))
    (base_path / "refs.json").write_text(json.dumps({"per_shot": {"A01": []}}))
    (base_path / "prompt_kit.json").write_text('{}')
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        with pytest.raises(PreflightError) as exc_info:
            await preflight_validate_episode_activity(episode_id, None, False)
        
        # Refs are checked against still model (Grok Image 2.0 cap 8), not clip model
        assert "9" in str(exc_info.value) or "8" in str(exc_info.value)
        assert "A01" in str(exc_info.value)
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_preflight_detects_version_bump_external_refs(tmp_path, monkeypatch):
    """Test: Preflight detects version bumps when refs are in external directory."""
    from hfvg.activities.preflight import preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity
    import json
    
    episode_id = "ep99"
    base_path = tmp_path / "data" / "episodes" / episode_id
    base_path.mkdir(parents=True, exist_ok=True)
    
    # External refs directory
    external_refs = tmp_path / "external_refs"
    external_refs.mkdir(parents=True, exist_ok=True)
    
    # Create ref file in external directory
    ref_file = external_refs / "duck.png"
    ref_file.write_text("fake image")
    
    # Create version sidecar next to ref file (v1)
    sidecar_dir = external_refs / ".versions"
    sidecar_dir.mkdir(exist_ok=True)
    (sidecar_dir / "r_duck.json").write_text(json.dumps({"version": 1}))
    
    # BEATMAP and shots
    (base_path / "BEATMAP.md").write_text("A01 (2.5s)")
    (base_path / "shots.json").write_text(json.dumps({
        "shots": [{
            "id": "A01",
            "prompt": "Duck scene",
            "count": 1,
            "characters": ["D"],
            "wet": False,
            "model": "kling",
            "duration": 3,
            "end_frame": False,
            "ref_keys": ["r_duck"]
        }]
    }))
    
    # refs.json with version bump to v2
    (base_path / "refs.json").write_text(json.dumps({
        "per_shot": {
            "A01": [{
                "key": "r_duck",
                "path": str(ref_file)
            }]
        },
        "versions": {
            "r_duck": 2  # Version bumped from 1 to 2
        }
    }))
    (base_path / "prompt_kit.json").write_text('{}')
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await preflight_validate_episode_activity(episode_id, None, False)
        
        # Preflight should detect version bump and count ref as to_generate
        assert result["to_generate_refs"] == 1, f"Expected 1 to_generate_ref (version bump), got {result['to_generate_refs']}"
        assert result["available_refs"] == 1, "Should still count as available"
    
    finally:
        temporalio.activity.info = original_info
