"""
Kling video provider for image-to-video generation.

Implements the documented Higgsfield API:
- Authorization: Key <id>:<secret>
- POST /{model_path} → {"request_id": "..."}
- GET /requests/{request_id}/status
- Deterministic idempotency keys covering all request params (including start_image)

Uses Kling 3.0 Pro (sound off): kling-video/v3.0/pro/image-to-video
- Native 1080p
- 3-12s duration (cost scales linearly with length)
- Cost: ~1.5 credits/second
"""

import os
import uuid
import hashlib
from pathlib import Path
from typing import Any

import httpx

from hfvg.pricing import estimate_from_response, rate_table_estimate
from hfvg.providers.common import apply_declined_preset
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


class KlingVideoProvider(GenerationProvider):
    """
    Kling 3.0 Pro video generation provider via Higgsfield.
    
    Routing: kling-video/v3.0/pro/image-to-video
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        """
        Initialize Kling video provider.
        
        Args:
            api_key: Higgsfield API key in format 'id:secret' (default: from HIGGSFIELD_API_KEY)
            base_url: API base URL (default: from HIGGSFIELD_BASE_URL or https://api.higgsfield.ai)
        """
        api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        if not api_key:
            raise ValueError("HIGGSFIELD_API_KEY not set. Video generation will fail.")
        
        if ":" not in api_key:
            raise ValueError(
                "HIGGSFIELD_API_KEY must be in format 'id:secret' "
                "(e.g. 'hf_abc123:sk_xyz789')"
            )
        
        self.api_key_id, self.api_key_secret = api_key.split(":", 1)
        
        self.base_url = base_url or os.getenv(
            "HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai"
        )
        self.model_path = "kling-video/v3.0/pro/image-to-video"
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Key {self.api_key_id}:{self.api_key_secret}",
                "Content-Type": "application/json",
            },
            timeout=120.0,  # Longer timeout for video
        )
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
    
    def _generate_idempotency_key(
        self,
        image_url: str,
        prompt: str,
        duration: int,
        end_image_url: str | None = None,
    ) -> str:
        """
        Generate deterministic idempotency key covering all request params.
        
        Format: hf-clip-{sha256(model|img|end_img|prompt_hash|dur)[:16]}
        """
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        key_input = f"{self.model_path}|{image_url}|{end_image_url or ''}|{prompt_hash}|{duration}"
        hash_digest = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        return f"hf-clip-{hash_digest}"
    
    async def upload_reference(self, image_path: str | Path, version: int = 1) -> dict:
        """
        Upload reference image and return upload result with URL.
        
        Before upload, normalizes the image (JPEG, downscale, compress).
        Uses upload cache to avoid re-uploading the same file.
        
        Args:
            image_path: Path to reference image (will be normalized before upload)
            version: Version number for cache key (default: 1)
        
        Returns:
            dict with 'public_url' (the CDN URL for the uploaded file)
        """
        import mimetypes
        from hfvg.upload_cache import UploadCache
        from hfvg.image_normalize import normalize_reference_image
        
        # Normalize image first (convert to JPEG, downscale, compress)
        normalized_path, original_sha = normalize_reference_image(Path(image_path))
        upload_path = normalized_path
        
        # Check upload cache first
        cache = UploadCache()
        await cache.init_db()
        
        cached_url = await cache.get_cached_url(str(upload_path), version)
        if cached_url:
            return {"public_url": cached_url}
        
        # Not in cache - upload the normalized file
        
        # Normalized images are always JPEG
        content_type = "image/jpeg"
        
        # Get upload URL (REQUIRES content_type)
        response = await self.client.post(
            "/files/generate-upload-url",
            json={"content_type": content_type}
        )
        response.raise_for_status()
        data = response.json()
        upload_url = data["upload_url"]
        public_url = data["public_url"]
        upload_headers = data["upload_headers"]
        
        # Upload normalized file with exact headers from API (includes x-amz-tagging)
        with open(upload_path, "rb") as f:
            image_bytes = f.read()
        
        # Use a plain httpx client for the PUT (no auth, just upload_headers)
        async with httpx.AsyncClient() as plain_client:
            upload_response = await plain_client.put(
                upload_url,
                content=image_bytes,
                headers=upload_headers
            )
            upload_response.raise_for_status()
        
        # Cache the uploaded URL
        await cache.cache_url(str(upload_path), version, public_url)
        
        return {"public_url": public_url}
    
    async def upload_image(self, image_path: str | Path) -> str:
        """
        Upload start image and return public URL (legacy method).
        
        Args:
            image_path: Path to start image
        
        Returns:
            public_url for use in generation
        """
        result = await self.upload_reference(image_path, version=1)
        return result["public_url"]
    
    async def _estimate_response(
        self, image_url: str, prompt: str, duration: int, end_image_url: str | None = None
    ) -> dict:
        """POST /estimate/{model_path} mirroring the full submit request (fail closed)."""
        payload = {
            "image_url": image_url,
            "duration": duration,
            "sound": "off",
            "prompt": prompt,
        }
        if end_image_url:
            payload["last_image_url"] = end_image_url
        try:
            response = await self.client.post(
                f"/estimate/{self.model_path}",
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            raise ValueError(f"Failed to get estimate for video generation: {e}") from e

    async def estimate_cost(
        self,
        image_url: str,
        prompt: str,
        duration: int,
    ) -> float:
        """
        Estimate cost in Higgsfield credits (informational; the ledger uses USD).

        API: POST /estimate/{model_path}; Response: {"credits": "<str>", "usd": "<str>"}

        Raises:
            ValueError: If estimate fails or response is invalid (fail closed)
        """
        data = await self._estimate_response(image_url, prompt, duration)
        try:
            if "credits" not in data:
                raise ValueError(f"Estimate response missing 'credits' field: {data}")
            credits_str = data["credits"]
            if not isinstance(credits_str, str):
                raise ValueError(f"Estimate 'credits' must be string, got {type(credits_str)}")
            return float(credits_str)
        except (ValueError, KeyError) as e:
            raise ValueError(f"Failed to get estimate for video generation: {e}") from e

    async def estimate_usd_micros(
        self, image_url: str, prompt: str, duration: int, end_image_url: str | None = None
    ) -> int:
        """
        Estimate cost in integer micro-dollars.

        Source of truth: the estimate's ``usd`` value. If the response carries no USD value, fall
        back to the configured per-second rate table (list price before discount, derived).
        """
        data = await self._estimate_response(image_url, prompt, duration, end_image_url)
        try:
            usd_micros = estimate_from_response(data)
            if usd_micros is None:
                usd_micros = rate_table_estimate("kling-3.0-pro", None, duration).usd_micros
            if usd_micros <= 0:
                raise ValueError(f"Non-positive USD estimate: {data}")
            return usd_micros
        except (ValueError, TypeError) as e:
            raise ValueError(f"Failed to get USD estimate for video generation: {e}") from e

    async def estimate_usd_and_credits(
        self, image_url: str, prompt: str, duration: int, end_image_url: str | None = None
    ) -> tuple[int, float]:
        """
        Estimate cost in both USD micro-dollars and credits from the provider's estimate.

        Returns:
            (usd_micros, credits) tuple
        """
        data = await self._estimate_response(image_url, prompt, duration, end_image_url)
        try:
            usd_micros = estimate_from_response(data)
            if usd_micros is None:
                usd_micros = rate_table_estimate("kling-3.0-pro", None, duration).usd_micros
            if usd_micros <= 0:
                raise ValueError(f"Non-positive USD estimate: {data}")
            
            # Extract credits from estimate response, or use table
            credits_str = data.get("credits")
            if credits_str is not None:
                credits = float(credits_str)
            else:
                # Fall back to table: ~1.5 credits/second for kling pro
                from hfvg.credit_pricing import estimate_clip_credits
                credits = estimate_clip_credits("kling_3.0", duration)
            
            if credits <= 0:
                raise ValueError(f"Non-positive credits estimate: {data}")
            
            return (usd_micros, credits)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Failed to get USD/credits estimate for video generation: {e}") from e


    async def submit_video(
        self,
        image_url: str,
        prompt: str,
        duration: int = 5,
        idempotency_key: str | None = None,
        end_image_url: str | None = None,
    ) -> str:
        """
        Submit video generation job.
        
        API: POST /{model_path}
        Request: {"image_url": "...", "duration": 5, "sound": "off", "prompt": "..."}
        Response: {"request_id": "..."}
        
        Args:
            image_url: Public URL of start image
            prompt: Generation prompt
            duration: Video duration in seconds (3-12), integer
            idempotency_key: Idempotency key (default: deterministic based on params)
        
        Returns:
            request_id for polling
        """
        # Generate deterministic idempotency key if not provided
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                image_url, prompt, duration, end_image_url
            )
        
        # Submit generation with documented API format
        payload = {
            "image_url": image_url,
            "duration": duration,  # Integer as required
            "sound": "off",  # Required for cost predictability
            "prompt": prompt,
        }
        if end_image_url:
            # Higgsfield's public Kling 3.0 docs name the optional final frame `last_image_url`
            # (NOT end_image_url): see docs/PROVIDERS.md.
            payload["last_image_url"] = end_image_url
        apply_declined_preset(payload)
        
        headers = {"Idempotency-Key": idempotency_key}
        
        # POST /{model_path} per documented API
        response = await self.client.post(
            f"/{self.model_path}",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        
        data = response.json()
        # Documented API returns "request_id"
        return data["request_id"]
    
    async def get_job_status(self, request_id: str) -> ProviderJob:
        """
        Get status of a generation job.
        
        API: GET /requests/{request_id}/status
        Response: {
            "status": "queued"|"in_progress"|"completed"|"failed"|"nsfw"|"canceled",
            "video": {"url": "..."},
            "cost": 7.5,
            "error": "..."
        }
        
        Args:
            request_id: Request identifier from submit
        
        Returns:
            ProviderJob with status, output_url, cost, error
        """
        # GET /requests/{request_id}/status per documented API
        response = await self.client.get(f"/requests/{request_id}/status")
        response.raise_for_status()
        
        data = response.json()
        status_str = data.get("status", "queued").lower()
        
        # Map Higgsfield API status to ProviderJobStatus
        if status_str == "completed":
            status = ProviderJobStatus.COMPLETED
        elif status_str == "failed":
            status = ProviderJobStatus.FAILED
        elif status_str in ("blocked", "nsfw", "canceled"):
            status = ProviderJobStatus.BLOCKED
        elif status_str == "in_progress":
            status = ProviderJobStatus.PROCESSING
        else:  # "queued"
            status = ProviderJobStatus.PENDING
        
        # Parse URL from documented response format: video.url
        output_url = None
        if "video" in data and isinstance(data["video"], dict):
            output_url = data["video"].get("url")
        
        # Parse cost from response, or None if not available
        cost = data.get("cost")
        
        return ProviderJob(
            job_id=request_id,
            status=status,
            progress=data.get("progress", 0.0),
            output_url=output_url,
            cost=cost,
            error=data.get("error"),
        )
