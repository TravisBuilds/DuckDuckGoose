"""Tests for G1 plan estimation with proper image_url handling."""

import pytest
import respx
import httpx
from unittest.mock import AsyncMock, patch

from hfvg.activities.g1_plan import compute_g1_plan_activity


@pytest.mark.asyncio
@respx.mock
async def test_g1_estimate_sends_string_image_url_for_kling(monkeypatch):
    """
    G1 estimate must send a valid string image_url to Kling provider, not None.
    
    Mutation: Remove placeholder URL assignment (image_url stays None) -> HTTP 400.
    """
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
    monkeypatch.setenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", "https://example.com/placeholder.jpg")
    
    # Mock Higgsfield still estimate
    still_route = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "0.05", "credits": "0.5"})
    )
    
    # Mock Kling clip estimate - will capture the request payload
    clip_route = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"usd": "0.28", "credits": "8.96"})
    )
    
    shots = [
        {
            "shot_id": "A01",
            "prompt": "Test shot",
            "params": {"duration": 5.0, "model": "kling", "resolution": "1k", "quality": "medium", "aspect_ratio": "9:16"},
            "refs": []
        }
    ]
    
    result = await compute_g1_plan_activity("ep01", shots, dry_run=False)
    
    # Verify clip estimate was called
    assert clip_route.called
    request = clip_route.calls[0].request
    
    # Parse request body
    import json
    body = json.loads(request.content)
    
    # CRITICAL: image_url must be a string, not None
    assert "image_url" in body, "image_url missing from clip estimate payload"
    assert isinstance(body["image_url"], str), f"image_url must be string, got {type(body['image_url'])}"
    assert body["image_url"] == "https://example.com/placeholder.jpg", f"Wrong image_url: {body['image_url']}"
    
    # Duration must be int, not float
    assert "duration" in body
    assert isinstance(body["duration"], int), f"duration must be int, got {type(body['duration'])}"
    assert body["duration"] == 5
    
    # Verify result
    assert result["estimates_available"] is True
    assert len(result["shots"]) == 1
    assert result["shots"][0]["clip_estimate_available"] is True


@pytest.mark.asyncio
@respx.mock
async def test_g1_estimate_sends_last_image_url_for_end_frame_shots(monkeypatch):
    """
    G1 estimate must send last_image_url (placeholder) for end-frame shots.
    
    Mutation: Remove last_image_url logic -> end frame not estimated correctly.
    """
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
    monkeypatch.setenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", "https://example.com/placeholder.jpg")
    
    # Mock Higgsfield still estimate
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "0.05", "credits": "0.5"})
    )
    
    # Mock Kling clip estimate
    clip_route = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"usd": "0.28", "credits": "8.96"})
    )
    
    shots = [
        {
            "shot_id": "A01",
            "prompt": "Test end frame shot",
            "params": {
                "duration": 5.0,
                "model": "kling",
                "resolution": "1k",
                "quality": "medium",
                "aspect_ratio": "9:16",
                "end_frame": True  # End frame shot
            },
            "refs": []
        }
    ]
    
    result = await compute_g1_plan_activity("ep01", shots, dry_run=False)
    
    # Verify clip estimate was called
    assert clip_route.called
    request = clip_route.calls[0].request
    
    # Parse request body
    import json
    body = json.loads(request.content)
    
    # For end-frame shots, last_image_url must be present and a string
    assert "end_image_url" in body or "last_image_url" in body, "last_image_url missing for end-frame shot"
    last_url = body.get("end_image_url") or body.get("last_image_url")
    assert isinstance(last_url, str), f"last_image_url must be string, got {type(last_url)}"
    assert last_url == "https://example.com/placeholder.jpg"
    
    # Still estimate should be doubled (start + end frame)
    assert result["shots"][0]["still_credits"] == 1.0  # 0.5 * 2


