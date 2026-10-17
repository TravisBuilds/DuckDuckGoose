"""Test episode audio activities in workflow context."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from hfvg.workflows.episode import EpisodeWorkflow
from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music


# Fake activities for dry-run test
@activity.defn(name="read_file")
async def fake_read_file(path: str) -> str:
    """Fake read_file activity."""
    return "fake script content"


@activity.defn(name="generate_script")
async def fake_generate_script(episode_id: str, outline: str) -> str:
    """Fake generate_script activity."""
    return "fake://script.txt"


@activity.defn(name="ingest_assets")
async def fake_ingest_assets(episode_id: str, assets: list) -> dict:
    """Fake ingest_assets activity."""
    return {"status": "completed"}


@activity.defn(name="render_shots")
async def fake_render_shots(script_path: str) -> str:
    """Fake render_shots activity."""
    return "fake://shots.mp4"


@activity.defn(name="trim_silence")
async def fake_trim_silence(video_url: str) -> str:
    """Fake trim_silence activity."""
    return "fake://trimmed.mp4"


@activity.defn(name="render_edit")
async def fake_render_edit(video_url: str, edit_params: dict) -> str:
    """Fake render_edit activity."""
    return "fake://edit.mp4"


@activity.defn(name="mix_audio")
async def fake_mix_audio(video_url: str, audio_urls: list) -> str:
    """Fake mix_audio activity."""
    return "fake://final.mp4"


@pytest.mark.asyncio
async def test_episode_audio_dry_run(tmp_path, monkeypatch):
    """Test EpisodeWorkflow runs through audio stage with real audio activities in dry-run mode."""
    # Set up environment for dry-run
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep_audio_test", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep_audio_test", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep_audio_test", "test")
    
    # Start workflow environment
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Register workflow and activities (real audio activities, fake others)
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflow],
            activities=[
                fake_read_file,
                fake_generate_script,
                fake_ingest_assets,
                fake_render_shots,
                fake_trim_silence,
                fake_render_edit,
                fake_mix_audio,
                # Real audio activities that should work in dry-run
                generate_voiceover,
                generate_sfx,
                generate_music,
            ],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["ep_audio_test", "Test outline"],
                id="test-episode-audio-workflow",
                task_queue="test-task-queue",
            )
            
            # Let workflow progress through stages (it will wait at final_approved signal)
            # We don't need to wait for completion, just verify it doesn't crash in audio stage
            
            # Query current state to verify it progressed past audio
            import asyncio
            await asyncio.sleep(2)  # Give it time to process
            
            state = await handle.query(EpisodeWorkflow.get_state)
            
            # Verify workflow progressed through audio stage without crashing
            # Stage progression: OUTLINE -> SCRIPT -> ASSETS -> SHOTS -> TRIM -> PICTURE_LOCK -> AUDIO -> FINAL_MIX
            from hfvg.models import PipelineStage
            assert state.stage in (
                PipelineStage.AUDIO,
                PipelineStage.FINAL_MIX,
                PipelineStage.COMPLETE
            ), f"Expected workflow to reach at least AUDIO stage, got {state.stage}"
            
            # Cancel the workflow since we're just testing the audio stage
            await handle.cancel()
