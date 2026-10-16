"""Test episode.py audio stage in dry-run."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from hfvg.workflows.episode import EpisodeWorkflow
from hfvg import activities


@pytest.mark.asyncio
async def test_episode_workflow_audio_stage_dry_run(tmp_path, monkeypatch):
    """Test that episode workflow can reach audio stage in dry-run without TypeError."""
    import os
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(tmp_path / "test.db"))
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflow],
            activities=[
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["ep99"],
                id="test-episode-audio",
                task_queue="test-task-queue",
            )
            
            # Wait for workflow to complete (should succeed in dry-run)
            result = await handle.result()
            
            # Verify it completed
            assert "final_video" in result or "status" in result
