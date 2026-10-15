"""
Integration test: API → Temporal → Activities → G_FINAL_CUT with real placeholder files.

This test verifies the full stack end-to-end:
1. POST /api/episodes to start workflow
2. Workflow executes through 3 gates (G1, G2, G_FINAL)
3. Activities create real placeholder files on disk
4. Gate approvals advance workflow to completion
5. All tests run with --disable-socket (no real API calls)
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

# Set ADMIN_SECRET before importing api.main
os.environ["ADMIN_SECRET"] = "test-secret-32-chars-minimum-length-required"

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow
from hfvg.worker import PRODUCTION_ACTIVITIES


@pytest.mark.asyncio
@pytest.mark.disable_socket
async def test_api_to_g_final_cut_integration(tmp_path):
    """
    Full integration: API → Temporal → Activities → G_FINAL_CUT via FastAPI TestClient.
    
    Tests the complete flow:
    1. POST /api/episodes to start workflow
    2. POST /api/episodes/{id}/approve for G1, G2, G_FINAL gates
    3. Verify still and clip files exist on disk
    4. Verify run report JSON + MD files are written
    """
    # Setup test database and environment
    db_path = tmp_path / "test_studio.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["DRY_RUN"] = "true"
    os.environ["ADMIN_SECRET"] = "test-secret-32-chars-minimum-length-required"
    
    from hfvg.studio_db import init_studio_db
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from fastapi.testclient import TestClient
    
    # Initialize all database tables
    await init_studio_db(str(db_path))
    
    # Initialize budget ledger
    ledger = BudgetLedger(str(db_path))
    await ledger.init_db()
    
    # Initialize credit ledger
    credit_ledger = CreditLedger(str(db_path))
    await credit_ledger.init_db()
    
    # Create beatmap file for test
    episode_id = "ep99"
    beatmap_dir = tmp_path / "episodes" / episode_id
    beatmap_dir.mkdir(parents=True, exist_ok=True)
    beatmap_path = beatmap_dir / "BEATMAP.md"
    
    # Write simple 2-shot beatmap
    beatmap_path.write_text("""
