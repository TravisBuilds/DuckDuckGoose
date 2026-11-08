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
    api_main.temporal_client = mock_client
    
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


@pytest.mark.asyncio
async def test_api_shots_no_beat_fields_when_story_off(tmp_path, monkeypatch):
    """
    Verify that when story shaping is disabled, beat fields are absent or null.
    """
    db_path = tmp_path / "test_studio.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["DRY_RUN"] = "true"
    os.environ["ADMIN_SECRET"] = "test-secret-32-chars-minimum-length-required"
    
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from fastapi.testclient import TestClient
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg.worker import PRODUCTION_ACTIVITIES
    
    await init_studio_db(str(db_path))
    credit_ledger = CreditLedger(str(db_path))
    await credit_ledger.init_db()
    
    episode_id = "ep98"
    episode_data_dir = tmp_path / "data" / "episodes" / episode_id
    episode_data_dir.mkdir(parents=True, exist_ok=True)
    
    beatmap_content = """# Test

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.0 (6.0) | Park | Day | D | Duck walks | Intro | - | - | K |
"""
    
    (episode_data_dir / "BEATMAP.md").write_text(beatmap_content)
    
    monkeypatch.chdir(tmp_path)
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="hfvg-tasks",
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
            activities=PRODUCTION_ACTIVITIES,
        ):
            from api import main as api_main
            api_main.temporal_client = env.client
            
            client = TestClient(api_main.app)
            
            # Start WITHOUT story shaping
            response = client.post(
                "/api/episodes",
                json={
                    "episode_id": episode_id,
                    "beatmap_content": beatmap_content,
                    "dry_run": True,
                    "enable_story_shaping": False,
                },
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200
            
            handle = env.client.get_workflow_handle(episode_id)
            
            # Wait for G1
            async def wait_for_g1():
                for _ in range(30):
                    st = await handle.query("get_state")
                    if st.get("current_gate") == "G1_SHOT_PLAN":
                        return st
                    await env.sleep(1)
                return None
            
            await wait_for_g1()
            
            # Query shots
            response = client.get(
                f"/api/episodes/{episode_id}/shots",
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200
            
            data = response.json()
            shots = data["shots"]
            
            # Shots should exist but beat fields should be absent or null
            if shots:
                shot = shots[0]
                # Fields may be present but null, or absent entirely
                assert shot.get("beat_id") is None or "beat_id" not in shot
                assert shot.get("beat_purpose") is None or "beat_purpose" not in shot
                assert shot.get("beat_emotional_tone") is None or "beat_emotional_tone" not in shot
