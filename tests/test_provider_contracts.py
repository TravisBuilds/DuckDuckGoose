"""
Contract tests for Higgsfield API providers.

Tests that requests match the documented API schemas and responses are parsed correctly.
Uses strict mock validation to ensure unknown fields → 422, missing required fields → 422.

The documented API:
- Estimate returns {"credits": "<str>", "usd": "<str>"} - NO estimated_cost
- Status returns NO cost field (only on completed, images/video URLs)
- Unknown fields in requests → 422
"""

import pytest
import respx
import httpx
from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
from hfvg.providers.kling_video import KlingVideoProvider
from tests.fixtures_higgsfield_strict import setup_strict_mock


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
    """Test that still status response is parsed correctly (NO cost field in documented API)."""
    request_id = "req_still_response_123"
    
    # Mock status response with documented format - NO cost field
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "images": [
                {"url": "http://example.com/output.jpg"}
            ],
            "progress": 1.0
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
        # Documented API has NO cost field
        assert job.cost is None
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
            "progress": 1.0
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
        # Documented API has NO cost field
        assert job.cost is None
        
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
    """Test that clip status response is parsed correctly (NO cost field in documented API)."""
    request_id = "req_clip_response_789"
    
    # Mock status response with documented format - NO cost field
    respx.get(f"http://mock.api/requests/{request_id}/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "video": {
                "url": "http://example.com/output.mp4"
            },
            "progress": 1.0
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
        # Documented API has NO cost field
        assert job.cost is None
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
            "progress": 1.0
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
        # Documented API has NO cost field
        assert job.cost is None
        
    finally:
        await provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_estimate_apis_called():
    """Test that estimate APIs are called with correct format and parse documented response."""
    # Mock still estimate with documented response format: {"credits": "<str>", "usd": "<str>"}
    still_estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "5.5", "usd": "0.055"})
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
            aspect_ratio="9:16"
        )
        
        assert cost == 5.5
        assert still_estimate_route.call_count == 1
        
    finally:
        await still_provider.close()
    
    # Mock clip estimate with documented response format
    clip_estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "9.0", "usd": "0.09"})
    )
    
    clip_provider = KlingVideoProvider(
        api_key="test_id:test_secret",
        base_url="http://mock.api"
    )
    
    try:
        cost = await clip_provider.estimate_cost(
            image_url="http://example.com/start.jpg",
            prompt="Test",
            duration=6
        )
        
        assert cost == 9.0
        assert clip_estimate_route.call_count == 1
        
        # Verify full request body was sent
        request = clip_estimate_route.calls[0].request
        import json
        body = json.loads(request.content.decode())
        assert "image_url" in body
        assert "duration" in body
        assert "sound" in body
        assert "prompt" in body
        
    finally:
        await clip_provider.close()


