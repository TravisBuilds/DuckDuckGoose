"""Test that generated audio plans validate with load_audio_plan for all 24 presets."""
import pytest
import json
import yaml
from pathlib import Path


def get_all_preset_ids():
    """Load preset IDs from the YAML file."""
    yaml_path = Path(__file__).parent.parent / "hfvg" / "data" / "genre_presets.yaml"
    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    # YAML has top-level keys: schema_version, templates, defaults, presets
    # We want the preset IDs, which are under presets
    preset_ids = list(data.get("presets", {}).keys())
    assert len(preset_ids) == 24, f"Expected 24 presets, got {len(preset_ids)}"
    return preset_ids


@pytest.fixture
def client(monkeypatch):
    """Test client with auth."""
    # Set env vars via monkeypatch (no module-level pollution)
    monkeypatch.setenv("ADMIN_SECRET", "test_admin_secret_at_least_32_chars_long_here")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("DRY_RUN", "true")
    
    from fastapi.testclient import TestClient
    from api.main import app
    
    test_client = TestClient(app)
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    login_response = test_client.post("/api/login", json={"admin_secret": admin_secret})
    assert login_response.status_code == 200
    
    session_cookie = login_response.cookies.get("studio_admin_token")
    test_client.cookies.set("studio_admin_token", session_cookie)
    
    return test_client


@pytest.mark.parametrize("archetype_id", get_all_preset_ids())
def test_audio_plan_validates_for_all_presets(client, archetype_id, tmp_path):
    """Test that the generated audio plan for each preset validates with load_audio_plan (DRY_RUN=false semantics)."""
    # Generate a draft with fake LLM
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": f"Test story for {archetype_id}",
            "archetype_id": archetype_id,
            "target_duration": 30,
        }
    )
    
    assert response.status_code == 200, f"Draft failed for {archetype_id}: {response.text}"
    data = response.json()
    
    assert "audio_plan_json" in data, f"No audio_plan_json for {archetype_id}"
    audio_plan = data["audio_plan_json"]
    
    # Write plan to a temp file in the expected location
    episode_id = data["episode_id"]
    episode_dir = tmp_path / episode_id
    audio_dir = episode_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    
    plan_path = audio_dir / "audio_plan.json"
    plan_path.write_text(json.dumps(audio_plan))
    
    # Now try to load and validate with the real schema (normalize_plan with dry_run=False)
    from hfvg.audio_mix import normalize_plan
    
    try:
        normalized = normalize_plan(audio_plan, base_dir=episode_dir, dry_run=False)
        
        # Basic schema checks
        assert "vo" in normalized, f"Missing 'vo' in normalized plan for {archetype_id}"
        assert "lines" in normalized["vo"], f"Missing 'vo.lines' for {archetype_id}"
        
        # Music or no music depending on preset
        if normalized.get("music"):
            music = normalized["music"]
            # Must have either prompt or path
            assert music.get("prompt") or music.get("path"), \
                f"Music for {archetype_id} needs prompt or path, got {music}"
        
    except Exception as e:
        pytest.fail(f"Audio plan validation failed for preset {archetype_id}: {e}\nPlan: {json.dumps(audio_plan, indent=2)}")


def test_audio_plan_schema_structure(monkeypatch):
    """Test that the fake LLM generates the correct schema structure."""
    monkeypatch.setenv("ADMIN_SECRET", "test_admin_secret_at_least_32_chars_long_here")
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("DRY_RUN", "true")
    
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    login_response = client.post("/api/login", json={"admin_secret": admin_secret})
    session_cookie = login_response.cookies.get("studio_admin_token")
    client.cookies.set("studio_admin_token", session_cookie)
    
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "Test schema structure",
            "archetype_id": "nostalgia-time-travel",  # Has dialogue
            "target_duration": 30,
        }
    )
    
    assert response.status_code == 200
    data = response.json()
    audio_plan = data["audio_plan_json"]
    
    # Check top-level structure
    assert "vo" in audio_plan, "audio_plan must have 'vo' key"
    assert "music" in audio_plan, "audio_plan must have 'music' key"
    
    # Check vo structure
    vo = audio_plan["vo"]
    assert "voice_id" in vo, "vo must have 'voice_id'"
    assert "lines" in vo, "vo must have 'lines'"
    
    # Check music structure
    music = audio_plan["music"]
    assert "prompt" in music or "path" in music, "music must have 'prompt' or 'path'"
    assert "level_db" in music, "music must have 'level_db'"
    assert "loop" in music, "music must have 'loop'"
    
    # If there are lines, check their structure
    for line in vo["lines"]:
        assert "id" in line, "VO line must have 'id'"
        assert "at" in line, "VO line must have 'at' (timestamp)"
        assert "text" in line, "VO line must have 'text'"
        assert "subtitle" in line, "VO line must have 'subtitle'"
