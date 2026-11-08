"""Test that GET /api/episodes/{id}/shots returns beat fields when available from workflow state."""

import asyncio
import json
import os
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_api_shots_includes_beat_fields_from_workflow_state(tmp_path, monkeypatch):
    """
    Verify that GET /api/episodes/{id}/shots merges beat fields from workflow state.
    """
    db_path = tmp_path / "test_studio.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["ADMIN_SECRET"] = "test-secret-32-chars-minimum-length-required"
    
    from hfvg.studio_db import init_studio_db
    from fastapi.testclient import TestClient
    import aiosqlite
    
    # Initialize database
    await init_studio_db(str(db_path))
    
    # Create a shot in the database
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
    from api import main as api_main
    
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
    
    # Store original client and restore after test
    original_client = api_main.temporal_client
    api_main.temporal_client = mock_client
    monkeypatch.setattr(api_main, "temporal_client", mock_client)
    
    client = TestClient(api_main.app)
    
    # Query shots API
    response = client.get(
        f"/api/episodes/{episode_id}/shots",
        headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
    )
    assert response.status_code == 200
    
    data = response.json()
    shots = data["shots"]
    assert len(shots) == 1
    
    shot = shots[0]
    assert shot["shot_id"] == "A01"
    assert shot["beat_id"] == "beat_1"
    assert shot["beat_purpose"] == "Setup: Duck discovers adventure"
    assert shot["beat_emotional_tone"] == "curiosity"
    
    # Cleanup
    api_main.temporal_client = original_client


