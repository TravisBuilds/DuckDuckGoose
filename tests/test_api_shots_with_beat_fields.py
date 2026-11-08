"""API tests for beat fields in /api/episodes/{id}/shots endpoint."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import aiosqlite
import json


@pytest.mark.asyncio
async def test_shots_endpoint_includes_beat_fields_from_workflow(tmp_path, monkeypatch):
    """Test that GET /api/episodes/{id}/shots merges beat fields from workflow state."""
    admin_secret = "test-secret-32-chars-minimum-length-required"
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    # Setup database
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    # Insert test episode and shot
    episode_id = "ep99"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO episodes (episode_id, beatmap_path) VALUES (?, ?)",
            (episode_id, "/fake/path")
        )
        await db.execute(
            """INSERT INTO shots (id, episode_id, shot_id, status, prompt, retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{episode_id}-A01", episode_id, "A01", "completed", "Duck walks", 0)
        )
        await db.commit()
    
    # Mock temporal client to return workflow state with beat fields
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.query = AsyncMock(return_value={
        "current_gate": "G2_SCENE_STILLS",
        "parsed_beatmap": {
            "shots": [
                {
                    "shot_id": "A01",
                    "beat_id": "beat_1",
                    "beat_purpose": "Setup: Duck discovers adventure",
                    "beat_emotional_tone": "curiosity"
                }
            ]
        }
    })
    mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
    
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
    # Use patch.object as context manager for proper isolation
    with patch.object(api_main, "temporal_client", mock_client):
        client = TestClient(app)
        
        response = client.get(
            f"/api/episodes/{episode_id}/shots",
            headers={"Authorization": f"Bearer {admin_secret}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "shots" in data
        shots = data["shots"]
        assert len(shots) == 1
        
        shot = shots[0]
        assert shot["shot_id"] == "A01"
        assert shot["prompt"] == "Duck walks"
        # Verify beat fields are merged from workflow state
        assert shot["beat_id"] == "beat_1"
        assert shot["beat_purpose"] == "Setup: Duck discovers adventure"
        assert shot["beat_emotional_tone"] == "curiosity"


@pytest.mark.asyncio
async def test_shots_endpoint_without_beat_fields_when_workflow_unavailable(tmp_path, monkeypatch):
    """Test that GET /api/episodes/{id}/shots works when workflow is not running."""
    admin_secret = "test-secret-32-chars-minimum-length-required"
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    episode_id = "ep98"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO episodes (episode_id, beatmap_path) VALUES (?, ?)",
            (episode_id, "/fake/path")
        )
        await db.execute(
            """INSERT INTO shots (id, episode_id, shot_id, status, prompt, retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{episode_id}-B01", episode_id, "B01", "completed", "Goose appears", 0)
        )
        await db.commit()
    
    # Mock temporal client that raises exception (workflow not found)
    mock_client = MagicMock()
    mock_client.get_workflow_handle = MagicMock(side_effect=Exception("Workflow not found"))
    
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
    with patch.object(api_main, "temporal_client", mock_client):
        client = TestClient(app)
        
        response = client.get(
            f"/api/episodes/{episode_id}/shots",
            headers={"Authorization": f"Bearer {admin_secret}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        shots = data["shots"]
        assert len(shots) == 1
        
        shot = shots[0]
        assert shot["shot_id"] == "B01"
        assert shot["prompt"] == "Goose appears"
        # Beat fields should be absent or None when workflow unavailable
        assert shot.get("beat_id") is None
        assert shot.get("beat_purpose") is None
        assert shot.get("beat_emotional_tone") is None


@pytest.mark.asyncio
async def test_shots_endpoint_without_beat_fields_in_parsed_beatmap(tmp_path, monkeypatch):
    """Test shots endpoint when workflow state exists but has no beat fields (story shaping off)."""
    admin_secret = "test-secret-32-chars-minimum-length-required"
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(db_path))
    
    episode_id = "ep97"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(
            "INSERT INTO episodes (episode_id, beatmap_path) VALUES (?, ?)",
            (episode_id, "/fake/path")
        )
        await db.execute(
            """INSERT INTO shots (id, episode_id, shot_id, status, prompt, retries)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"{episode_id}-C01", episode_id, "C01", "completed", "Duck flies", 0)
        )
        await db.commit()
    
    # Mock workflow state without beat fields (story shaping disabled)
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.query = AsyncMock(return_value={
        "current_gate": "G1_SHOT_PLAN",
        "parsed_beatmap": {
            "shots": [
                {
                    "shot_id": "C01",
                    # No beat_id, beat_purpose, or beat_emotional_tone
                }
            ]
        }
    })
    mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
    
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
    with patch.object(api_main, "temporal_client", mock_client):
        client = TestClient(app)
        
        response = client.get(
            f"/api/episodes/{episode_id}/shots",
            headers={"Authorization": f"Bearer {admin_secret}"}
        )
        
        assert response.status_code == 200
        data = response.json()
        shots = data["shots"]
        assert len(shots) == 1
        
        shot = shots[0]
        assert shot["shot_id"] == "C01"
        # Beat fields should not be present when not in workflow state
        assert "beat_id" not in shot or shot.get("beat_id") is None
        assert "beat_purpose" not in shot or shot.get("beat_purpose") is None
        assert "beat_emotional_tone" not in shot or shot.get("beat_emotional_tone") is None
