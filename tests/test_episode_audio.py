"""Test episode.py audio stage in dry-run."""

import pytest


@pytest.mark.asyncio
async def test_audio_activities_accept_episode_id(tmp_path, monkeypatch):
    """Test that audio activities accept episode_id parameter without TypeError."""
    import os
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(tmp_path / "test.db"))
    
    # Import activities
    from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music
    
    # Test generate_voiceover with episode_id
    result = await generate_voiceover("ep99", "Test script", "voice-default")
    assert result is not None
    assert "voiceover" in result or "audio" in result
    
    # Test generate_sfx with episode_id
    result = await generate_sfx("ep99", "Test sound")
    assert result is not None
    assert "sfx" in result or "audio" in result
    
    # Test generate_music with episode_id
    result = await generate_music("ep99", "upbeat", 60.0)
    assert result is not None
    assert "music" in result or "audio" in result
