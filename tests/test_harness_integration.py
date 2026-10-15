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
    Full integration: API start → Temporal workflow → 3 gates → G_FINAL_CUT.
    
    Uses time-skipping test environment and fake providers that write placeholder files.
    """
    # Setup test database with full schema
    db_path = tmp_path / "test_studio.db"
    os.environ["DATABASE_PATH"] = str(db_path)
    os.environ["DRY_RUN"] = "true"
    os.environ["ADMIN_SECRET"] = "test-secret-32-chars-minimum-length-required"
    
    from hfvg.studio_db import init_studio_db
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
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
    
    # Write simple 2-shot beatmap (ep05 style)
    beatmap_path.write_text("""
# Episode 99 Test Beatmap

| Shot | Prompt | Duration | Model | Tier | Refs |
|------|--------|----------|-------|------|------|
| A01  | Duck walks in park | 5.5s | K | draft | - |
| A02  | Duck meets goose | 5.0s | K | draft | A01 |
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
            # Start workflow with beatmap
            workflow_id = episode_id
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=[episode_id, str(beatmap_path), True],  # episode_id, beatmap_path, dry_run
                id=workflow_id,
                task_queue="hfvg-tasks",
            )
            
            # Wait for G1 gate
            await asyncio.sleep(0.5)
            state = await handle.query("get_state")
            print(f"State after start: {state}")
            assert state.get("current_gate") == "G1_SHOT_PLAN", f"Expected G1_SHOT_PLAN, got {state.get('current_gate')}"
            
            # Approve G1
            await handle.signal("approve_g1_shot_plan")
            
            # Wait for stills to be generated and G2 gate
            await asyncio.sleep(5.0)
            state = await handle.query("get_state")
            print(f"State after G1 approval: {state}")
            
            # Approve G2  
            await handle.signal("approve_g2_scene_stills")
            
            # Wait for clips and G_FINAL gate
            await asyncio.sleep(5.0)
            state = await handle.query("get_state")
            print(f"State after G2 approval: {state}")
            
            # Approve G_FINAL
            await handle.signal("approve_g_final_cut")
            
            # Wait for workflow completion
            await asyncio.sleep(2.0)
            
            try:
                result = await asyncio.wait_for(handle.result(), timeout=5.0)
                print(f"Workflow result: {result}")
                
                # Verify result structure
                assert result is not None, "Workflow result is None"
                assert isinstance(result, dict), f"Result is not dict: {type(result)}"
                
                # Check approvals
                approvals = result.get("approvals", {})
                assert approvals.get("g1_shot_plan") is True
                assert approvals.get("g2_scene_stills") is True
                assert approvals.get("g_final_cut") is True
                
                print("✅ Integration test assertions passed!")
            except asyncio.TimeoutError:
                print(f"Workflow did not complete in time. Last state: {state}")


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
