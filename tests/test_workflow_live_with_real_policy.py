"""Test workflow with real production gate policy and HTTP-mocked provider."""

import json
import os
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_live_workflow_with_real_policy_succeeds(tmp_path, monkeypatch, respx_mock):
    """
    Test: Run 2-shot live episode with real production gate policy, provider HTTP-mocked.
    
    Verifies:
    - Budget lines are created even when policy lacks ep05 config
    - Still reserve succeeds (not "Budget line ep05:L2_drafts not found")
    - Provider HTTP endpoint is called
    
    This test must FAIL on code before init_episode_budget_lines_activity fix.
    """
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.activities import all_activities
    import httpx
    
    # Set up environment
    episode_id = "ep05"
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-1234567890123456789012345678")
    
    # Use real production gate policy
    policy_path = Path(__file__).parent.parent / "examples" / "mid-mountain-rest" / "HARNESS-GATES.json"
    monkeypatch.setenv("GATE_POLICY_PATH", str(policy_path))
    
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    await create_episode(db_path, episode_id)
    await set_live_mode(db_path, episode_id, True)
    
    # Create episode files
    ep_dir = tmp_path / "data" / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    
    # BEATMAP with 2 simple shots
    (ep_dir / "BEATMAP.md").write_text("""# Episode 5

## A01
Duck at the pool.

## A02
Duck swimming.
""")
    
    # shots.json with 2 draft stills (will use L2_drafts)
    (ep_dir / "shots.json").write_text(json.dumps({
        "shots": [
            {
                "id": "A01",
                "prompt": "Duck at the pool",
                "count": 1,
                "characters": ["D"],
                "wet": False,
                "model": "kling",
                "duration": 3,
                "end_frame": False,
                "ref_keys": []
            },
            {
                "id": "A02",
                "prompt": "Duck swimming",
                "count": 1,
                "characters": ["D"],
                "wet": True,
                "model": "kling",
                "duration": 3,
                "end_frame": False,
                "ref_keys": []
            }
        ]
    }))
    
    # refs.json (no refs needed)
    (ep_dir / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    
    # prompt_kit.json
    (ep_dir / "prompt_kit.json").write_text(json.dumps({}))
    
    # Mock Higgsfield API HTTP endpoints
    respx_mock.post("https://api.higgsfield.ai/v1/images/generation").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "gen_123",
                    "status": "in-progress"
                }
            }
        )
    )
    
    respx_mock.post("https://api.higgsfield.ai/v1/images/estimate").mock(
        return_value=httpx.Response(
            200,
            json={
                "type": "description",
                "pricing_description": "$0.08 per image"
            }
        )
    )
    
    respx_mock.get("https://api.higgsfield.ai/v1/images/generation/gen_123").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "id": "gen_123",
                    "status": "completed",
                    "result": {
                        "url": "https://storage.example.com/image.jpg"
                    }
                }
            }
        )
    )
    
    # Run workflow through Gate 1 (refs + still generation)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-harness",
            workflows=[EpisodeWorkflowV2],
            activities=all_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                episode_id,
                id=f"test-{episode_id}",
                task_queue="test-harness",
            )
            
            # Wait for workflow to reach G1 (after init and first still)
            # The workflow should:
            # 1. Init budget lines (must create L1_refs, L2_drafts, L3_final_stills, L4_video, L7_revision)
            # 2. Run preflight (must verify budget lines exist)
            # 3. Generate A01 draft still (must reserve from L2_drafts successfully)
            
            # Give it some time to process
            import asyncio
            await asyncio.sleep(2)
            
            # Check workflow status
            desc = await handle.describe()
            
            # If budget lines weren't initialized, workflow would have failed with:
            # "Budget line ep05:L2_drafts not found"
            # With fix, it should be running or completed G1
            
            # For this test, we just verify it didn't fail with budget line error
            if desc.status.name == "FAILED":
                # Get the failure details
                try:
                    await handle.result()
                except Exception as e:
                    error_msg = str(e)
                    # This error indicates the bug we're fixing
                    if "Budget line" in error_msg and "not found" in error_msg:
                        pytest.fail(
                            f"Workflow failed with budget line error (bug not fixed): {error_msg}"
                        )
                    # Some other error - let it propagate
                    raise
            
            # Verify budget lines exist in DB
            import aiosqlite
            async with aiosqlite.connect(db_path, uri=True) as db:
                async with db.execute(
                    "SELECT line_name FROM budget_lines WHERE episode_id = ? ORDER BY line_name",
                    (episode_id,)
                ) as cur:
                    lines = [row[0] async for row in cur]
            
            assert "L1_refs" in lines, "L1_refs budget line missing"
            assert "L2_drafts" in lines, "L2_drafts budget line missing"
            assert "L3_final_stills" in lines, "L3_final_stills budget line missing"
            assert "L4_video" in lines, "L4_video budget line missing"
            assert "L7_revision" in lines, "L7_revision budget line missing"
            
            # Verify HTTP mock was called (still generation reached provider)
            assert respx_mock.calls.called, "Provider HTTP endpoint not called"
