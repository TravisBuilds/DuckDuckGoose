"""
Test that episodes can be started with enable_story_shaping flag.
"""
import os
import pytest
from unittest.mock import MagicMock, AsyncMock
from fastapi.testclient import TestClient
from api.main import app
import api.main as api_main


def test_start_episode_with_story_shaping_enabled(monkeypatch):
    """Test that enable_story_shaping flag can be set when creating an episode."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    
    # Mock temporal client to avoid 503 errors
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.id = "test-workflow-story"
    mock_handle.result_run_id = "test-run-id"
    mock_client.start_workflow = AsyncMock(return_value=mock_handle)
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep-story-test",
            "beatmap_content": "Test beatmap for story shaping\n\nIdea: A test story\nGenre: test\n",
            "dry_run": True,
            "enable_story_shaping": True,
        },
        headers={"Authorization": f"Bearer {admin_secret}"},
    )
    
    # Expect success or workflow already started (if running in parallel)
    assert response.status_code in [200, 409], f"Unexpected status {response.status_code}: {response.text}"
    
    if response.status_code == 200:
        data = response.json()
        assert "workflow_id" in data
        assert data["episode_id"] == "ep-story-test"
        
        # Verify workflow was started with enable_story_shaping=True
        assert mock_client.start_workflow.called
        call_args = mock_client.start_workflow.call_args
        # Check that the 7th argument (index 6) is True
        assert len(call_args.args) >= 7, "Should have 7 args including enable_story_shaping"
        assert call_args.args[6] is True, "enable_story_shaping should be True"


def test_start_episode_with_story_shaping_disabled(monkeypatch):
    """Test that enable_story_shaping defaults to False."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    
    # Mock temporal client
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.id = "test-workflow-no-story"
    mock_handle.result_run_id = "test-run-id"
    mock_client.start_workflow = AsyncMock(return_value=mock_handle)
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep-no-story-test",
            "beatmap_content": "Test beatmap without story shaping\n\nIdea: A test story\nGenre: test\n",
            "dry_run": True,
            # enable_story_shaping not specified, should default to False
        },
        headers={"Authorization": f"Bearer {admin_secret}"},
    )
    
    # Expect success or workflow already started
    assert response.status_code in [200, 409], f"Unexpected status {response.status_code}: {response.text}"
    
    if response.status_code == 200:
        data = response.json()
        assert "workflow_id" in data
        assert data["episode_id"] == "ep-no-story-test"
        
        # Verify workflow was started with enable_story_shaping=False (default)
        assert mock_client.start_workflow.called
        call_args = mock_client.start_workflow.call_args
        # Check that the 7th argument (index 6) is False
        assert len(call_args.args) >= 7, "Should have 7 args including enable_story_shaping"
        assert call_args.args[6] is False, "enable_story_shaping should default to False"
