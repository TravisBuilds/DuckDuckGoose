"""Tests for provider adapters."""

import pytest

from hfvg.providers import HiggsfieldProvider, ElevenLabsProvider, ProviderJobStatus


@pytest.mark.asyncio
async def test_higgsfield_dry_run_image():
    """Test Higgsfield image generation in dry-run mode."""
    provider = HiggsfieldProvider(dry_run=True)
    
    job_id = await provider.submit_image(
        prompt="A cozy mountain resort lobby",
        resolution="2k",
        quality="high"
    )
    
    assert job_id.startswith("img_mock_")
    
    # Poll job
    result = await provider.get_job_status(job_id)
    assert result.status == ProviderJobStatus.COMPLETED
    assert result.output_url is not None
    assert result.cost > 0


@pytest.mark.asyncio
async def test_higgsfield_dry_run_video():
    """Test Higgsfield video generation in dry-run mode."""
    provider = HiggsfieldProvider(dry_run=True)
    
    job_id = await provider.submit_video(
        start_image="https://example.com/start.png",
        prompt="Gentle breathing motion",
        model="kling_3.0",
        duration=5.0,
        resolution="1080p"
    )
    
    assert job_id.startswith("vid_mock_")
    
    result = await provider.get_job_status(job_id)
    assert result.status == ProviderJobStatus.COMPLETED


@pytest.mark.asyncio
async def test_higgsfield_cost_estimation():
    """Test Higgsfield cost estimation."""
    provider = HiggsfieldProvider(dry_run=True)
    
    # Image cost
    cost = await provider.estimate_cost("image", {
        "model": "gpt_image_2",
        "resolution": "2k",
        "quality": "high"
    })
    assert cost == 6.5
    
    # Draft still cost
    cost = await provider.estimate_cost("image", {
        "model": "gpt_image_2",
        "resolution": "1k",
        "quality": "medium"
    })
    assert cost == 1.0
    
    # Kling video cost
    cost = await provider.estimate_cost("video", {
        "model": "kling_3.0",
        "quality": "pro",
        "duration": 5.0
    })
    assert cost == 7.5  # 1.5/s * 5s


@pytest.mark.asyncio
async def test_higgsfield_balance():
    """Test Higgsfield balance check."""
    provider = HiggsfieldProvider(dry_run=True)
    
    balance = await provider.get_balance()
    assert balance == 1500.0  # Mock balance


@pytest.mark.asyncio
async def test_elevenlabs_dry_run_audio():
    """Test ElevenLabs audio generation in dry-run mode."""
    provider = ElevenLabsProvider(dry_run=True)
    
    job_id = await provider.submit_audio(
        text="きょうのお客様は、三世代のニホンザルのご家族です。",
        voice_id="4sirbXwrtRlmPV80MJkQ",
        model="eleven_multilingual_v2"
    )
    
    assert job_id.startswith("aud_mock_")
    
    result = await provider.get_job_status(job_id)
    assert result.status == ProviderJobStatus.COMPLETED
    assert result.output_url is not None


@pytest.mark.asyncio
async def test_elevenlabs_cost_estimation():
    """Test ElevenLabs cost estimation."""
    provider = ElevenLabsProvider(dry_run=True)
    
    # TTS cost
    cost = await provider.estimate_cost("tts", {
        "model": "eleven_multilingual_v2",
        "text": "A" * 1000  # 1k chars
    })
    assert cost > 0
    
    # Music cost
    cost = await provider.estimate_cost("music", {
        "model": "eleven_music",
        "duration": 95.0
    })
    assert cost == 1400.0


@pytest.mark.asyncio
async def test_elevenlabs_balance():
    """Test ElevenLabs balance check."""
    provider = ElevenLabsProvider(dry_run=True)
    
    balance = await provider.get_balance()
    assert balance == 5000.0  # Mock balance


@pytest.mark.asyncio
async def test_providers_never_call_real_api_in_dry_run():
    """Ensure providers don't make real API calls in dry-run mode."""
    # This test verifies that even with invalid API keys,
    # dry-run mode works without errors
    
    hf_provider = HiggsfieldProvider(api_key="invalid_key", dry_run=True)
    el_provider = ElevenLabsProvider(api_key="invalid_key", dry_run=True)
    
    # These should all succeed with mock responses
    job_id = await hf_provider.submit_image("test")
    assert job_id.startswith("img_mock_")
    
    job_id = await el_provider.submit_audio("test", "test_voice")
    assert job_id.startswith("aud_mock_")
    
    # Balance checks should work
    assert await hf_provider.get_balance() == 1500.0
    assert await el_provider.get_balance() == 5000.0
