"""API tests for story shaping endpoints."""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch

from api.main import app
from hfvg.models import StoryBeat, StoryShape


@pytest.fixture
def mock_temporal_client():
    """Mock Temporal client for API tests."""
    client = MagicMock()
    workflow_info = MagicMock()
    workflow_info.id = "ep-test-01"
    
    async def mock_list_workflows(query):
        async def async_gen():
            yield workflow_info
        return async_gen()
    
    client.list_workflows = mock_list_workflows
    return client


@pytest.fixture
def test_story_shape():
    """Create a test story shape."""
    return StoryShape(
        logline="A duck's adventure",
        hook="Duck discovers mystery",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Setup",
                emotional_tone="curiosity",
                target_duration_s=30.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Conflict",
                emotional_tone="tension",
                target_duration_s=60.0,
                shot_ids=["A02", "A03"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Resolution",
                emotional_tone="joy",
                target_duration_s=30.0,
                shot_ids=["A04"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="Mystery resolved",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )


def test_get_story_shape(mock_temporal_client, test_story_shape):
    """Test GET /api/episodes/{id}/story-shape endpoint."""
    with patch("api.main.temporal_client", mock_temporal_client):
        handle = MagicMock()
        
        async def mock_query(query_name):
            return {
                "story_shape": test_story_shape.model_dump(),
                "story_shape_critique": {
                    "score": 85,
                    "issues": [],
                    "passes": True,
                },
                "story_shape_revision": None,
                "story_shape_log": [{"action": "generated"}],
                "approved_story_shape": False,
            }
        
        handle.query = mock_query
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.get(
                "/api/episodes/ep-test-01/story-shape",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
            )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["episode_id"] == "ep-test-01"
        assert data["story_shape"] is not None
        assert data["story_shape"]["logline"] == test_story_shape.logline
        assert data["critique"]["score"] == 85
        assert data["approved"] is False


def test_approve_story_shape(mock_temporal_client):
    """Test POST /api/episodes/{id}/approve with story_shape gate_id."""
    with patch("api.main.temporal_client", mock_temporal_client):
        handle = MagicMock()
        
        async def mock_execute_update(update_name, *args):
            return {
                "status": "approved",
                "logline": "A duck's adventure",
                "score": 85,
            }
        
        handle.execute_update = mock_execute_update
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.post(
                "/api/episodes/ep-test-01/approve",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
                json={"gate_id": "story_shape", "note": "Looks good"},
            )
        
        assert response.status_code == 200
        data = response.json()
        
        assert data["success"] is True
        assert data["gate_id"] == "story_shape"
        assert data["result"]["status"] == "approved"


def test_approve_story_shape_without_auth():
    """Test that story shape approval requires authentication."""
    client = TestClient(app)
    response = client.post(
        "/api/episodes/ep-test-01/approve",
        json={"gate_id": "story_shape"},
    )
    
    assert response.status_code == 401


def test_get_story_shape_not_found(mock_temporal_client):
    """Test GET story shape for non-existent episode."""
    with patch("api.main.temporal_client", mock_temporal_client):
        async def mock_list_workflows(query):
            async def async_gen():
                return
                yield  # Empty generator
            return async_gen()
        
        mock_temporal_client.list_workflows = mock_list_workflows
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.get(
                "/api/episodes/ep-nonexistent/story-shape",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
            )
        
        assert response.status_code == 404


def test_approve_story_shape_validator_fails(mock_temporal_client):
    """Test that story shape approval can be refused by validator."""
    from temporalio.exceptions import ApplicationError
    
    with patch("api.main.temporal_client", mock_temporal_client):
        handle = MagicMock()
        
        async def mock_execute_update(update_name, *args):
            raise ApplicationError(
                "Cannot approve story shape: story shape not generated yet",
                non_retryable=True,
            )
        
        handle.execute_update = mock_execute_update
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.post(
                "/api/episodes/ep-test-01/approve",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
                json={"gate_id": "story_shape"},
            )
        
        assert response.status_code == 409
        assert "story shape" in response.json()["detail"].lower()


def test_approve_story_shape_with_story_alias(mock_temporal_client):
    """Test that 'story' alias maps correctly to approve_story_shape method."""
    with patch("api.main.temporal_client", mock_temporal_client):
        handle = MagicMock()
        
        async def mock_execute_update(update_name, *args):
            assert update_name == "approve_story_shape", f"Expected approve_story_shape, got {update_name}"
            return {
                "status": "approved",
                "logline": "Test story",
                "score": 85,
            }
        
        handle.execute_update = mock_execute_update
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.post(
                "/api/episodes/ep-test-01/approve",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
                json={"gate_id": "story"},  # Using 'story' alias
            )
        
        assert response.status_code == 200


def test_update_story_shape(mock_temporal_client, test_story_shape):
    """Test PUT /api/episodes/{id}/story-shape endpoint."""
    with patch("api.main.temporal_client", mock_temporal_client):
        handle = MagicMock()
        
        async def mock_execute_update(update_name, story_dict):
            return {
                "story_shape": story_dict,
                "critique": {"score": 90, "passes": True},
                "needs_revision": False,
            }
        
        handle.execute_update = mock_execute_update
        mock_temporal_client.get_workflow_handle = MagicMock(return_value=handle)
        
        with patch("api.main.ADMIN_SECRET", "test-secret-32-chars-minimum-length-required"):
            client = TestClient(app)
            response = client.put(
                "/api/episodes/ep-test-01/story-shape",
                headers={"Authorization": "Bearer test-secret-32-chars-minimum-length-required"},
                json=test_story_shape.model_dump(),
            )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["critique"]["score"] == 90
