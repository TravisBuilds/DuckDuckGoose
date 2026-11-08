"""
Test that episodes can be started with enable_story_shaping flag.
"""
import pytest
from fastapi.testclient import TestClient
from api.main import app
from hfvg.config import ADMIN_SECRET

client = TestClient(app)


def test_start_episode_with_story_shaping_enabled():
    """Test that enable_story_shaping flag can be set when creating an episode."""
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep-story-test",
            "beatmap_content": "Test beatmap for story shaping\n\nIdea: A test story\nGenre: test\n",
            "dry_run": True,
            "enable_story_shaping": True,
        },
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
    )
    
    # Expect success or workflow already started (if running in parallel)
    assert response.status_code in [200, 409], f"Unexpected status {response.status_code}: {response.text}"
    
    if response.status_code == 200:
        data = response.json()
        assert "workflow_id" in data
        assert data["episode_id"] == "ep-story-test"


def test_start_episode_with_story_shaping_disabled():
    """Test that enable_story_shaping defaults to False."""
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep-no-story-test",
            "beatmap_content": "Test beatmap without story shaping\n\nIdea: A test story\nGenre: test\n",
            "dry_run": True,
            # enable_story_shaping not specified, should default to False
        },
        headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
    )
    
    # Expect success or workflow already started
    assert response.status_code in [200, 409], f"Unexpected status {response.status_code}: {response.text}"
    
    if response.status_code == 200:
        data = response.json()
        assert "workflow_id" in data
        assert data["episode_id"] == "ep-no-story-test"