# Episode 99 Test Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.5 (6.0) | Park | Day | D | Duck walks in park | Intro | - | - | K |
| 2 | A02 | 5.0 (6.0) | Park | Day | D G | Duck meets goose | Meeting | - | - | K |
""")
    
    # Create time-skipping Temporal environment
    async with await WorkflowEnvironment.start_time_skipping() as env:
        
        # Start worker with workflows and activities (using production activity list)
        async with Worker(
            env.client,
            task_queue="hfvg-tasks",
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
            activities=PRODUCTION_ACTIVITIES,
        ):
            # Import api.main after setting up environment
            from api import main as api_main
            
            # Inject test Temporal client into API
            api_main.temporal_client = env.client
            
            # Create TestClient
            client = TestClient(api_main.app)
            
            # Step 1: Start episode via API
            response = client.post(
                f"/api/episodes",
                json={
                    "episode_id": episode_id,
                    "beatmap_content": beatmap_path.read_text(),
                    "dry_run": True,
                },
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200, f"Failed to start episode: {response.text}"
            print(f"✓ Episode {episode_id} started")
            
            # Wait for G1 gate
            await asyncio.sleep(1.0)
            
            # Step 2: Approve G1_SHOT_PLAN
            response = client.post(
                f"/api/episodes/{episode_id}/approve",
                json={"gate_id": "g1_shot_plan"},
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200, f"Failed to approve G1: {response.text}"
            print(f"✓ G1_SHOT_PLAN approved")
            
            # Wait for stills generation and G2 gate
            await asyncio.sleep(5.0)
            
            # Step 3: Approve G2_SCENE_STILLS
            response = client.post(
                f"/api/episodes/{episode_id}/approve",
                json={"gate_id": "g2_scene_stills"},
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200, f"Failed to approve G2: {response.text}"
            print(f"✓ G2_SCENE_STILLS approved")
            
            # Wait for clips generation and G_FINAL gate
            await asyncio.sleep(5.0)
            
            # Step 4: Approve G_FINAL_CUT
            response = client.post(
                f"/api/episodes/{episode_id}/approve",
                json={"gate_id": "g_final_cut"},
                headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"},
            )
            assert response.status_code == 200, f"Failed to approve G_FINAL: {response.text}"
            print(f"✓ G_FINAL_CUT approved")
            
            # Wait for workflow completion
            await asyncio.sleep(2.0)
            
            # Get workflow handle and result (FAIL on timeout - no catch)
            handle = env.client.get_workflow_handle(episode_id)
            result = await asyncio.wait_for(handle.result(), timeout=10.0)
            
            print(f"Workflow result: {result}")
            
            # Verify result structure
            assert result is not None, "Workflow result is None"
            assert isinstance(result, dict), f"Result is not dict: {type(result)}"
            
            # Check approvals
            approvals = result.get("approvals", {})
            assert approvals.get("g1_shot_plan") is True
            assert approvals.get("g2_scene_stills") is True
            assert approvals.get("g_final_cut") is True
            
            # Step 5: Assert files exist on disk
            dry_run_dir = Path("./data/dry_run_outputs")
            assert dry_run_dir.exists(), f"Dry-run output directory not found: {dry_run_dir}"
            
            # Check for still files (2 shots × 1 still each)
            still_files = list(dry_run_dir.glob(f"{episode_id}_*_*.jpg"))
            assert len(still_files) >= 2, f"Expected at least 2 still files, found {len(still_files)}: {still_files}"
            print(f"✓ Found {len(still_files)} still files")
            
            # Check for clip files (2 shots × 1 clip each)
            clip_files = list(dry_run_dir.glob(f"{episode_id}_*_*.mp4"))
            assert len(clip_files) >= 2, f"Expected at least 2 clip files, found {len(clip_files)}: {clip_files}"
            print(f"✓ Found {len(clip_files)} clip files")
            
            # Step 6: Assert run report exists
            reports_dir = Path("./data/reports")
            report_json = reports_dir / f"{episode_id}.json"
            report_md = reports_dir / f"{episode_id}.md"
            
            assert report_json.exists(), f"Run report JSON not found: {report_json}"
            assert report_md.exists(), f"Run report MD not found: {report_md}"
            print(f"✓ Run report files exist: {report_json}, {report_md}")
            
            # Verify report content
            report_data = json.loads(report_json.read_text())
            assert report_data["episode_id"] == episode_id
            assert report_data["status"] == "complete"
            assert report_data["shots_count"] == 2
            
            print("✅ Integration test passed: All gates, files, and report verified!")


@pytest.mark.asyncio
@pytest.mark.disable_socket
async def test_multi_animal_routing_enforcement(tmp_path):
    """Test that multi-animal shots (count > 1) are forced to Kling."""
    from hfvg.routing import plan_route, RoutingError
    
    # Single animal: can use Seedance
    single_shot = {
        "shot_id": "A01",
        "model": "seedance",
        "tier": "draft",
        "duration": 5.0,
        "count": 1,
    }
    route = plan_route(single_shot)
    assert route.model == "seedance"
    
    # Multi-animal: must use Kling
    multi_shot = {
        "shot_id": "A02",
        "model": "kling",
        "tier": "draft",
        "duration": 5.0,
        "count": 2,
    }
    route = plan_route(multi_shot)
    assert route.model == "kling"
    
    # Multi-animal with Seedance: should fail
    bad_shot = {
        "shot_id": "A03",
        "model": "seedance",
        "tier": "draft",
        "duration": 5.0,
        "count": 2,
    }
    with pytest.raises(RoutingError, match="Multi-animal.*Kling"):
        plan_route(bad_shot)
    
    # S-marked multi-animal shot (from parser): should fail routing to Seedance
    s_multi_shot = {
        "shot_id": "B01",
        "model": "S",  # Parser uses short codes
        "tier": "draft",
        "duration": 5.0,
        "count": 2,
        "characters": {"D", "G"},  # Duck and Goose = 2 animals
    }
    # Convert S to seedance for routing
    test_shot = s_multi_shot.copy()
    test_shot["model"] = "seedance" if test_shot["model"] == "S" else test_shot["model"]
    with pytest.raises(RoutingError, match="Multi-animal.*Kling"):
        plan_route(test_shot)


@pytest.mark.asyncio
@pytest.mark.disable_socket
async def test_credit_ledger_reserve_commit(tmp_path):
    """Test credit ledger reserve and commit flow."""
    from hfvg.credit_ledger import CreditLedger
    
    db_path = tmp_path / "credit_test.db"
    ledger = CreditLedger(db_path=str(db_path))
    
    # init_db creates tables but doesn't create episode line
    await ledger.init_db()
    
    # Create episode line
    episode_id = "ep99"
    await ledger.create_episode_line(episode_id, 500)
    
    # Reserve credits
    job_id = "still-ep99-A01-v1"
    result = await ledger.reserve_credits(episode_id, job_id, 10, "Test still")
    assert result["reserved"] is True
    
    # Commit credits
    await ledger.commit_credits(episode_id, job_id, 9)  # actual cost < reserved
    
    # Check balance
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT spent_credits FROM credit_lines WHERE episode_id = ?",
            (episode_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == 9
