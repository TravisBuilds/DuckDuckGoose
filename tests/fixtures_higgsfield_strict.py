"""
Strict Higgsfield API mock fixture matching ONLY the documented contract.

Based on docs.higgsfield.ai for:
- xai/grok-imagine-image-2.0 (still image generation)
- kling-video/v3.0/pro/image-to-video (video generation)

Enforces:
- Estimate returns {"credits": "<str>", "usd": "<str>"} - NO estimated_cost
- Status returns NO cost or estimated_cost fields
- Unknown/extra fields in request → 422
- Missing required fields → 422
"""

import httpx
import respx
from typing import Dict, Any


def strict_higgsfield_mock():
    """
    Create a strict mock that enforces the documented Higgsfield API contract.
    
    Returns a respx router with strict validation.
    """
    
    # Allowed request fields per endpoint
    STILL_REQUEST_FIELDS = {"prompt", "resolution", "quality", "image_urls", "num_references"}
    STILL_ESTIMATE_FIELDS = {"prompt", "resolution", "quality", "num_references"}
    VIDEO_REQUEST_FIELDS = {"image_url", "duration", "sound", "prompt"}
    VIDEO_ESTIMATE_FIELDS = {"image_url", "duration", "sound", "prompt"}
    
    def validate_fields(body: Dict[str, Any], allowed: set) -> None:
        """Validate that only allowed fields are present."""
        extra = set(body.keys()) - allowed
        if extra:
            raise ValueError(f"Unknown fields: {extra}")
    
    # Still image estimate - returns credits as string
    @respx.route(method="POST", path__regex=r"/estimate/xai/grok-imagine-image-2\.0")
    def still_estimate_handler(request):
        try:
            import json
            body = json.loads(request.content)
            validate_fields(body, STILL_ESTIMATE_FIELDS)
            
            # Documented response format
            return httpx.Response(200, json={
                "credits": "4.0",
                "usd": "0.04"
            })
        except (ValueError, KeyError) as e:
            return httpx.Response(422, json={"error": str(e)})
    
    # Video estimate - requires full documented request
    @respx.route(method="POST", path__regex=r"/estimate/kling-video/v3\.0/pro/image-to-video")
    def video_estimate_handler(request):
        try:
            import json
            body = json.loads(request.content)
            validate_fields(body, VIDEO_ESTIMATE_FIELDS)
            
            # Required fields
            if "image_url" not in body:
                return httpx.Response(422, json={"error": "Missing required field: image_url"})
            if "duration" not in body:
                return httpx.Response(422, json={"error": "Missing required field: duration"})
            if not isinstance(body["duration"], int):
                return httpx.Response(422, json={"error": "duration must be integer"})
            if "sound" not in body:
                return httpx.Response(422, json={"error": "Missing required field: sound"})
            
            # Calculate based on duration
            duration = body["duration"]
            credits = duration * 1.5
            
            # Documented response format
            return httpx.Response(200, json={
                "credits": str(credits),
                "usd": f"{credits * 0.01:.2f}"
            })
        except (ValueError, KeyError) as e:
            return httpx.Response(422, json={"error": str(e)})
    
    # Still image generation
    @respx.route(method="POST", path__regex=r"/xai/grok-imagine-image-2\.0")
    def still_submit_handler(request):
        try:
            import json
            body = json.loads(request.content)
            validate_fields(body, STILL_REQUEST_FIELDS)
            
            # Required fields
            if "prompt" not in body:
                return httpx.Response(422, json={"error": "Missing required field: prompt"})
            if "resolution" in body and body["resolution"] not in ("1k", "2k"):
                return httpx.Response(422, json={"error": "resolution must be 1k or 2k"})
            if "quality" in body and body["quality"] not in ("low", "medium"):
                return httpx.Response(422, json={"error": "quality must be low or medium"})
            
            # Success
            return httpx.Response(200, json={"request_id": "req_still_test_123"})
        except (ValueError, KeyError) as e:
            return httpx.Response(422, json={"error": str(e)})
    
    # Video generation
    @respx.route(method="POST", path__regex=r"/kling-video/v3\.0/pro/image-to-video")
    def video_submit_handler(request):
        try:
            import json
            body = json.loads(request.content)
            validate_fields(body, VIDEO_REQUEST_FIELDS)
            
            # Required fields
            if "image_url" not in body:
                return httpx.Response(422, json={"error": "Missing required field: image_url"})
            if "duration" not in body:
                return httpx.Response(422, json={"error": "Missing required field: duration"})
            if not isinstance(body["duration"], int):
                return httpx.Response(422, json={"error": "duration must be integer"})
            if "sound" not in body:
                return httpx.Response(422, json={"error": "Missing required field: sound"})
            
            # Success
            return httpx.Response(200, json={"request_id": "req_video_test_456"})
        except (ValueError, KeyError) as e:
            return httpx.Response(422, json={"error": str(e)})
    
    # Status polling - NO cost field in documented response
    @respx.route(method="GET", path__regex=r"/requests/([^/]+)/status")
    def status_handler(request, request_id):
        # Simulate progression: first call pending, second completed
        if not hasattr(status_handler, 'calls'):
            status_handler.calls = {}
        
        call_count = status_handler.calls.get(request_id, 0) + 1
        status_handler.calls[request_id] = call_count
        
        if call_count == 1:
            # First poll: pending
            return httpx.Response(200, json={
                "status": "queued",
                "progress": 0.0
            })
        else:
            # Subsequent polls: completed
            # Documented response has NO cost field
            if request_id.startswith("req_still"):
                return httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "http://cdn.example.com/output.jpg"}],
                    "progress": 1.0
                })
            else:
                return httpx.Response(200, json={
                    "status": "completed",
                    "video": {"url": "http://cdn.example.com/output.mp4"},
                    "progress": 1.0
                })
    
    return {
        "still_estimate": still_estimate_handler,
        "video_estimate": video_estimate_handler,
        "still_submit": still_submit_handler,
        "video_submit": video_submit_handler,
        "status": status_handler,
    }


def setup_strict_mock():
    """
    Set up the strict mock and return handlers for verification.
    
    Usage:
        @pytest.mark.asyncio
        @respx.mock
        async def test_something():
            handlers = setup_strict_mock()
            # ... test code ...
    """
    return strict_higgsfield_mock()
