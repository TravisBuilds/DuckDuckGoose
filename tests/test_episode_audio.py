"""Test episode audio activities in workflow context."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio import activity
from hfvg.workflows.episode import EpisodeWorkflow


# Track which audio activities were called
audio_calls = []


# Real audio activities wrapped to track calls
@activity.defn(name="generate_voiceover")
async def tracked_generate_voiceover(episode_id: str, script: str, voice_id: str) -> str:
    """Track voiceover call."""
    audio_calls.append(("voiceover", episode_id, script, voice_id))
    return f"dry-run://voiceover-{episode_id}.mp3"


@activity.defn(name="generate_sfx")
async def tracked_generate_sfx(episode_id: str, description: str) -> str:
    """Track SFX call."""
    audio_calls.append(("sfx", episode_id, description))
    return f"dry-run://sfx-{episode_id}.mp3"


@activity.defn(name="generate_music")
async def tracked_generate_music(episode_id: str, style: str, duration: float) -> str:
    """Track music call."""
    audio_calls.append(("music", episode_id, style, duration))
    return f"dry-run://music-{episode_id}.mp3"


# Mock other activities needed by EpisodeWorkflow
@activity.defn(name="read_file")
async def mock_read_file(path: str) -> str:
    return "Mock script content"


@activity.defn(name="generate_script")
async def mock_generate_script(episode_id: str, outline: str) -> str:
    return "dry-run://script.txt"


@activity.defn(name="ingest_assets")
async def mock_ingest_assets(episode_id: str, assets: list) -> dict:
    return {"status": "completed"}


@activity.defn(name="render_shots")
async def mock_render_shots(script_path: str) -> str:
    return "dry-run://shots.mp4"


@activity.defn(name="trim_silence")
async def mock_trim_silence(video_url: str) -> str:
    return "dry-run://trimmed.mp4"


@activity.defn(name="render_edit")
async def mock_render_edit(video_url: str, edit_params: dict) -> str:
    return "dry-run://edit.mp4"


@activity.defn(name="mix_audio")
async def mock_mix_audio(video_url: str, audio_urls: list) -> str:
    return "dry-run://final.mp4"


@pytest.mark.asyncio
async def test_episode_audio_workflow_calls(tmp_path, monkeypatch):
    """Test EpisodeWorkflow calls audio activities with episode_id as first arg."""
    global audio_calls
    audio_calls = []
    
    # Set up environment
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "1")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep_audio_test", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep_audio_test", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep_audio_test", "test")
    
    # Start workflow environment with time skipping
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-audio-queue",
            workflows=[EpisodeWorkflow],
            activities=[
                mock_read_file,
                mock_generate_script,
                mock_ingest_assets,
                mock_render_shots,
                mock_trim_silence,
                mock_render_edit,
                mock_mix_audio,
                tracked_generate_voiceover,
                tracked_generate_sfx,
                tracked_generate_music,
            ],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["ep_audio_test", "Test episode outline"],
                id="test-episode-audio",
                task_queue="test-audio-queue",
            )
            
            # Give workflow time to progress through stages
            import asyncio
            await asyncio.sleep(5)
            
            # Cancel since we're just testing the audio stage
            await handle.cancel()
            
            # Verify audio activities were called with episode_id as first arg
            assert len(audio_calls) >= 3, f"Expected at least 3 audio calls, got {len(audio_calls)}: {audio_calls}"
            
            voiceover_calls = [c for c in audio_calls if c[0] == "voiceover"]
            sfx_calls = [c for c in audio_calls if c[0] == "sfx"]
            music_calls = [c for c in audio_calls if c[0] == "music"]
            
            assert len(voiceover_calls) >= 1, f"Expected voiceover call, got: {audio_calls}"
            assert len(sfx_calls) >= 1, f"Expected sfx call, got: {audio_calls}"
            assert len(music_calls) >= 1, f"Expected music call, got: {audio_calls}"
            
            # Verify episode_id is correct in all calls
            for call in audio_calls:
                call_type, episode_id = call[0], call[1]
                assert episode_id == "ep_audio_test", f"{call_type} called with wrong episode_id: {episode_id}"


def test_audio_activities_signature():
    """Test that audio activities have episode_id as first parameter."""
    import inspect
    from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music
    
    # Check generate_voiceover signature
    sig = inspect.signature(generate_voiceover)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_voiceover first param should be episode_id, got {params[0]}"
    assert params[1] == "script", f"generate_voiceover second param should be script, got {params[1]}"
    assert params[2] == "voice_id", f"generate_voiceover third param should be voice_id, got {params[2]}"
    
    # Check generate_sfx signature
    sig = inspect.signature(generate_sfx)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_sfx first param should be episode_id, got {params[0]}"
    assert params[1] == "description", f"generate_sfx second param should be description, got {params[1]}"
    
    # Check generate_music signature
    sig = inspect.signature(generate_music)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_music first param should be episode_id, got {params[0]}"
    assert params[1] == "style", f"generate_music second param should be style, got {params[1]}"
    assert params[2] == "duration", f"generate_music third param should be duration, got {params[2]}"
