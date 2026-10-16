"""Test episode_v2.py failed_shots handling."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import workflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow


@workflow.defn
class FakeFailedShot:
    """Fake shot workflow that returns failed status."""
    
    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """Return a failed result with provider detail."""
        return {
            "shot_id": shot_plan["shot_id"],
            "status": "failed",
            "error": "422 PROVIDER_DETAIL_XYZ"
        }


@workflow.defn
class FakeSuccessShot:
    """Fake shot workflow that succeeds."""
    
    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """Return a success result."""
        return {
            "shot_id": shot_plan["shot_id"],
            "status": "completed",
            "still_url": "http://example.com/still.jpg",
            "clip_url": "http://example.com/clip.mp4"
        }


@pytest.mark.asyncio
async def test_episode_v2_failed_shot_records_provider_detail(tmp_path, monkeypatch):
    """Test that a shot returning {'status':'failed','error':'...'} ends up in failed_shots."""
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
            workflows=[EpisodeWorkflowV2, FakeFailedShot],
            activities=[],
        ):
            # Patch ShotWorkflow to FakeFailedShot
            original_shot_workflow = EpisodeWorkflowV2._shot_workflow_cls
            EpisodeWorkflowV2._shot_workflow_cls = FakeFailedShot
            
            try:
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
                
                # Verify failed shot is recorded with exact provider detail
                assert len(state["failed_shots"]) == 1
                failed = state["failed_shots"][0]
                assert failed["shot_id"] == "A01"
                assert failed["status"] == "failed"
                assert failed["reason"] == "422 PROVIDER_DETAIL_XYZ"
            finally:
                EpisodeWorkflowV2._shot_workflow_cls = original_shot_workflow


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
            workflows=[EpisodeWorkflowV2, FakeSuccessShot],
            activities=[],
        ):
            # Patch ShotWorkflow to FakeSuccessShot
            original_shot_workflow = EpisodeWorkflowV2._shot_workflow_cls
            EpisodeWorkflowV2._shot_workflow_cls = FakeSuccessShot
            
            try:
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
            finally:
                EpisodeWorkflowV2._shot_workflow_cls = original_shot_workflow
