"""Test G-AUD-KEY: Missing API keys detected before reservation/spend."""

import os
import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music


@pytest.mark.asyncio
async def test_generate_voiceover_refuses_missing_elevenlabs_key_before_reservation(monkeypatch):
    """Test: missing ELEVENLABS_API_KEY raises non-retryable error BEFORE any reservation."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    
    with pytest.raises(ApplicationError) as exc_info:
        await generate_voiceover(
            episode_id="ep99",
            script="Test voiceover",
            voice_id="test-voice-id",
            model="eleven_v3",
            line_id="vo01"
        )
    
    assert exc_info.value.non_retryable is True, "Error must be non-retryable"
    assert exc_info.value.type == "MissingCredentials"
    assert "ELEVENLABS_API_KEY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_sfx_refuses_missing_elevenlabs_key_before_reservation(monkeypatch):
    """Test: missing ELEVENLABS_API_KEY raises non-retryable error BEFORE any reservation."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    
    with pytest.raises(ApplicationError) as exc_info:
        await generate_sfx(
            episode_id="ep99",
            description="Test sound effect",
            duration=3.0,
            cue_id="sfx01"
        )
    
    assert exc_info.value.non_retryable is True, "Error must be non-retryable"
    assert exc_info.value.type == "MissingCredentials"
    assert "ELEVENLABS_API_KEY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_generate_music_refuses_missing_elevenlabs_key_before_reservation(monkeypatch):
    """Test: missing ELEVENLABS_API_KEY raises non-retryable error BEFORE any reservation."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    
    with pytest.raises(ApplicationError) as exc_info:
        await generate_music(
            episode_id="ep99",
            style="ambient calm",
            duration=120.0,
            model="music_v1",
            cue_id="music01"
        )
    
    assert exc_info.value.non_retryable is True, "Error must be non-retryable"
    assert exc_info.value.type == "MissingCredentials"
    assert "ELEVENLABS_API_KEY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_audio_activities_skip_credential_check_in_dry_run(monkeypatch):
    """Test: credential check skipped in DRY_RUN mode."""
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    
    # Should not raise - dry run doesn't need credentials
    result = await generate_voiceover(
        episode_id="ep99",
        script="Test",
        voice_id="voice-id",
        line_id="vo01"
    )
    assert result["dry_run"] is True
    assert "dry_run_stand_in" in result["source"]
