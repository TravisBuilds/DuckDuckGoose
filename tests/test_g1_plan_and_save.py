"""Tests for G1 credit plan computation and local file saving."""

import asyncio
import pytest
import respx
import httpx
from pathlib import Path
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.activities.g1_plan import compute_g1_plan_activity
from hfvg.activities.save_assets import save_asset_locally


@pytest.mark.asyncio
async def test_g1_plan_computes_per_shot_and_total_estimates(monkeypatch):
    """Test that G1 plan computes per-shot and total credits/USD from provider estimates."""
    
    # Mock Higgsfield still estimate
    with respx.mock:
        still_estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "0.06", "usd": "0.06"})
        )
        
        # Mock Kling clip estimate
        clip_estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(200, json={"credits": "0.25", "usd": "0.25"})
        )
        
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
        
        shots = [
            {"shot_id": "A01", "prompt": "Shot A01", "params": {"duration": 5.0, "model": "kling"}},
            {"shot_id": "A02", "prompt": "Shot A02", "params": {"duration": 5.0, "model": "kling"}},
        ]
        
        plan = await compute_g1_plan_activity("ep99", shots, dry_run=False)
        
        # Verify per-shot estimates
        assert len(plan["shots"]) == 2
        assert plan["shots"][0]["shot_id"] == "A01"
        assert plan["shots"][0]["still_credits"] == 0.06
        assert plan["shots"][0]["clip_credits"] == 0.25
        assert plan["shots"][0]["total_credits"] == 0.31
        
        # Verify totals
        assert plan["total_still_credits"] == 0.12  # 0.06 * 2
        assert plan["total_clip_credits"] == 0.50   # 0.25 * 2
        assert plan["total_credits"] == 0.62        # 0.12 + 0.50
        assert plan["total_usd"] == "0.62"
        
        # Verify estimates were called
        assert still_estimate_route.call_count == 2  # Once per shot
        assert clip_estimate_route.call_count == 2


@pytest.mark.asyncio
async def test_g1_plan_marks_estimate_unavailable_on_failure(monkeypatch):
    """Test that G1 plan marks estimates as unavailable when provider estimates fail (no silent defaults)."""
    
    # Mock estimate endpoints to fail
    with respx.mock:
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(500, json={"error": "Service unavailable"})
        )
        respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(500, json={"error": "Service unavailable"})
        )
        
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
        
        shots = [
            {"shot_id": "A01", "prompt": "Shot A01", "params": {"duration": 5.0, "model": "kling"}},
        ]
        
        plan = await compute_g1_plan_activity("ep99", shots, dry_run=False)
        
        # Should NOT fall back to defaults (as per user requirement)
        # Mark estimates as unavailable instead
        assert plan["shots"][0]["still_credits"] == 0.0
        assert plan["shots"][0]["still_estimate_available"] is False
        assert plan["shots"][0]["clip_credits"] == 0.0
        assert plan["shots"][0]["clip_estimate_available"] is False
        assert plan["total_credits"] == 0.0


@pytest.mark.asyncio
async def test_save_asset_locally_downloads_and_saves_still():
    """Test that save_asset_locally downloads a still and saves it to the correct path."""
    
    with respx.mock:
        # Mock asset URL
        asset_url = "http://example.com/still.jpg"
        asset_content = b"fake-still-image-data"
        
        respx.get(asset_url).mock(
            return_value=httpx.Response(
                200,
                content=asset_content,
                headers={"content-type": "image/jpeg"}
            )
        )
        
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            os.chdir(tmpdir)
            
            # Save still
            local_path = await save_asset_locally("ep99", "A01", asset_url, "still", attempt=1)
            
            # Verify path format
            expected_path = Path("data/episodes/ep99/outputs/A01/still_v1.jpg")
            assert Path(local_path) == expected_path
            
            # Verify file exists and contains correct data
            assert expected_path.exists()
            assert expected_path.read_bytes() == asset_content


@pytest.mark.asyncio
async def test_save_asset_locally_downloads_and_saves_clip():
    """Test that save_asset_locally downloads a clip and saves it to the correct path."""
    
    with respx.mock:
        # Mock asset URL
        asset_url = "http://example.com/clip.mp4"
        asset_content = b"fake-clip-video-data"
        
        respx.get(asset_url).mock(
            return_value=httpx.Response(
                200,
                content=asset_content,
                headers={"content-type": "video/mp4"}
            )
        )
        
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            os.chdir(tmpdir)
            
            # Save clip
            local_path = await save_asset_locally("ep99", "A01", asset_url, "clip", attempt=1)
            
            # Verify path format
            expected_path = Path("data/episodes/ep99/outputs/A01/clip_v1.mp4")
            assert Path(local_path) == expected_path
            
            # Verify file exists and contains correct data
            assert expected_path.exists()
            assert expected_path.read_bytes() == asset_content


