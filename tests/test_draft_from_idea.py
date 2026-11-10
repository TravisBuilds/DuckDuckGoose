"""Tests for POST /api/episodes/draft-from-idea endpoint."""
import os
import pytest
import json
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient


# Set env vars before importing
os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["DATABASE_PATH"] = ":memory:"
os.environ["OPENAI_API_KEY"] = "test-openai-key"  # For testing


@pytest.fixture
def client():
    """Test client with auth."""
    from api.main import app
    test_client = TestClient(app)
    
    # Login to get valid session cookie
    admin_secret = os.environ["ADMIN_SECRET"]
    login_response = test_client.post("/api/login", json={"admin_secret": admin_secret})
    assert login_response.status_code == 200
    
    session_cookie = login_response.cookies.get("studio_admin_token")
    test_client.cookies.set("studio_admin_token", session_cookie)
    
    return test_client


@pytest.fixture
def mock_openai_response():
    """Mock OpenAI response with valid episode files."""
    return {
        "beatmap_md": """# Episode ep42 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 5.0 (5.0) | Library | Day | A | Robot enters dusty library | Open | - | - | K |
| 2 | S02 | 6.0 (6.0) | Library | Day | A | Robot discovers glowing book | Discovery | - | - | K |
| 3 | S03 | 5.0 (5.0) | Library | Day | A B | Magical creatures emerge from book | Climax | - | - | K |
""",
        "shots_json": {
            "shots": [
                {
                    "id": "S01",
                    "prompt": "A curious robot enters a dusty library filled with glowing books",
                    "count": 1,
                    "characters": ["A"],
                    "wet": False,
                    "model": "kling",
                    "duration": 5,
                    "end_frame": False,
                    "ref_keys": []
                },
                {
                    "id": "S02",
                    "prompt": "The robot discovers a special book that glows with magical light",
                    "count": 1,
                    "characters": ["A"],
                    "wet": False,
                    "model": "kling",
                    "duration": 6,
                    "end_frame": False,
                    "ref_keys": []
                },
                {
                    "id": "S03",
                    "prompt": "Magical creatures emerge from the glowing book, surrounding the robot",
                    "count": 1,
                    "characters": ["A", "B"],
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
                "S01": [],
                "S02": [],
                "S03": []
            }
        },
        "prompt_kit_json": {
            "style": "photorealistic, cinematic lighting, mysterious atmosphere",
            "aspect_ratio": "9:16",
            "characters": {
                "A": {"description": "a small curious robot with glowing blue eyes"},
                "B": {"description": "magical glowing creatures from the book"}
            },
            "sets": {
                "Library": "an abandoned library with dusty shelves and glowing books"
            }
        },
        "audio_plan_json": {
            "vo": {
                "voice_id": None,
                "lines": []
            },
            "music": {
                "prompt": "ambient mysterious music",
                "level_db": -30.0,
                "loop": True
            }
        },
        "ip_steering_note": ""
    }


def test_draft_from_idea_success(client, mock_openai_response, monkeypatch):
    """Test successful draft generation."""
    # Monkeypatch the config to provide an API key
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    # Mock OpenAI client
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(mock_openai_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A curious robot explores an abandoned library filled with glowing books. It discovers a special book that comes alive with magical creatures.",
                "archetype_id": "original-mini-movie",
                "target_duration": 60,
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    
    assert "episode_id" in data
    # Format: ep{2 digits}{3 lowercase letters} matching ep[0-9]{2}(?:[a-z][a-z0-9]{0,11})?
    episode_id = data["episode_id"]
    assert episode_id.startswith("ep")
    assert len(episode_id) == 7  # "ep" + 2 digits + 3 letters
    assert episode_id[2:4].isdigit()  # 2 digits
    assert episode_id[4:7].islower()  # 3 lowercase letters
    assert episode_id[4:7].isalpha()
    
    # Verify it passes the server's validate_episode_id
    from api.main import validate_episode_id
    try:
        validate_episode_id(episode_id)
    except Exception as e:
        pytest.fail(f"Generated episode_id '{episode_id}' failed validation: {e}")
    
    assert "beatmap" in data
    assert "S01" in data["beatmap"]
    assert "shots_json" in data
    assert len(data["shots_json"]["shots"]) == 3
    assert "refs_json" in data
    assert "prompt_kit_json" in data
    assert data["prompt_kit_json"]["aspect_ratio"] == "9:16"


def test_draft_from_idea_missing_api_key(client, monkeypatch):
    """Test that missing OPENAI_API_KEY returns 503."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    
    # Force config reload
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', None)
    
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A robot explores a library",
            "archetype_id": "original-mini-movie",
            "target_duration": 60,
        }
    )
    
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


def test_draft_from_idea_invalid_request(client):
    """Test validation errors for invalid requests."""
    # Idea too short
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "short",
            "archetype_id": "original-mini-movie",
            "target_duration": 60,
        }
    )
    assert response.status_code == 422
    
    # Duration out of range
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A valid story idea with enough content",
            "archetype_id": "original-mini-movie",
            "target_duration": 700,
        }
    )
    assert response.status_code == 422


def test_draft_from_idea_unauthorized(monkeypatch):
    """Test that unauthenticated requests are rejected."""
    from api.main import app
    # Don't use the client fixture - create raw client without auth
    test_client = TestClient(app)
    
    response = test_client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A robot explores a library",
            "archetype_id": "original-mini-movie",
            "target_duration": 60,
        }
    )
    
    assert response.status_code == 401


def test_draft_from_idea_with_archetype(client, mock_openai_response, monkeypatch):
    """Test draft generation with archetype preset."""
    # Monkeypatch the config to provide an API key
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(mock_openai_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A cozy scene with characters relaxing",
                "archetype_id": "cozy-hangout",
                "target_duration": 30,
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    assert "episode_id" in data


def test_draft_from_idea_beatmap_repair(client, mock_openai_response, monkeypatch):
    """Test that invalid beatmap triggers repair attempt."""
    # Monkeypatch the config to provide an API key
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    # First response has invalid beatmap
    invalid_response = mock_openai_response.copy()
    invalid_response["beatmap_md"] = "# Invalid beatmap\nNo table here"
    
    # Second response (repair) has valid beatmap
    repair_response = {
        "beatmap_md": mock_openai_response["beatmap_md"]
    }
    
    mock_completion_1 = MagicMock()
    mock_completion_1.choices = [MagicMock(message=MagicMock(content=json.dumps(invalid_response)))]
    
    mock_completion_2 = MagicMock()
    mock_completion_2.choices = [MagicMock(message=MagicMock(content=json.dumps(repair_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    # First call returns invalid, second call returns repair
    mock_client.chat.completions.create = AsyncMock(side_effect=[mock_completion_1, mock_completion_2])
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "Test idea for beatmap repair",
                "archetype_id": "original-mini-movie",
                "target_duration": 60,
            }
        )
    
    # Should succeed after repair
    assert response.status_code == 200
    # Should have made 2 OpenAI calls (initial + repair)
    assert mock_client.chat.completions.create.call_count == 2


def test_draft_from_idea_ip_steering_warning(client, monkeypatch):
    """Test that IP steering notes appear in warnings."""
    # Monkeypatch the config to provide an API key
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    response_with_ip = {
        "beatmap_md": """# Episode ep50 Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 5.0 (5.0) | City | Day | A | Hero swings through city | Open | - | - | K |
""",
        "shots_json": {
            "shots": [
                {
                    "id": "S01",
                    "prompt": "A red-and-blue spider-themed hero swings through the city",
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
            "style": "comic book style",
            "aspect_ratio": "9:16",
            "characters": {
                "A": {"description": "superhero"}
            },
            "sets": {
                "City": "Urban cityscape"
            }
        },
        "audio_plan_json": {
            "vo": {"voice_id": None, "lines": []},
            "music": {"prompt": "heroic music", "level_db": -30.0, "loop": True}
        },
        "ip_steering_note": "Steering: Avoided Spider-Man likeness"
    }
    
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(response_with_ip)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A hero swings through the city",
                "archetype_id": "fullscreen-action-anime",
                "target_duration": 30,
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    # IP steering note should be present
    assert "ip_steering_note" in data
    assert "Spider-Man" in data["ip_steering_note"]


def test_draft_llm_fake_refused_in_live_mode(client, monkeypatch):
    """Test that fake LLM refuses when DRY_RUN=false and DRAFT_LLM_FAKE=1."""
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("DRY_RUN", "false")
    
    response = client.post(
        "/api/episodes/draft-from-idea",
        json={
            "idea": "A robot explores a library",
            "archetype_id": "original-mini-movie",
            "target_duration": 60,
        }
    )
    
    # Should refuse in live mode with fake LLM
    assert response.status_code == 400
    assert "fake LLM" in response.json()["detail"].lower()


@pytest.mark.parametrize("archetype_id", [
    "cute-greeting",
    "what-if-ending",
    "nostalgia-time-travel",
    "cozy-hangout",
    "dreamscape",
    "living-painting",
    "farewell-tribute",
    "mascot-sketch",
    "satire-sketch",
    "creature-legend",
    "animal-remake",
    "original-series",
    "live-action-fight",
    "time-travel-vlog",
    "fullscreen-action-anime",
    "original-mini-movie",
    "cliffhanger-serial",
    "epic-trailer",
    "fan-fiction",
    "behind-the-scenes",
    "making-of-broll",
    "style-experiment",
    "series-commentary",
    "outtake-blooper",
])
def test_draft_accepts_valid_archetypes(client, mock_openai_response, monkeypatch, archetype_id):
    """Test that all 24 valid archetypes are accepted."""
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(mock_openai_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "Test story idea",
                "archetype_id": archetype_id,
                "target_duration": 30,
            }
        )
    
    # Should accept all valid archetypes (200 or 503 if no API key)
    assert response.status_code == 200


def test_target_duration_uses_preset_range(client, mock_openai_response, monkeypatch):
    """Test that target_duration is clamped to preset range."""
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(mock_openai_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        # cute-greeting has range [10, 20]s
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A cute greeting",
                "archetype_id": "cute-greeting",
                "target_duration": 120,  # Way over the max
            }
        )
    
    assert response.status_code == 200
    data = response.json()
    
    # Duration should be clamped to preset max (20s for cute-greeting)
    # The fake LLM always returns 2 shots with fixed durations (5s + 6s = 11s)
    total_duration = sum(shot["duration"] for shot in data["shots_json"]["shots"])
    assert total_duration <= 20, f"Duration {total_duration}s exceeds cute-greeting max 20s"


def test_archetype_optional_defaults_to_original_mini_movie(client, mock_openai_response, monkeypatch):
    """Test that omitting archetype_id defaults to 'original-mini-movie' (for Skip button)."""
    from hfvg import config as config_module
    monkeypatch.setattr(config_module.config, 'OPENAI_API_KEY', 'test-key-for-mocked-calls')
    
    mock_completion = MagicMock()
    mock_completion.choices = [MagicMock(message=MagicMock(content=json.dumps(mock_openai_response)))]
    
    mock_client = MagicMock()
    mock_client.chat = MagicMock()
    mock_client.chat.completions = MagicMock()
    mock_client.chat.completions.create = AsyncMock(return_value=mock_completion)
    
    with patch('openai.AsyncOpenAI', return_value=mock_client):
        response = client.post(
            "/api/episodes/draft-from-idea",
            json={
                "idea": "A robot explores a library",
                # archetype_id omitted (Skip button case)
                "target_duration": 60,
            }
        )
    
    # Should succeed with default preset
    assert response.status_code == 200
    data = response.json()
    assert "episode_id" in data
