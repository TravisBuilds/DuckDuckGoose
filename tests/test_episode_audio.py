"""Test episode audio activities signature and direct calls."""

import pytest
import inspect
from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music


def test_audio_activities_signature():
    """Test that audio activities have episode_id as first parameter."""
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


@pytest.mark.asyncio
async def test_audio_activities_dry_run_calls(tmp_path, monkeypatch):
    """Test audio activities work in dry-run mode with episode_id."""
    # Set up dry-run environment
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "1")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "1")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep_audio_test", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep_audio_test", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep_audio_test", "test")
    
    # Test calling activities directly with episode_id as first arg
    vo_url = await generate_voiceover("ep_audio_test", "Test voiceover script", "voice-default")
    assert vo_url.startswith("dry-run://"), f"Expected dry-run URL, got {vo_url}"
    
    sfx_url = await generate_sfx("ep_audio_test", "Test sound effect")
    assert sfx_url.startswith("dry-run://"), f"Expected dry-run URL, got {sfx_url}"
    
    music_url = await generate_music("ep_audio_test", "upbeat", 30.0)
    assert music_url.startswith("dry-run://"), f"Expected dry-run URL, got {music_url}"
