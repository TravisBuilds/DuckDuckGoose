"""Tests for SOUL V2 API parameter compliance."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_soul_v2_submit_uses_only_documented_params():
    """Test: SOUL V2 submit_image uses only documented API parameters."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    # Mock httpx client
    mock_response = MagicMock()
    mock_response.json.return_value = {"request_id": "test-request-id"}
    mock_response.raise_for_status = MagicMock()
    
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    
    provider = HiggsfieldProvider(api_key="test:key", dry_run=False)
    provider.http_client = mock_client
    
    # Submit image with our parameters
    await provider.submit_image(
        prompt="Test prompt",
        model="soul_v2",
        resolution="2k",  # Our format
        quality="high",   # Our parameter (should NOT be sent to API)
    )
    
    # Verify API call
    assert mock_client.post.called
    call_args = mock_client.post.call_args
    
    # Check URL
    assert call_args[0][0] == "https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard"
    
    # Check body - should only have documented SOUL V2 parameters
    body = call_args[1]["json"]
    assert "prompt" in body
    assert body["prompt"] == "Test prompt"
    
    # Resolution should be mapped to API format
    assert "resolution" in body
    assert body["resolution"] == "1080p"  # 2k -> 1080p
    
    # Quality should NOT be in body (not a SOUL V2 parameter)
    assert "quality" not in body


@pytest.mark.asyncio
async def test_soul_v2_resolution_mapping():
    """Test: Resolution values are correctly mapped to SOUL V2 API format."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    mock_response = MagicMock()
    mock_response.json.return_value = {"request_id": "test-id"}
    mock_response.raise_for_status = MagicMock()
    
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    
    provider = HiggsfieldProvider(api_key="test:key", dry_run=False)
    provider.http_client = mock_client
    
    # Test 2k -> 1080p
    await provider.submit_image(prompt="test", model="soul_v2", resolution="2k")
    body = mock_client.post.call_args[1]["json"]
    assert body["resolution"] == "1080p"
    
    # Test 1k -> 720p
    await provider.submit_image(prompt="test", model="soul_v2", resolution="1k")
    body = mock_client.post.call_args[1]["json"]
    assert body["resolution"] == "720p"
    
    # Test 720p -> 720p (already in API format)
    await provider.submit_image(prompt="test", model="soul_v2", resolution="720p")
    body = mock_client.post.call_args[1]["json"]
    assert body["resolution"] == "720p"
    
    # Test 1080p -> 1080p (already in API format)
    await provider.submit_image(prompt="test", model="soul_v2", resolution="1080p")
    body = mock_client.post.call_args[1]["json"]
    assert body["resolution"] == "1080p"


@pytest.mark.asyncio
async def test_soul_v2_documented_parameters_only():
    """Test: Verify we only send parameters documented in SOUL V2 API schema.
    
    Documented parameters per https://docs.higgsfield.ai/docs/models/soul-2/generate:
    - prompt (required)
    - resolution (720p/1080p)
    - aspect_ratio
    - enhance_prompt
    - custom_reference_id
    - custom_reference_strength
    - style_id
    - style_strength
    - seed
    - batch_size
    
    NOT supported:
    - quality (not in schema)
    - references (different mechanism in SOUL V2)
    """
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    mock_response = MagicMock()
    mock_response.json.return_value = {"request_id": "test-id"}
    mock_response.raise_for_status = MagicMock()
    
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    
    provider = HiggsfieldProvider(api_key="test:key", dry_run=False)
    provider.http_client = mock_client
    
    await provider.submit_image(
        prompt="Test",
        model="soul_v2",
        resolution="2k",
        quality="high",
        references=["ref1.png", "ref2.png"],
    )
    
    body = mock_client.post.call_args[1]["json"]
    
    # Must have prompt
    assert "prompt" in body
    
    # Must have resolution in API format
    assert "resolution" in body
    assert body["resolution"] in ["720p", "1080p"]
    
    # Must NOT have our internal parameters
    assert "quality" not in body
    assert "references" not in body
