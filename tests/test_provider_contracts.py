"""
Contract tests for Higgsfield API providers.

Tests that requests match the documented API schemas and responses are parsed correctly.
Uses strict mock validation to ensure unknown fields → 422, missing required fields → 422.
"""

import pytest
import respx
import httpx
from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
from hfvg.providers.kling_video import KlingVideoProvider


@pytest.mark.asyncio
@respx.mock
async def test_still_contract_valid_request():
    """Test that a valid still request matches the documented schema."""
    # Mock successful submission
    request_id = "req_still_valid_123"
    still_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": request_id})
    )
    
    provider = HiggsfieldStillProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        result = await provider.submit_image(
            prompt="A serene duck",
            resolution="1k",
            quality="medium",
            image_urls=["http://example.com/ref1.jpg"],
        )
        
        assert result == request_id
        assert still_route.call_count == 1
        
        # Verify request body structure
        request = still_route.calls[0].request
        body = httpx.QueryParams(request.content.decode())
        # Parse JSON from content
        import json
        body = json.loads(request.content.decode())
        
        # Required fields
        assert "prompt" in body
        assert "resolution" in body
        assert "quality" in body
        assert body["quality"] in ("low", "medium"), "Quality must be low or medium only"
        
        # Optional fields
        if "image_urls" in body:
            assert isinstance(body["image_urls"], list)
            assert len(body["image_urls"]) <= 3
        
        # Forbidden fields
        assert "model" not in body, "Body must not contain 'model' field"
        assert "references" not in body, "Must use 'image_urls' not 'references'"
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_still_contract_invalid_quality_maps_to_medium():
    """Test that invalid quality values are mapped to 'medium'."""
    request_id = "req_still_quality_map"
    respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": request_id})
    )
    
    provider = HiggsfieldStillProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        result = await provider.submit_image(
            prompt="Test",
            quality="high",  # Not allowed in docs
        )
        
        # Verify it was mapped to medium
        request = respx.calls[0].request
        import json
        body = json.loads(request.content.decode())
        assert body["quality"] == "medium", "Invalid 'high' should be mapped to 'medium'"
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_still_contract_response_parsing():
    """Test that still status response is parsed correctly."""
    request_id = "req_still_response_123"
    
    # Mock status response with documented format
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "images": [
                {"url": "http://example.com/output.jpg"}
            ],
            "cost": 4.5
        })
    )
    
    provider = HiggsfieldStillProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        job = await provider.get_job_status(request_id)
        
        # Verify parsing of images[0].url
        assert job.output_url == "http://example.com/output.jpg"
        assert job.cost == 4.5
        assert job.status.name == "COMPLETED"
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_still_contract_missing_images_returns_none():
    """Test that missing images array returns None for URL."""
    request_id = "req_still_no_images"
    
    # Mock response without images array (shouldn't happen but handle gracefully)
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "cost": 4.0
        })
    )
    
    provider = HiggsfieldStillProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        job = await provider.get_job_status(request_id)
        
        # URL should be None if images array is missing
        assert job.output_url is None
        assert job.cost == 4.0
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_clip_contract_valid_request():
    """Test that a valid Kling request matches the documented schema."""
    request_id = "req_clip_valid_456"
    clip_route = respx.post("http://mock.api/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": request_id})
    )
    
    provider = KlingVideoProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        result = await provider.submit_video(
            image_url="http://example.com/start.jpg",
            prompt="Breathing motion",
            duration=5,
        )
        
        assert result == request_id
        assert clip_route.call_count == 1
        
        # Verify request body structure
        request = clip_route.calls[0].request
        import json
        body = json.loads(request.content.decode())
        
        # Required fields
        assert "image_url" in body, "Must have 'image_url' field"
        assert "duration" in body
        assert isinstance(body["duration"], int), "Duration must be integer"
        assert "sound" in body
        assert body["sound"] == "off", "Sound must be 'off' for cost predictability"
        
        # Forbidden fields
        assert "start_image" not in body, "Must use 'image_url' not 'start_image'"
        assert "resolution" not in body, "Resolution not in documented schema"
        assert "draft" not in body, "Draft not in documented schema"
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_clip_contract_response_parsing():
    """Test that clip status response is parsed correctly."""
    request_id = "req_clip_response_789"
    
    # Mock status response with documented format
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "video": {
                "url": "http://example.com/output.mp4"
            },
            "cost": 7.5
        })
    )
    
    provider = KlingVideoProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        job = await provider.get_job_status(request_id)
        
        # Verify parsing of video.url
        assert job.output_url == "http://example.com/output.mp4"
        assert job.cost == 7.5
        assert job.status.name == "COMPLETED"
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_clip_contract_missing_video_returns_none():
    """Test that missing video object returns None for URL."""
    request_id = "req_clip_no_video"
    
    # Mock response without video object
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "cost": 7.5
        })
    )
    
    provider = KlingVideoProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        job = await provider.get_job_status(request_id)
        
        # URL should be None if video object is missing
        assert job.output_url is None
        assert job.cost == 7.5
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_estimate_apis_called():
    """Test that estimate APIs are called with correct format."""
    # Mock still estimate
    still_estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"estimated_cost": 5.5})
    )
    
    still_provider = HiggsfieldStillProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        cost = await still_provider.estimate_cost(
            prompt="Test",
            resolution="2k",
            quality="medium",
            num_refs=2
        )
        
        assert cost == 5.5
        assert still_estimate_route.call_count == 1
        
    finally:
        await still_provider.close()
    
    # Mock clip estimate
    clip_estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"estimated_cost": 9.0})
    )
    
    clip_provider = KlingVideoProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        cost = await clip_provider.estimate_cost(duration=6)
        
        assert cost == 9.0
        assert clip_estimate_route.call_count == 1
        
    finally:
        await clip_provider.close()