@pytest.mark.asyncio
async def test_save_asset_locally_detects_extension_from_content_type():
    """Test that save_asset_locally uses the correct extension from Content-Type header."""
    
    with respx.mock:
        # Mock asset URL with .webp content
        asset_url = "http://example.com/still"
        asset_content = b"fake-webp-data"
        
        respx.get(asset_url).mock(
            return_value=httpx.Response(
                200,
                content=asset_content,
                headers={"content-type": "image/webp"}
            )
        )
        
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            os.chdir(tmpdir)
            
            # Save still
            local_path = await save_asset_locally("ep99", "A01", asset_url, "still", attempt=1)
            
            # Should use .webp extension from Content-Type
            expected_path = Path("data/episodes/ep99/outputs/A01/still_v1.webp")
            assert Path(local_path) == expected_path
            assert expected_path.exists()


@pytest.mark.asyncio
async def test_save_asset_locally_handles_multiple_attempts():
    """Test that save_asset_locally creates separate files for different attempts."""
    
    with respx.mock:
        asset_url = "http://example.com/still.jpg"
        asset_content_v1 = b"fake-still-v1"
        asset_content_v2 = b"fake-still-v2"
        
        # Mock different responses for different attempts
        call_count = {"count": 0}
        
        def mock_response(request):
            call_count["count"] += 1
            content = asset_content_v1 if call_count["count"] == 1 else asset_content_v2
            return httpx.Response(200, content=content, headers={"content-type": "image/jpeg"})
        
        respx.get(asset_url).mock(side_effect=mock_response)
        
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            import os
            os.chdir(tmpdir)
            
            # Save v1
            path_v1 = await save_asset_locally("ep99", "A01", asset_url, "still", attempt=1)
            
            # Save v2
            path_v2 = await save_asset_locally("ep99", "A01", asset_url, "still", attempt=2)
            
            # Both should exist with different paths
            assert Path(path_v1).name == "still_v1.jpg"
            assert Path(path_v2).name == "still_v2.jpg"
            assert Path(path_v1).read_bytes() == asset_content_v1
            assert Path(path_v2).read_bytes() == asset_content_v2


@pytest.mark.asyncio
async def test_g1_plan_dry_run_no_real_api_calls(monkeypatch):
    """Test that G1 plan in dry run mode doesn't make real /estimate API calls."""
    
    # Mock estimate endpoints - they should NOT be called in dry run
    with respx.mock:
        still_estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "0.06", "usd": "0.06"})
        )
        clip_estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(200, json={"credits": "0.25", "usd": "0.25"})
        )
        
        import os
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
        
        shots = [
            {"shot_id": "A01", "prompt": "Shot A01", "params": {"duration": 5.0, "model": "kling"}},
            {"shot_id": "A02", "prompt": "Shot A02", "params": {"duration": 5.0, "model": "kling"}},
        ]
        
        # Call with dry_run=True
        plan = await compute_g1_plan_activity("ep99", shots, dry_run=True)
        
        # Should return placeholder estimates
        assert len(plan["shots"]) == 2
        assert plan["shots"][0]["shot_id"] == "A01"
        assert plan["shots"][0]["still_credits"] == 1.0
        assert plan["shots"][0]["clip_credits"] == 5.0
        
        # Verify NO API calls were made
        assert still_estimate_route.call_count == 0
        assert clip_estimate_route.call_count == 0
        
        # Verify estimates_available flag is True (placeholders are "available")
        assert plan["estimates_available"] is True


@pytest.mark.asyncio
async def test_g1_plan_unavailable_flag_when_estimates_fail(monkeypatch):
    """Test that G1 plan sets estimates_available=False when any estimate fails."""
    
    # Mock: still estimate succeeds, clip estimate fails
    with respx.mock:
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "0.06", "usd": "0.06"})
        )
        respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(500, json={"error": "Service unavailable"})
        )
        
        import os
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
        
        shots = [
            {"shot_id": "A01", "prompt": "Shot A01", "params": {"duration": 5.0, "model": "kling"}},
        ]
        
        plan = await compute_g1_plan_activity("ep99", shots, dry_run=False)
        
        # Should mark clip estimate as unavailable
        assert plan["shots"][0]["still_estimate_available"] is True
        assert plan["shots"][0]["clip_estimate_available"] is False
        
        # Total should have unavailable flag
        assert plan["estimates_available"] is False, "estimates_available must be False when any estimate fails"


@pytest.mark.asyncio
async def test_g1_plan_clip_estimate_no_placeholder_url(monkeypatch):
    """Test that clip estimate doesn't use a placeholder image URL."""
    
    captured_requests = []
    
    def capture_request(request):
        captured_requests.append(request)
        return httpx.Response(200, json={"credits": "0.25", "usd": "0.25"})
    
    with respx.mock:
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "0.06", "usd": "0.06"})
        )
        clip_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            side_effect=capture_request
        )
        
        import os
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
        
        shots = [
            {"shot_id": "A01", "prompt": "Shot A01", "params": {"duration": 5.0, "model": "kling"}},
        ]
        
        plan = await compute_g1_plan_activity("ep99", shots, dry_run=False)
        
        # Verify estimate was called
        assert clip_route.call_count == 1
        
        # Verify request body doesn't contain placeholder URL
        request_body = captured_requests[0].content.decode()
        assert "placeholder" not in request_body.lower(), "Clip estimate should not use placeholder image URL"
        assert "example.com" not in request_body.lower(), "Clip estimate should not use example.com placeholder"
