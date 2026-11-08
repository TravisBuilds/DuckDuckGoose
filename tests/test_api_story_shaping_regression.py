"""Regression test for story shaping positional argument bug.

Verifies that API-started episodes with enable_story_shaping=False reach G1.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_api_episode_with_story_shaping_false_reaches_g1(tmp_path, monkeypatch):
    """
    Regression test: API-started episode with enable_story_shaping=False must reach G1.
    
    Root cause: api/main.py was passing enable_story_shaping as 7th arg,
    but EpisodeWorkflowV2.run expects reopen_from as 7th and enable_story_shaping as 8th.
    Result: reopen_from=False triggered _run_reopened path, hanging before G1.
    
    Fix: Pass None for reopen_from before enable_story_shaping flag.
    """
    # Setup environment
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
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.temporal_converter import temporal_data_converter
    
    # Initialize databases
    await init_studio_db(str(db_path))
    credit_ledger = CreditLedger(str(db_path))
    await credit_ledger.init_db()
    
    # Create episode files
    episode_id = "ep99"
    episode_data_dir = tmp_path / "data" / "episodes" / episode_id
    episode_data_dir.mkdir(parents=True, exist_ok=True)
    
    beatmap_content = """# Episode 99 Test

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.0 (6.0) | Park | Day | D | Duck walks | Intro | - | - | K |
"""
    
    (episode_data_dir / "BEATMAP.md").write_text(beatmap_content)
    (episode_data_dir / "shots.json").write_text(json.dumps({
        "shots": [{
            "id": "A01",
            "prompt": "Duck walks in park",
            "count": 1,
            "characters": ["D"],
            "wet": False,
            "model": "kling",
            "duration": 6,
            "end_frame": False,
            "ref_keys": []
        }]
    }))
    (episode_data_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": []}}))
    (episode_data_dir / "prompt_kit.json").write_text('{}')
    
    monkeypatch.chdir(tmp_path)
    
    # Start Temporal environment
    async with await WorkflowEnvironment.start_time_skipping(data_converter=temporal_data_converter) as env:
        async with Worker(
            env.client,
            task_queue="hfvg-tasks",
            workflows=[EpisodeWorkflowV2],
            activities=PRODUCTION_ACTIVITIES,
        ):
            from api import main as api_main
            api_main.temporal_client = env.client
            
            client = TestClient(api_main.app)
            
            # Start episode with enable_story_shaping=False (the critical case)
            response = client.post(
                "/api/episodes",
                json={
                    "episode_id": episode_id,
                    "beatmap_content": beatmap_content,
                    "dry_run": True,
                    "enable_story_shaping": False,  # Must be False to trigger the bug
                },
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200, f"Failed to start episode: {response.text}"
            
            # Get workflow handle
            handle = env.client.get_workflow_handle(episode_id)
            
            # Wait for G1 (if the bug is present, this will hang/timeout)
            async def wait_for_g1(budget_s=10.0):
                loop = asyncio.get_running_loop()
                end = loop.time() + budget_s
                while loop.time() < end:
                    st = await handle.query("get_state")
                    if st.get("current_gate") == "G1_SHOT_PLAN":
                        return st
                    await env.sleep(1)
                return None
            
            state = await wait_for_g1()
            assert state is not None, "Workflow did not reach G1 within timeout (likely hung in reopen path)"
            assert state.get("current_gate") == "G1_SHOT_PLAN", f"Expected G1, got {state.get('current_gate')}"
