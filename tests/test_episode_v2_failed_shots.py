"""Test episode_v2.py failed_shots handling."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow
from hfvg import activities


@pytest.mark.asyncio
async def test_episode_v2_failed_shot_records_provider_detail(tmp_path, monkeypatch):
    """Test that a shot returning {'status':'failed','error':'...'} ends up in failed_shots."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep99", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep99", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep99", "test")
    
    # Set up budget
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep99", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    # Set up credits
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create beatmap
    import json
    beatmap_dir = tmp_path / "data" / "episodes" / "ep99"
    beatmap_dir.mkdir(parents=True)
    (beatmap_dir / "BEATMAP.md").write_text("## A01\nDuration: 1 second\nDuck waddles in.\n")
    (beatmap_dir / "shots.json").write_text(json.dumps({
        "A01": {"prompt": "Test shot", "refs": []}
    }))
    
    # Mock submit_still activity to return error with provider detail
    @activity.defn(name="submit_still_job_enforced")
    async def mock_submit_still(*args, **kwargs):
        # Return dict simulating 422 provider error
        raise ValueError("422 PROVIDER_DETAIL_XYZ")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
            activities=[
                mock_submit_still,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.load_shot_plans,
                activities.load_refs_to_generate,
                activities.generate_ref,
                activities.recheck_and_update_shot_plans,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99"],
                id="test-episode-failed-shot",
                task_queue="test-task-queue",
            )
            
            # Wait for workflow to complete
            result = await handle.result()
            
            # Query state
            state = await handle.query(EpisodeWorkflowV2.get_state)
            
            # Verify failed shot is recorded with provider detail
            assert len(state["failed_shots"]) == 1
            failed = state["failed_shots"][0]
            assert failed["shot_id"] == "A01"
            assert failed["status"] == "failed"
            assert "422 PROVIDER_DETAIL_XYZ" in failed["reason"]


@pytest.mark.asyncio
async def test_episode_v2_success_shot_not_in_failed_shots(tmp_path, monkeypatch):
    """Test that a successful shot does not appear in failed_shots."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep99", None)
    
    # Create beatmap
    import json
    beatmap_dir = tmp_path / "data" / "episodes" / "ep99"
    beatmap_dir.mkdir(parents=True)
    (beatmap_dir / "BEATMAP.md").write_text("## A01\nDuration: 1 second\nDuck waddles in.\n")
    (beatmap_dir / "shots.json").write_text(json.dumps({
        "A01": {"prompt": "Test shot", "refs": []}
    }))
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
            activities=[
                activities.submit_still_job,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.load_shot_plans,
                activities.load_refs_to_generate,
                activities.generate_ref,
                activities.recheck_and_update_shot_plans,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99"],
                id="test-episode-success-shot",
                task_queue="test-task-queue",
            )
            
            # Wait for workflow to complete
            result = await handle.result()
            
            # Query state
            state = await handle.query(EpisodeWorkflowV2.get_state)
            
            # Verify no failed shots
            assert len(state["failed_shots"]) == 0
