"""Test that members (non-admin) can complete the wizard upload steps (D8)."""
import os
import pytest
import json
from pathlib import Path
from io import BytesIO


# Set env vars before importing
os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["DATABASE_PATH"] = ":memory:"


@pytest.fixture
async def setup_db(tmp_path, monkeypatch):
    """Setup test database with admin and member users."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    episodes_dir = tmp_path / "data" / "episodes"
    episodes_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EPISODES_DIR", str(episodes_dir))
    
    from hfvg.users import UserManager
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    # Create admin and member users
    admin = await user_mgr.create_user(
        name="Admin User",
        email="admin@test.com",
        role="admin",
        password="admin123"
    )
    
    member = await user_mgr.create_user(
        name="Member User",
        email="member@test.com",
        role="member",
        password="member123"
    )
    
    yield path, admin, member, episodes_dir


def get_client(email: str, password: str):
    """Get authenticated test client."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    response = client.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200, f"Login failed: {response.text}"
    
    return client


@pytest.mark.asyncio
async def test_member_can_upload_shots(setup_db):
    """Test that a member can upload shots for their episode."""
    db_path, admin, member, episodes_dir = setup_db
    
    client = get_client("member@test.com", "member123")
    
    # Create mock beatmap and refs
    beatmap_content = """# Episode ep01test Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 5.0 (5.0) | Workshop | Day | A | Robot examines books | Establish | - | - | K |
"""
    
    shots_json = {
        "shots": [
            {
                "id": "S01",
                "prompt": "Robot examines books",
                "count": 1,
                "characters": ["A"],
                "wet": False,
                "model": "kling",
                "duration": 5,
                "end_frame": False,
                "ref_keys": []
            }
        ]
    }
    
    refs_json = {"per_shot": {"S01": []}}
    
    # Upload shots (multipart form data)
    response = client.post(
        "/api/episodes/ep01test/upload-shots",
        files={
            "shots_file": ("shots.json", BytesIO(json.dumps(shots_json).encode()), "application/json"),
            "refs_file": ("refs.json", BytesIO(json.dumps(refs_json).encode()), "application/json"),
        },
    )
    
    # Member should be allowed (D8 fix)
    assert response.status_code == 200, f"Member upload-shots failed: {response.text}"
    data = response.json()
    assert data["success"] is True


@pytest.mark.asyncio
async def test_member_can_upload_prompt_kit(setup_db):
    """Test that a member can upload prompt_kit for their episode."""
    db_path, admin, member, episodes_dir = setup_db
    
    client = get_client("member@test.com", "member123")
    
    prompt_kit = {
        "style": "photorealistic",
        "aspect_ratio": "9:16",
        "characters": {
            "A": {"description": "Robot"}
        },
        "sets": {
            "Workshop": "Old workshop"
        }
    }
    
    response = client.post(
        "/api/episodes/ep01test/upload-prompt-kit",
        files={
            "prompt_kit_file": ("prompt_kit.json", BytesIO(json.dumps(prompt_kit).encode()), "application/json"),
        },
    )
    
    # Member should be allowed (D8 fix)
    assert response.status_code == 200, f"Member upload-prompt-kit failed: {response.text}"
    data = response.json()
    assert data["success"] is True


@pytest.mark.asyncio
async def test_member_can_upload_audio_plan(setup_db):
    """Test that a member can upload audio_plan for their episode."""
    db_path, admin, member, episodes_dir = setup_db
    
    client = get_client("member@test.com", "member123")
    
    audio_plan = {
        "vo": {
            "voice_id": None,
            "lines": []
        },
        "music": {
            "prompt": "ambient music",
            "level_db": -30.0,
            "loop": True
        }
    }
    
    response = client.post(
        "/api/episodes/ep01test/upload-audio-plan",
        files={
            "audio_plan_file": ("audio_plan.json", BytesIO(json.dumps(audio_plan).encode()), "application/json"),
        },
    )
    
    # Member should be allowed (D8 fix)
    assert response.status_code == 200, f"Member upload-audio-plan failed: {response.text}"
    data = response.json()
    assert data["success"] is True


@pytest.mark.asyncio
async def test_unauthenticated_cannot_upload(setup_db):
    """Test that unauthenticated requests are rejected."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    db_path, admin, member, episodes_dir = setup_db
    
    # No login
    client = TestClient(app)
    
    audio_plan = {
        "vo": {"voice_id": None, "lines": []},
        "music": {"prompt": "test", "level_db": -30.0, "loop": True}
    }
    
    response = client.post(
        "/api/episodes/ep01test/upload-audio-plan",
        files={
            "audio_plan_file": ("audio_plan.json", BytesIO(json.dumps(audio_plan).encode()), "application/json"),
        },
    )
    
    # Should get 401
    assert response.status_code == 401
