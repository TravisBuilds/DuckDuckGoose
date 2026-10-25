"""G-AUD-COST: Tests for actual ElevenLabs cost tracking from response headers."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hfvg.providers.elevenlabs import (
    ELEVENLABS_CREDIT_USD_MICROS,
    ElevenLabsProvider,
    credits_to_usd,
    credits_to_usd_micros,
    estimate_credits,
)


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Temporary output directory for provider."""
    return tmp_path / "audio_output"


@pytest.fixture
def provider(tmp_output_dir: Path) -> ElevenLabsProvider:
    """ElevenLabs provider with real API key."""
    return ElevenLabsProvider(dry_run=False, output_dir=tmp_output_dir, api_key="test-key")


def mock_httpx_response(content: bytes, headers: dict[str, str], status_code: int = 200):
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.headers = headers
    resp.text = ""
    return resp


@pytest.mark.asyncio
async def test_tts_extracts_character_cost_from_header(provider: ElevenLabsProvider, tmp_output_dir: Path):
    """TTS submit extracts actual credits from character-cost header."""
    text = "Hello world, this is a test of the audio system."
    voice_id = "21m00Tcm4TlvDq8ikWAM"
    model = "eleven_v3"
    
    # Mock audio content (minimal MP3 header + silence)
    audio_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 100
    
    # Mock response with character-cost header (28 credits for 71 chars)
    mock_resp = mock_httpx_response(
        audio_bytes, 
        {"character-cost": "28", "content-type": "audio/mpeg"}
    )
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        
        job_id = await provider.submit_audio(text, voice_id, model=model)
        
        # Verify the job metadata includes actual credits
        job_status = await provider.get_job_status(job_id)
        assert job_status.raw is not None
        assert job_status.raw["credits_used"] == 28.0
        assert job_status.raw["characters"] == len(text)
        assert job_status.raw["cost_from_header"] is True


@pytest.mark.asyncio
async def test_sfx_extracts_character_cost_from_header(provider: ElevenLabsProvider, tmp_output_dir: Path):
    """SFX submit extracts actual credits from character-cost header."""
    text = "thunder and lightning storm"
    duration = 5.0
    
    audio_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 200
    mock_resp = mock_httpx_response(
        audio_bytes,
        {"character-cost": "20", "content-type": "audio/mpeg"}
    )
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        
        job_id = await provider.submit_sfx(text, duration=duration)
        
        job_status = await provider.get_job_status(job_id)
        assert job_status.raw is not None
        assert job_status.raw["credits_used"] == 20.0
        assert job_status.raw["cost_from_header"] is True


@pytest.mark.asyncio
async def test_music_extracts_character_cost_from_header(provider: ElevenLabsProvider, tmp_output_dir: Path):
    """Music submit extracts actual credits from character-cost header."""
    prompt = "upbeat jazz piano"
    duration = 10.0
    model = "music_v2"
    
    audio_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 500
    mock_resp = mock_httpx_response(
        audio_bytes,
        {"character-cost": "150", "content-type": "audio/mpeg"}
    )
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        
        job_id = await provider.submit_music(prompt, duration, model=model)
        
        job_status = await provider.get_job_status(job_id)
        assert job_status.raw is not None
        assert job_status.raw["credits_used"] == 150.0
        assert job_status.raw["cost_from_header"] is True


@pytest.mark.asyncio
async def test_fallback_when_character_cost_header_absent(provider: ElevenLabsProvider, tmp_output_dir: Path):
    """When character-cost header is missing, fall back to estimate and flag it."""
    text = "Test text without header"
    voice_id = "21m00Tcm4TlvDq8ikWAM"
    
    audio_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 100
    # Response WITHOUT character-cost header
    mock_resp = mock_httpx_response(audio_bytes, {"content-type": "audio/mpeg"})
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        
        job_id = await provider.submit_audio(text, voice_id, model="eleven_v3")
        
        job_status = await provider.get_job_status(job_id)
        assert job_status.raw is not None
        # Should have estimated credits
        assert job_status.raw["credits_used"] is not None
        assert job_status.raw["credits_used"] > 0
        # Should flag that it's from estimate, not header
        assert job_status.raw["cost_from_header"] is False


def test_estimate_credits():
    """Test credit estimation functions."""
    # TTS: ~2.5 chars per credit
    tts_credits = estimate_credits("tts", {"text": "x" * 25, "characters": 25})
    assert 8 <= tts_credits <= 12  # Conservative estimate
    
    # SFX: 20 credits per 5 seconds
    sfx_credits = estimate_credits("sfx", {"duration": 10.0})
    assert sfx_credits == 40.0
    
    # Music: 1400 credits per 95 seconds
    music_credits = estimate_credits("music", {"duration": 95.0})
    assert music_credits == 1400.0


def test_credits_to_usd():
    """Test credit-to-USD conversion."""
    # 1 credit = $0.0002
    assert credits_to_usd(1.0) == 0.0002
    assert credits_to_usd(28.0) == 0.0056
    assert credits_to_usd(1000.0) == 0.2


def test_credits_to_usd_micros():
    """Test credit-to-microdollar conversion."""
    # 1 credit = 200 micro-dollars
    assert credits_to_usd_micros(1.0) == ELEVENLABS_CREDIT_USD_MICROS
    assert credits_to_usd_micros(28.0) == 28 * ELEVENLABS_CREDIT_USD_MICROS
    assert credits_to_usd_micros(0.5) == 100  # Rounds to nearest


@pytest.mark.asyncio
async def test_case_insensitive_header_extraction(provider: ElevenLabsProvider, tmp_output_dir: Path):
    """character-cost header extraction is case-insensitive."""
    text = "case test"
    voice_id = "test"
    
    audio_bytes = b"\xff\xfb\x90\x00" + b"\x00" * 50
    # Test with Title-Case header
    mock_resp = mock_httpx_response(audio_bytes, {"Character-Cost": "15"})
    
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client
        
        job_id = await provider.submit_audio(text, voice_id)
        
        job_status = await provider.get_job_status(job_id)
        assert job_status.raw["credits_used"] == 15.0
        assert job_status.raw["cost_from_header"] is True
