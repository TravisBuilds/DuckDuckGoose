"""Test episode audio activities in Temporal workflow."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from hfvg.workflows.episode import EpisodeWorkflow


# Track audio activity calls
audio_calls_tracker = []


@activity.defn(name="generate_voiceover")
async def track_voiceover(episode_id: str, script: str, voice_id: str) -> str:
    """Track voiceover call with episode_id."""
    audio_calls_tracker.append(("voiceover", episode_id))
    return f"dry-run://vo-{episode_id}.mp3"


@activity.defn(name="generate_sfx")
async def track_sfx(episode_id: str, description: str) -> str:
    """Track SFX call with episode_id."""
    audio_calls_tracker.append(("sfx", episode_id))
    return f"dry-run://sfx-{episode_id}.mp3"


@activity.defn(name="generate_music")
async def track_music(episode_id: str, style: str, duration: float) -> str:
    """Track music call with episode_id."""
    audio_calls_tracker.append(("music", episode_id))
    return f"dry-run://music-{episode_id}.mp3"


# Mock all other activities
@activity.defn(name="read_file")
async def mock_read_file(path: str) -> str:
    return "mock content"


@activity.defn(name="generate_script")
async def mock_generate_script(episode_id: str, outline: str) -> str:
    return "dry-run://script.txt"


@activity.defn(name="ingest_assets")
async def mock_ingest_assets(episode_id: str, assets: list) -> dict:
    return {"status": "completed"}


@activity.defn(name="render_shots")
async def mock_render_shots(script_path: str) -> str:
    return "dry-run://shots.mp4"


@activity.defn(name="trim_clips")
async def mock_trim_clips(clip_urls: list, params: dict) -> str:
    return "dry-run://trimmed.mp4"


@activity.defn(name="render_edit")
async def mock_render_edit(video_url: str, edit_params: dict) -> str:
    return "dry-run://edit.mp4"


@activity.defn(name="mix_audio")
async def mock_mix_audio(video_url: str, audio_urls: list) -> str:
    return "dry-run://final.mp4"


def test_audio_activities_signature():
    """Test that audio activities have episode_id as first parameter."""
    import inspect
    from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music
    
    sig = inspect.signature(generate_voiceover)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"voiceover: expected episode_id first, got {params[0]}"
    
    sig = inspect.signature(generate_sfx)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"sfx: expected episode_id first, got {params[0]}"
    
    sig = inspect.signature(generate_music)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"music: expected episode_id first, got {params[0]}"


@pytest.mark.asyncio
async def test_episode_audio_calls_with_episode_id(tmp_path, monkeypatch):
    """Test EpisodeWorkflow calls audio activities with episode_id as first arg through Temporal."""
    global audio_calls_tracker
    audio_calls_tracker = []
    
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep_audio", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep_audio", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep_audio", "test")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-audio",
            workflows=[EpisodeWorkflow],
            activities=[
                mock_read_file, mock_generate_script, mock_ingest_assets,
                mock_render_shots, mock_trim_clips, mock_render_edit, mock_mix_audio,
                track_voiceover, track_sfx, track_music,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["ep_audio", "Test idea", ["instagram"]],
                id="test-audio-workflow",
                task_queue="test-audio",
            )
            
            # Send signals to progress workflow through to audio stage
            import asyncio
            await asyncio.sleep(0.1)
            await handle.signal("readback_approved")
            await asyncio.sleep(0.1)
            await handle.signal("character_locks_approved")
            await asyncio.sleep(0.1)
            await handle.signal("storyboard_approved")
            
            # Wait for workflow to reach audio stage and call activities
            await asyncio.sleep(2)
            
            # Cancel to stop before final_approved wait
            await handle.cancel()
            
            # Verify audio activities were called with episode_id
            assert len(audio_calls_tracker) >= 3, f"Expected 3 audio calls, got {len(audio_calls_tracker)}: {audio_calls_tracker}"
            
            for call_type, episode_id in audio_calls_tracker:
                assert episode_id == "ep_audio", f"{call_type} called with wrong episode_id: {episode_id}"
