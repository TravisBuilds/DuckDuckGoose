"""
Higgsfield still image provider using xai/grok-imagine-image-2.0.

Model: xai/grok-imagine-image-2.0 (default)
- 1k resolution, quality medium
- Up to 3 reference images (duck refs + location plate)
- Cost: ~9 Higgsfield credits per still
- Idempotency-Key on every call
- Pre-spend budget check via cost estimate

Fallback: alibaba/qwen-image-3/edit
"""

import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


class HiggsfieldStillProvider(GenerationProvider):
    """
    Higgsfield still generation provider.
    
    Uses xai/grok-imagine-image-2.0 by default.
    Fallback: alibaba/qwen-image-3/edit
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        model_path: str | None = None,
        fallback_model: str | None = None,
        base_url: str | None = None,
    ):
        """
        Initialize Higgsfield still provider.
        
        Args:
            api_key: Higgsfield API key (default: from HIGGSFIELD_API_KEY)
            model_path: Model path (default: xai/grok-imagine-image-2.0)
            fallback_model: Fallback model (default: alibaba/qwen-image-3/edit)
            base_url: API base URL (default: https://api.higgsfield.ai)
        """
        self.api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "HIGGSFIELD_API_KEY not set. Still generation will fail."
            )
        
        self.model_path = model_path or os.getenv(
            "MODEL_PATH_STILL", "xai/grok-imagine-image-2.0"
        )
        self.fallback_model = fallback_model or os.getenv(
            "MODEL_PATH_STILL_FALLBACK", "alibaba/qwen-image-3/edit"
        )
        
        self.base_url = base_url or "https://api.higgsfield.ai"
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
    
    async def upload_reference(self, image_path: str | Path) -> str:
        """
        Upload reference image and return media ID.
        
        Args:
            image_path: Path to reference image
        
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
        prompt: str,
        model: str,
        resolution: str = "1k",
        quality: str = "medium",
        num_refs: int = 0,
    ) -> float:
        """
        Estimate cost before submission.
        
        Args:
            prompt: Generation prompt
            model: Model path
            resolution: Resolution (1k, 2k)
            quality: Quality (medium, high)
            num_refs: Number of reference images
        
        Returns:
            Estimated cost in Higgsfield app credits
        """
        # Cost structure (from credit plan):
        # - Base: ~4 credits for 1k medium
        # - Per ref: ~1 credit
        # - Quality multiplier: high = 1.5x
        # - Resolution multiplier: 2k = 2x
        
        base_cost = 4.0
        
        if quality == "high":
            base_cost *= 1.5
        
        if resolution == "2k":
            base_cost *= 2.0
        
        ref_cost = num_refs * 1.0
        
        total = base_cost + ref_cost
        
        # Round to 1 decimal
        return round(total, 1)
    
    async def submit_image(
        self,
        prompt: str,
        model: str | None = None,
        resolution: str = "1k",
        quality: str = "medium",
        references: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit still image generation job.
        
        Args:
            prompt: Generation prompt
            model: Model path (default: self.model_path)
            resolution: Resolution (1k, 2k)
            quality: Quality (medium, high)
            references: List of reference image paths or media IDs
            idempotency_key: Idempotency key for deduplication
        
        Returns:
            job_id for polling
        """
        model = model or self.model_path
        idempotency_key = idempotency_key or str(uuid.uuid4())
        
        # Upload references if needed
        media_ids = []
        if references:
            for ref in references[:3]:  # Max 3 refs
                if ref.startswith("med_"):
                    # Already a media ID
                    media_ids.append(ref)
                elif Path(ref).exists():
                    # Local file - upload it
                    media_id = await self.upload_reference(ref)
                    media_ids.append(media_id)
        
        # Submit generation
        payload = {
            "model": model,
            "prompt": prompt,
            "resolution": resolution,
            "quality": quality,
        }
        
        if media_ids:
            payload["references"] = media_ids
        
        headers = {"Idempotency-Key": idempotency_key}
        
        response = await self.client.post(
            "/v1/generate/image",
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
