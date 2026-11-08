"""
Test that episodes can be started with enable_story_shaping flag.
"""
import os
import pytest
from unittest.mock import MagicMock, AsyncMock


def test_start_episode_with_story_shaping_enabled(monkeypatch):
    """Test that enable_story_shaping flag can be set when creating an episode."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    
    # Import after setting env vars
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
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
            "episode_id": "ep98",
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
        assert data["episode_id"] == "ep98"
        
        # Verify workflow was started with enable_story_shaping=True (8th arg, index 7)
        assert mock_client.start_workflow.called
        call_args = mock_client.start_workflow.call_args
        workflow_args = call_args.kwargs.get('args', [])
        assert len(workflow_args) == 8, f"Should have 8 args (including enable_story_shaping), got {len(workflow_args)}"
        assert workflow_args[7] is True, f"8th arg (enable_story_shaping) should be True, got {workflow_args[7]}"


def test_start_episode_with_story_shaping_disabled(monkeypatch):
    """Test that enable_story_shaping defaults to False."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    monkeypatch.setenv("DATABASE_PATH", ":memory:")
    
    # Import after setting env vars
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
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
            "episode_id": "ep97",
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
        assert data["episode_id"] == "ep97"
        
        # Verify workflow was started with enable_story_shaping=False (8th arg, index 7)
        assert mock_client.start_workflow.called
        call_args = mock_client.start_workflow.call_args
        workflow_args = call_args.kwargs.get('args', [])
        assert len(workflow_args) == 8, f"Should have 8 args (including enable_story_shaping), got {len(workflow_args)}"
        assert workflow_args[7] is False, f"8th arg (enable_story_shaping) should be False, got {workflow_args[7]}"