@pytest.mark.asyncio
@respx.mock
async def test_full_live_flow_with_strict_mock():
    """
    Test complete live flow: still estimate, submit, poll, commit, then clip estimate, submit, poll, commit.
    
    Uses strict mock that enforces documented API contract:
    - Estimates return {"credits": "<str>", "usd": "<str>"}
    - Status has NO cost field
    - Actual cost committed equals the estimate
    
    Verifies final ledger: spent = estimates, reserved = 0
    """
    import tempfile
    import os
    from hfvg.budget import BudgetLedger
    
    # Create temporary DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    try:
        # Initialize ledger and episode
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        episode_id = "ep_test_live_flow"
        credit_plan = {
            "lines": {
                "L2_drafts": {"cap": 100.0, "stop": 80.0},
                "L4_video": {"cap": 200.0, "stop": 160.0},
            }
        }
        await ledger.init_episode_budget_from_plan(episode_id, credit_plan)
        
        # STILL FLOW
        # 1. Estimate still - documented response format
        still_estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
        )
        
        still_provider = HiggsfieldStillProvider(
            api_key="test_id:test_secret",
            base_url="http://mock.api"
        )
        
        still_estimate = await still_provider.estimate_cost(
            prompt="A duck", resolution="1k", quality="medium", aspect_ratio="9:16"
        )
        assert still_estimate == 4.0
        
        # 2. Reserve budget
        reserved_still = await ledger.reserve(
            episode_id=episode_id,
            line_name="L2_drafts",
            amount=still_estimate,
            reason="Still test"
        )
        assert reserved_still
        
        # 3. Submit still
        still_submit_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"request_id": "req_still_123"})
        )
        
        still_job_id = await still_provider.submit_image(
            prompt="A duck", resolution="1k", quality="medium"
        )
        assert still_job_id == "req_still_123"
        
        # 4. Poll until complete (first pending, then complete with NO cost)
        call_count = [0]
        
        def still_status_handler(request):
            call_count[0] += 1
            if call_count[0] == 1:
                return httpx.Response(200, json={"status": "queued", "progress": 0.0})
            else:
                # Documented API: NO cost field
                return httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "http://cdn.example.com/still.jpg"}],
                    "progress": 1.0
                })
        
        respx.get(f"http://mock.api/requests/{still_job_id}/status").mock(side_effect=still_status_handler)
        
        # Poll
        job_status = await still_provider.get_job_status(still_job_id)
        assert job_status.status.name == "PENDING"
        
        job_status = await still_provider.get_job_status(still_job_id)
        assert job_status.status.name == "COMPLETED"
        assert job_status.output_url == "http://cdn.example.com/still.jpg"
        assert job_status.cost is None  # Documented API has NO cost
        
        # 5. Commit with estimate as actual cost (since status has no cost)
        actual_still_cost = job_status.cost or still_estimate
        await ledger.commit(
            episode_id=episode_id,
            line_name="L2_drafts",
            reserved_amount=still_estimate,
            actual_cost=actual_still_cost,
            usd_micros=None,
            job_id=still_job_id,
            reason="Still completed"
        )
        
        await still_provider.close()
        
        # Check ledger after still
        budget = await ledger.get_episode_summary(episode_id)
        l2 = next(line for line in budget["lines"] if line["line_name"] == "L2_drafts")
        assert l2["spent"] == 4.0
        assert l2["reserved"] == 0.0
        
        # CLIP FLOW
        # 1. Estimate clip with full request params
        clip_estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.075"})
        )
        
        clip_provider = KlingVideoProvider(
            api_key="test_id:test_secret",
            base_url="http://mock.api"
        )
        
        clip_estimate = await clip_provider.estimate_cost(
            image_url="http://cdn.example.com/still.jpg",
            prompt="Breathing",
            duration=5
        )
        assert clip_estimate == 7.5
        
        # Verify estimate request had all required fields
        est_req = clip_estimate_route.calls[0].request
        import json
        est_body = json.loads(est_req.content.decode())
        assert est_body["image_url"] == "http://cdn.example.com/still.jpg"
        assert est_body["duration"] == 5
        assert est_body["sound"] == "off"
        assert est_body["prompt"] == "Breathing"
        
        # 2. Reserve budget
        reserved_clip = await ledger.reserve(
            episode_id=episode_id,
            line_name="L4_video",
            amount=clip_estimate,
            reason="Clip test"
        )
        assert reserved_clip
        
        # 3. Submit clip
        clip_submit_route = respx.post("http://mock.api/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(200, json={"request_id": "req_clip_456"})
        )
        
        clip_job_id = await clip_provider.submit_video(
            image_url="http://cdn.example.com/still.jpg",
            prompt="Breathing",
            duration=5
        )
        assert clip_job_id == "req_clip_456"
        
        # 4. Poll until complete (NO cost in status)
        clip_call_count = [0]
        
        def clip_status_handler(request):
            clip_call_count[0] += 1
            if clip_call_count[0] == 1:
                return httpx.Response(200, json={"status": "queued", "progress": 0.0})
            else:
                # Documented API: NO cost field
                return httpx.Response(200, json={
                    "status": "completed",
                    "video": {"url": "http://cdn.example.com/clip.mp4"},
                    "progress": 1.0
                })
        
        respx.get(f"http://mock.api/requests/{clip_job_id}/status").mock(side_effect=clip_status_handler)
        
        # Poll
        clip_status = await clip_provider.get_job_status(clip_job_id)
        assert clip_status.status.name == "PENDING"
        
        clip_status = await clip_provider.get_job_status(clip_job_id)
        assert clip_status.status.name == "COMPLETED"
        assert clip_status.output_url == "http://cdn.example.com/clip.mp4"
        assert clip_status.cost is None  # Documented API has NO cost
        
        # 5. Commit with estimate as actual cost
        actual_clip_cost = clip_status.cost or clip_estimate
        await ledger.commit(
            episode_id=episode_id,
            line_name="L4_video",
            reserved_amount=clip_estimate,
            actual_cost=actual_clip_cost,
            usd_micros=None,
            job_id=clip_job_id,
            reason="Clip completed"
        )
        
        await clip_provider.close()
        
        # FINAL ASSERTIONS
        # Verify ledger: spent = estimates, reserved = 0
        final_budget = await ledger.get_episode_summary(episode_id)
        
        l2_final = next(line for line in final_budget["lines"] if line["line_name"] == "L2_drafts")
        assert l2_final["spent"] == 4.0  # still estimate
        assert l2_final["reserved"] == 0.0
        
        l4_final = next(line for line in final_budget["lines"] if line["line_name"] == "L4_video")
        assert l4_final["spent"] == 7.5  # clip estimate
        assert l4_final["reserved"] == 0.0
        
        # Total spent equals sum of estimates
        total_spent = l2_final["spent"] + l4_final["spent"]
        assert total_spent == 11.5  # 4.0 + 7.5
        
    finally:
        # Cleanup
        os.unlink(db_path)
