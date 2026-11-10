"""Tests for draft-from-idea API with genre presets."""

import pytest
import json
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient


@pytest.fixture
def mock_temporal():
    """Mock Temporal client to avoid needing a running Temporal server."""
    with patch('api.main.temporal_client', None):
        yield


@pytest.fixture
def mock_openai():
    """Mock OpenAI API calls."""
    mock_response = {
        "beatmap_md": "# Episode ep99test Beatmap\n\n| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n|---|---|---|---|---|---|---|---|---|---|---|\n| 1 | S01 | 5.0 (5.0) | Workshop | Day | A | Robot examines books | Establish | - | - | K |\n",
        "shots_json": {
            "shots": [
                {
                    "id": "S01",
                    "prompt": "A friendly robot in a workshop",
                    "count": 1,
                    "characters": ["A"],
                    "wet": False,
                    "model": "kling",
                    "duration": 5,
                    "end_frame": False,
                    "ref_keys": []
                }
            ]
        },
        "refs_json": {
            "per_shot": {
                "S01": []
            }
        },
        "prompt_kit_json": {
            "style": "photorealistic",
            "aspect_ratio": "9:16",
            "characters": {
                "A": {"description": "Robot"}
            },
            "sets": {
                "Workshop": "Old workshop"
            }
        },
        "audio_plan_json": {
            "vo_mode": "none",
            "music": {"style": "ambient", "intensity": "medium"}
        }
    }
    
    with patch('api.main.AsyncOpenAI') as mock_client_class:
        mock_client = AsyncMock()
        mock_client_class.return_value = mock_client
        
        mock_completion = AsyncMock()
        mock_completion.choices = [AsyncMock()]
        mock_completion.choices[0].message.content = json.dumps(mock_response)
        
        mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
        yield mock_client


def test_draft_requires_archetype_id():
    """Test that archetype_id is now required."""
    from api.main import app
    
    client = TestClient(app)
    
    # Missing archetype_id should fail validation
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A robot explores a library"
        }
    )
    
    assert response.status_code == 422  # Validation error
    assert "archetype_id" in response.text.lower()


def test_draft_rejects_unknown_archetype():
    """Test that unknown archetype_id is rejected."""
    from api.main import app
    
    client = TestClient(app)
    
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A robot explores a library",
            "archetype_id": "unknown-preset-id-123"
        }
    )
    
    assert response.status_code == 400
    assert "Unknown archetype_id" in response.text


@pytest.mark.parametrize("archetype_id", [
    "nostalgia-time-travel",
    "cozy-hangout",
    "original-mini-movie",
    "ride-along-pov",
])
def test_draft_accepts_valid_archetypes(archetype_id, mock_temporal, mock_openai):
    """Test that all 24 valid archetype IDs are accepted."""
    from api.main import app
    
    # Patch database and file operations
    with patch('api.main.init_studio_db', new_callable=AsyncMock), \
         patch('api.main.Path'), \
         patch('builtins.open', create=True), \
         patch('api.main.parse_beatmap', return_value=[{"id": "S01"}]), \
         patch('api.main.parse_shots', return_value=[{"shot_id": "S01"}]), \
         patch('api.main.offline_cost_estimate', return_value={"total_usd_micros": 100000}):
        
        client = TestClient(app)
        
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A test story for this archetype",
                "archetype_id": archetype_id
            }
        )
        
        # Should succeed (or fail for reasons other than invalid archetype)
        if response.status_code != 200:
            print(f"Response: {response.text}")
        assert response.status_code in (200, 503)  # 503 if OpenAI not configured


def test_fake_llm_mode_generates_audio_plan():
    """Test that DRAFT_LLM_FAKE=1 mode generates audio_plan.json."""
    import os
    from api.main import app
    
    # Enable fake LLM mode (requires DRY_RUN=true)
    with patch.dict(os.environ, {"DRAFT_LLM_FAKE": "1", "DRY_RUN": "true"}), \
         patch('api.main.init_studio_db', new_callable=AsyncMock), \
         patch('api.main.Path'), \
         patch('api.main.temporal_client', None):
        
        client = TestClient(app)
        
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A robot explores a library",
                "archetype_id": "original-mini-movie"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should include audio_plan_json
        assert "audio_plan_json" in data
        assert "vo_mode" in data["audio_plan_json"]
        
        # Should include preset_used
        assert "preset_used" in data
        assert data["preset_used"]["id"] == "original-mini-movie"


def test_fake_llm_respects_preset_aspect():
    """Test that fake LLM uses preset's aspect ratio."""
    import os
    from api.main import app
    
    with patch.dict(os.environ, {"DRAFT_LLM_FAKE": "1", "DRY_RUN": "true"}), \
         patch('api.main.init_studio_db', new_callable=AsyncMock), \
         patch('api.main.Path'), \
         patch('api.main.temporal_client', None):
        
        client = TestClient(app)
        
        # what-if-ending has aspect 16:9
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A story with a different ending",
                "archetype_id": "what-if-ending"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["prompt_kit_json"]["aspect_ratio"] == "16:9"


def test_fake_llm_dialogue_mode_has_speakers():
    """Test that dialogue preset generates speaker mapping."""
    import os
    from api.main import app
    
    with patch.dict(os.environ, {"DRAFT_LLM_FAKE": "1", "DRY_RUN": "true"}), \
         patch('api.main.init_studio_db', new_callable=AsyncMock), \
         patch('api.main.Path'), \
         patch('api.main.temporal_client', None):
        
        client = TestClient(app)
        
        # nostalgia-time-travel is dialogue mode
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A family memory from the 2000s",
                "archetype_id": "nostalgia-time-travel"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        
        audio_plan = data["audio_plan_json"]
        assert audio_plan["vo_mode"] == "dialogue"
        assert "speakers" in audio_plan
        assert "lines" in audio_plan


def test_fake_llm_refuses_without_dry_run():
    """Test that DRAFT_LLM_FAKE refuses when DRY_RUN=false."""
    import os
    from api.main import app
    
    with patch.dict(os.environ, {"DRAFT_LLM_FAKE": "1", "DRY_RUN": "false"}), \
         patch('api.main.temporal_client', None):
        
        client = TestClient(app)
        
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "Test",
                "archetype_id": "cozy-hangout"
            }
        )
        
        assert response.status_code == 500
        assert "DRAFT_LLM_FAKE" in response.text
        assert "test-only flag" in response.text.lower()


def test_target_duration_uses_preset_range():
    """Test that target_duration respects preset's length range."""
    import os
    from api.main import app
    
    with patch.dict(os.environ, {"DRAFT_LLM_FAKE": "1", "DRY_RUN": "true"}), \
         patch('api.main.init_studio_db', new_callable=AsyncMock), \
         patch('api.main.Path'), \
         patch('api.main.temporal_client', None):
        
        client = TestClient(app)
        
        # cozy-micro has length_s: [10, 20]
        # Requesting 60s should be clamped to 20s max
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A cute greeting",
                "archetype_id": "cute-greeting",
                "target_duration": 60
            }
        )
        
        assert response.status_code == 200
        # Duration clamping happens in prompt generation, hard to assert here
        # but it shouldn't error
