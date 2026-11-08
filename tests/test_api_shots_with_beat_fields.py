"""API tests for beat fields in /api/episodes/{id}/shots endpoint."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta
import aiosqlite
import json
import sys

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.models import Act, StoryBeat, StoryShape
from hfvg.activities.story_shaping import (
    generate_story_shape_activity,
    critique_story_shape_activity,
    revise_story_shape_activity,
    assign_shots_to_beats_activity,
    set_test_stub_story_shape,
)
from hfvg.activities import parse_beatmap_activity


@pytest.fixture(autouse=True)
def cleanup_api_module():
    """Cleanup api.main module after each test to prevent state leakage."""
    # Save original state
    original_module = sys.modules.get('api.main')
    
    yield
    
    # Teardown: restore to original state
    if original_module is None:
        # Module wasn't imported before test, so remove it
        sys.modules.pop('api.main', None)
    else:
        # Module existed before test, so restore it
        sys.modules['api.main'] = original_module


@pytest.mark.asyncio
async def test_shots_endpoint_includes_beat_fields_from_workflow(tmp_path, monkeypatch):
    """Test that GET /api/episodes/{id}/shots merges beat fields from workflow state."""
    # Ensure api.main is reloaded with new environment
    if 'api.main' in sys.modules:
        del sys.modules['api.main']
    
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
        "shot_beats": {
            "A01": {
                "beat_id": "beat_1",
                "beat_purpose": "Setup: Duck discovers adventure",
                "beat_emotional_tone": "curiosity"
            }
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
    # Ensure api.main is reloaded with new environment
    if 'api.main' in sys.modules:
        del sys.modules['api.main']
    
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
    # Ensure api.main is reloaded with new environment
    if 'api.main' in sys.modules:
        del sys.modules['api.main']
    
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
        "shot_beats": {}  # Empty when story shaping disabled
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


@pytest.mark.asyncio
async def test_real_workflow_exposes_shot_beats_in_get_state():
    """
    Integration test: Run real EpisodeWorkflowV2 with story shaping,
    query get_state, and verify shot_beats are correctly exposed.
    """
    # Minimal story shape with beat-to-shot assignments
    test_story_shape = StoryShape(
        logline="A duck's quick adventure",
        hook="Duck finds magic",
        hook_deadline_s=5.0,
        midpoint_beat_id="beat_1",
        payoff="Duck learns courage",
        acts=[
            Act(act_number=1, description="Setup", target_duration_s=30.0, beat_ids=["beat_1"]),
        ],
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Setup: Duck discovers adventure",
                emotional_tone="curiosity",
                target_duration_s=30.0,
                shot_ids=["A01", "A02"],
            ),
        ],
    )
    
    # Stub activities for minimal workflow execution
    @activity.defn(name="acquire_run_lock")
    async def stub_acquire_run_lock(*args):
        return {"acquired": True}
    
    @activity.defn(name="release_run_lock")
    async def stub_release_run_lock(*args):
        return {"released": True}
    
    @activity.defn(name="upsert_episode_activity")
    async def stub_upsert_episode(*args):
        return {}
    
    @activity.defn(name="init_episode_credit_line_activity")
    async def stub_init_credit_line(*args):
        return {}
    
    @activity.defn(name="init_episode_budget_lines_activity")
    async def stub_init_budget_lines(*args):
        return {"lines": ["L1_refs", "L2_drafts", "L3_final_stills", "L4_video", "L7_revision"]}
    
    @activity.defn(name="load_gate_policy_activity")
    async def stub_load_gate_policy(*args):
        return {
            "gates": {},
            "duck_casting": {},
        }
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        set_test_stub_story_shape(test_story_shape)
        
        try:
            async with Worker(
                env.client,
                task_queue="test-shot-beats",
                workflows=[EpisodeWorkflowV2],
                activities=[
                    generate_story_shape_activity,
                    critique_story_shape_activity,
                    revise_story_shape_activity,
                    assign_shots_to_beats_activity,
                    parse_beatmap_activity,
                    stub_acquire_run_lock,
                    stub_release_run_lock,
                    stub_upsert_episode,
                    stub_init_credit_line,
                    stub_init_budget_lines,
                    stub_load_gate_policy,
                ],
            ):
                handle = await env.client.start_workflow(
                    EpisodeWorkflowV2.run,
                    args=["ep-beat-test", None, True, None, None, None, None, True],  # enable_story_shaping=True
                    id="ep-beat-test-shot-beats",
                    task_queue="test-shot-beats",
                )
                
                # Wait for story shaping to generate
                await env.sleep(5)
                
                # Approve story shape to proceed to shot assignment
                await handle.execute_update("approve_story_shape")
                
                # Wait for shot assignment to complete
                await env.sleep(5)
                
                # Query the REAL get_state
                state = await handle.query("get_state")
                
                # Assert shot_beats is present and populated
                assert "shot_beats" in state, "shot_beats key missing from get_state"
                shot_beats = state["shot_beats"]
                
                assert isinstance(shot_beats, dict), "shot_beats must be a dict"
                
                # Verify shot_beats contains our test shots with beat context
                assert "A01" in shot_beats, "Shot A01 should be in shot_beats"
                assert "A02" in shot_beats, "Shot A02 should be in shot_beats"
                
                # Verify beat fields for A01
                a01_beats = shot_beats["A01"]
                assert a01_beats["beat_id"] == "beat_1"
                assert a01_beats["beat_purpose"] == "Setup: Duck discovers adventure"
                assert a01_beats["beat_emotional_tone"] == "curiosity"
                
                # Verify beat fields for A02
                a02_beats = shot_beats["A02"]
                assert a02_beats["beat_id"] == "beat_1"
                assert a02_beats["beat_purpose"] == "Setup: Duck discovers adventure"
                assert a02_beats["beat_emotional_tone"] == "curiosity"
                
        finally:
            set_test_stub_story_shape(None)
