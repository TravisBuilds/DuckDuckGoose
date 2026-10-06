"""
Kling video provider for image-to-video generation.

Uses Kling 3.0 Pro (sound off): kling-video/v3.0/pro/image-to-video
- Native 1080p
- 3-12s duration (cost scales linearly with length)
- Cost: ~1.5 credits/second
- Idempotency-Key on every call
"""

import os
import uuid
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
            api_key: Higgsfield API key (default: from HIGGSFIELD_API_KEY)
            base_url: API base URL (default: https://api.higgsfield.ai)
        """
        self.api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "HIGGSFIELD_API_KEY not set. Video generation will fail."
            )
        
        self.base_url = base_url or "https://api.higgsfield.ai"
        self.model_path = "kling-video/v3.0/pro/image-to-video"
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=120.0,  # Longer timeout for video
        )
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
    
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
            resolution: Resolution (1080p native)
        
        Returns:
            Estimated cost in Higgsfield app credits
        """
        # Kling 3.0 Pro: ~1.5 credits/second
        cost_per_second = 1.5
        
        # Clamp duration to valid range
        duration = max(3.0, min(12.0, duration))
        
        total = duration * cost_per_second
        
        # Round to 1 decimal
        return round(total, 1)
    
    async def submit_video(
        self,
        start_image: str,
        prompt: str,
        model: str | None = None,
        duration: float = 5.0,
        resolution: str = "1080p",
        draft: bool = False,
        references: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit video generation job.
        
        Args:
            start_image: Path to start image or media ID
            prompt: Generation prompt
            model: Model path (default: kling-video/v3.0/pro/image-to-video)
            duration: Video duration in seconds (3-12)
            resolution: Resolution (1080p)
            draft: Ignored for Kling (always pro quality)
            references: Additional reference images (not used for Kling)
            idempotency_key: Idempotency key for deduplication
        
        Returns:
            job_id for polling
        """
        model = model or self.model_path
        idempotency_key = idempotency_key or str(uuid.uuid4())
        
        # Clamp duration
        duration = max(3.0, min(12.0, duration))
        
        # Upload start image if needed
        if start_image.startswith("med_"):
            # Already a media ID
            start_image_id = start_image
        elif Path(start_image).exists():
            # Local file - upload it
            start_image_id = await self.upload_image(start_image)
        elif start_image.startswith("http"):
            # URL - need to import via API
            # For now, assume it's already accessible
            start_image_id = start_image
        else:
            raise ValueError(f"Invalid start_image: {start_image}")
        
        # Submit generation
        payload = {
            "model": model,
            "start_image": start_image_id,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "sound": False,  # Sound off (as per requirements)
        }
        
        headers = {"Idempotency-Key": idempotency_key}
        
        response = await self.client.post(
            "/v1/generate/video",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        
        data = response.json()
        return data["job_id"]
    
    async def get_job_status(self, job_id: str) -> ProviderJob:
        """
        Get status of a generation job.
        
        Args:
            job_id: Job identifier
        
        Returns:
            ProviderJob with status, output_url, cost, error
        """
        response = await self.client.get(f"/v1/jobs/{job_id}")
        response.raise_for_status()
        
        data = response.json()
        status_str = data.get("status", "pending").lower()
        
        # Map Higgsfield status to ProviderJobStatus
        if status_str == "completed":
            status = ProviderJobStatus.COMPLETED
        elif status_str == "failed":
            status = ProviderJobStatus.FAILED
        elif status_str == "blocked" or status_str == "nsfw":
            status = ProviderJobStatus.BLOCKED
        elif status_str == "processing" or status_str == "running":
            status = ProviderJobStatus.PROCESSING
        else:
            status = ProviderJobStatus.PENDING
        
        return ProviderJob(
            job_id=job_id,
            status=status,
            progress=data.get("progress", 0.0),
            output_url=data.get("output_url"),
            cost=data.get("cost", 0.0),
            error=data.get("error"),
        )

    async def get_balance(self) -> float:
        """Get current Higgsfield credit balance."""
        response = await self.client.get("/v1/account/balance")
        response.raise_for_status()
        data = response.json()
        return data.get("balance", 0.0)