@pytest.mark.asyncio
@respx.mock
async def test_g1_estimate_sends_duration_as_int_not_float(monkeypatch):
    """
    G1 estimate must send duration as int, not float.
    
    Mutation: Remove int() cast -> duration sent as float -> potential API rejection.
    """
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
    monkeypatch.setenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", "https://example.com/placeholder.jpg")
    
    # Mock Higgsfield still estimate
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "0.05", "credits": "0.5"})
    )
    
    # Mock Kling clip estimate
    clip_route = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"usd": "0.28", "credits": "8.96"})
    )
    
    shots = [
        {
            "shot_id": "A01",
            "prompt": "Test shot",
            "params": {
                "duration": 5.5,  # Float duration from beatmap
                "model": "kling",
                "resolution": "1k",
                "quality": "medium",
                "aspect_ratio": "9:16"
            },
            "refs": []
        }
    ]
    
    result = await compute_g1_plan_activity("ep01", shots, dry_run=False)
    
    # Verify clip estimate was called
    assert clip_route.called
    request = clip_route.calls[0].request
    
    # Parse request body
    import json
    body = json.loads(request.content)
    
    # Duration must be int
    assert "duration" in body
    assert isinstance(body["duration"], int), f"duration must be int, got {type(body['duration'])} = {body['duration']}"
    assert body["duration"] == 5  # Truncated to int


@pytest.mark.asyncio
@respx.mock
async def test_g1_estimate_available_false_only_on_genuine_failure(monkeypatch):
    """
    estimates_available should be False only for genuine estimate failures, not None image_url.
    
    The fix ensures we send valid image_url, so this tests that we handle real failures correctly.
    """
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
    
    # Mock still estimate success
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "0.05", "credits": "0.5"})
    )
    
    # Mock clip estimate failure (genuine API error)
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(500, json={"error": "Internal server error"})
    )
    
    shots = [
        {
            "shot_id": "A01",
            "prompt": "Test shot",
            "params": {"duration": 5.0, "model": "kling"},
            "refs": []
        }
    ]
    
    result = await compute_g1_plan_activity("ep01", shots, dry_run=False)
    
    # estimates_available should be False due to genuine clip estimate failure
    assert result["estimates_available"] is False
    assert result["shots"][0]["clip_estimate_available"] is False
    assert result["shots"][0]["still_estimate_available"] is True


@pytest.mark.asyncio
async def test_g1_seedance_estimate_no_image_url(monkeypatch):
    """
    Seedance estimates don't use image URLs (rate table based).
    
    Verify Seedance path doesn't try to pass image_url.
    """
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test.db")
    
    # Mock still provider
    with patch("hfvg.activities.g1_plan.HiggsfieldStillProvider") as MockStill:
        mock_still = AsyncMock()
        mock_still.estimate_usd_and_credits = AsyncMock(return_value=(50_000, 0.5))
        mock_still.close = AsyncMock()
        MockStill.return_value = mock_still
        
        # Mock Seedance provider
        with patch("hfvg.activities.g1_plan.SeedanceVideoProvider") as MockSeedance:
            mock_seedance = AsyncMock()
            mock_seedance.estimate_usd_and_credits = AsyncMock(return_value=(280_000, 8.96))
            mock_seedance.close = AsyncMock()
            MockSeedance.return_value = mock_seedance
            
            shots = [
                {
                    "shot_id": "A01",
                    "prompt": "Test shot",
                    "params": {"duration": 5.0, "model": "seedance", "resolution": "720p"},
                    "refs": []
                }
            ]
            
            result = await compute_g1_plan_activity("ep01", shots, dry_run=False)
            
            # Verify Seedance estimate was called with duration and resolution only (no image_url)
            mock_seedance.estimate_usd_and_credits.assert_called_once()
            call_kwargs = mock_seedance.estimate_usd_and_credits.call_args[1]
            assert "duration" in call_kwargs
            assert isinstance(call_kwargs["duration"], int)
            assert "resolution" in call_kwargs
            assert "image_url" not in call_kwargs  # Seedance doesn't use image_url
            
            assert result["estimates_available"] is True
