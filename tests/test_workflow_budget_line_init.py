"""Workflow-level test: Verify budget lines are initialized before generation.

This test MUST fail if init_episode_budget_lines_activity call is removed from workflow.
"""

import pytest
import aiosqlite
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from unittest.mock import AsyncMock, patch

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg import activities
from hfvg.activities.run_lock import acquire_run_lock, release_run_lock
from hfvg.activities.credit_activities import (
    init_episode_credit_line_activity,
    init_episode_budget_lines_activity,
)
from hfvg.studio_db import init_studio_db


@pytest.mark.asyncio
async def test_workflow_creates_budget_lines_before_stills(tmp_path, monkeypatch):
    """
    Workflow-level test: EpisodeWorkflowV2 must create budget lines before stills.
    
    This test MUST FAIL if init_episode_budget_lines_activity call is removed from workflow.
    Mutation: replacing the call at ~line 169 with `budget_result = {}` breaks this test.
    """
    episode_id = "ep99"
    db_path = str(tmp_path / "test.db")
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    # Initialize DB
    await init_studio_db(db_path)
    
    # Create episode
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute("""
            INSERT INTO episodes (episode_id, beatmap_path, live_mode, g108_approved)
            VALUES (?, ?, 1, 1)
        """, (episode_id, ""))
        await db.commit()
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Mock activities that aren't relevant to budget line creation
        from temporalio import activity
        
        @activity.defn(name="parse_beatmap_activity")
        async def mock_beatmap(*args):
            return {
                "shots": [{
                    "shot_id": "A01",
                    "prompt": "Test shot",
                    "count": 1,
                    "characters": ["D"],
                    "wet": False,
                    "model": "kling",
                    "duration": 3,
                    "end_frame": False,
                    "ref_keys": []
                }]
            }
        
        @activity.defn(name="load_refs_to_generate_activity")
        async def mock_refs(*args):
            return []
        
        @activity.defn(name="preflight_validate_episode_activity")
        async def mock_preflight(*args):
            return {"status": "passed", "episode_id": episode_id, "validated_shots": 1, 
                    "available_refs": 0, "to_generate_refs": 0}
        
        @activity.defn(name="upsert_episode_activity")
        async def mock_upsert(*args):
            return {"ok": True}
        
        @activity.defn(name="verify_episode_files_activity")
        async def mock_verify(*args):
            return {"passed": True}
        
        # Mock still submission to not actually submit
        @activity.defn(name="submit_still_job_enforced")
        async def mock_submit_still(*args, **kwargs):
            # This is where budget reserve happens - we want it to succeed
            # to prove budget lines were created
            return {"job_id": "still-test", "line_name": "L2_drafts", "reserved_amount": 100_000}
        
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[
                activities.load_gate_policy_activity,
                mock_beatmap,
                mock_preflight,
                mock_upsert,
                mock_verify,
                activities.approve_g108_activity,
                init_episode_credit_line_activity,
                init_episode_budget_lines_activity,  # CRITICAL: workflow must call this
                mock_refs,
                mock_submit_still,
                acquire_run_lock,
                release_run_lock,
            ],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=[episode_id, None, False, None, 500],  # credit_cap=500
                id=f"test-{episode_id}",
                task_queue="test-queue",
            )
            
            # Wait for workflow to reach Gate 1 (budget lines created, preflight passed)
            # Then approve G1 so it proceeds to stills
            import asyncio
            await asyncio.sleep(0.1)  # Let workflow initialize
            
            # Check that budget lines were created (MUTATION TEST: fails if init call removed)
            async with aiosqlite.connect(db_path, uri=True) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM budget_lines WHERE episode_id = ?",
                    (episode_id,)
                ) as cur:
                    count = (await cur.fetchone())[0]
            
            # ASSERTION 1: Budget lines must exist
            # If init_episode_budget_lines_activity call is removed from workflow, this FAILS
            assert count > 0, (
                f"Budget lines not created! Expected >0, got {count}. "
                f"Workflow must call init_episode_budget_lines_activity before generation."
            )
            
            # ASSERTION 2: At least L7_revision should be created (always created by policy)
            async with aiosqlite.connect(db_path, uri=True) as db:
                async with db.execute(
                    "SELECT line_name FROM budget_lines WHERE episode_id = ?",
                    (episode_id,)
                ) as cur:
                    lines = [row[0] async for row in cur]
            
            assert "L7_revision" in lines, (
                f"L7_revision line not found. Got lines: {lines}. "
                f"Workflow must call init_episode_budget_lines_activity."
            )
            
            # Approve G1 to proceed to stills (would trigger still generation if we let it run)
            await handle.signal(EpisodeWorkflowV2.approve_shot_plan)
            
            # Wait a bit for it to start stills phase
            await asyncio.sleep(0.2)
            
            # Terminate workflow (we've verified budget lines exist)
            await handle.cancel()
            
            # ASSERTION 3: Verify budget lines are usable (were initialized correctly)
            # This tests that the lines have proper cap/stop values
            from hfvg.budget import BudgetLedger
            ledger = BudgetLedger(db_path)
            
            for line_name in lines:
                status = await ledger.get_line_status(episode_id, line_name)
                assert status["cap"] > 0, f"Line {line_name} has no cap (not initialized properly)"
                assert status["stop"] > 0, f"Line {line_name} has no stop (not initialized properly)"
