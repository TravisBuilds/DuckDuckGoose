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
        start_image: str,
        prompt: str,
        duration: float,
        resolution: str,
        draft: bool,
    ) -> str:
        """
        Generate deterministic idempotency key covering all request params.
        
        Format: hf-clip-{sha256(img|prompt|dur|res|draft)[:16]}
        """
        key_input = f"{start_image}|{prompt}|{duration}|{resolution}|{draft}"
        hash_digest = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        return f"hf-clip-{hash_digest}"
    
    async def upload_image(self, image_path: str | Path) -> str:
        """
        Upload start image and return media ID.
        
        Args:
            image_path: Path to start image
        
        Returns:
            media_id for use in generation
        """
        # Get upload URL
        response = await self.client.post(
            "/files/generate-upload-url",
            json={"filename": Path(image_path).name}
        )
        response.raise_for_status()
        data = response.json()
        upload_url = data["upload_url"]
        media_id = data["media_id"]
        
        # Upload file
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        
        # Use a plain httpx client for the PUT (no auth needed)
        async with httpx.AsyncClient() as plain_client:
            upload_response = await plain_client.put(upload_url, content=image_bytes)
            upload_response.raise_for_status()
        
        return media_id
    
    async def estimate_cost(
        self,
        duration: float,
        resolution: str = "1080p",
    ) -> float:
        """
        Estimate cost before submission.
        
        Args:
            duration: Video duration in seconds (3-12)
            resolution: Resolution (default 1080p)
        
        Returns:
            Estimated cost in Higgsfield app credits
        """
        # Kling 3.0 Pro pricing: 1.5 credits/second
        cost_per_second = 1.5
        
        total = duration * cost_per_second
        
        # Round to 1 decimal
        return round(total, 1)
    
    async def submit_video(
        self,
        start_image: str,
        prompt: str,
        duration: float = 5.0,
        resolution: str = "1080p",
        draft: bool = False,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit video generation job.
        
        API: POST /{model_path}
        Response: {"request_id": "..."}
        
        Args:
            start_image: URL or path to start image
            prompt: Generation prompt
            duration: Video duration in seconds (3-12)
            resolution: Resolution (default 1080p)
            draft: Draft mode (faster, lower quality)
            idempotency_key: Idempotency key (default: deterministic based on params)
        
        Returns:
            request_id for polling
        """
        # Generate deterministic idempotency key if not provided
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                start_image, prompt, duration, resolution, draft
            )
        
        # Upload start image if it's a local path
        if Path(start_image).exists():
            start_image = await self.upload_image(start_image)
        
        # Submit generation
        payload = {
            "start_image": start_image,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "draft": draft,
        }
        
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
            "output_url": "...",
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
        
        return ProviderJob(
            job_id=request_id,
            status=status,
            progress=data.get("progress", 0.0),
            output_url=data.get("output_url"),
            cost=data.get("cost", 0.0),
            error=data.get("error"),
        )
