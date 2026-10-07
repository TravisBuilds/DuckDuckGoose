"""
Higgsfield still image provider.

Implements the documented Higgsfield API:
- Authorization: Key <id>:<secret>
- POST /{model_path} → {"request_id": "..."}
- GET /requests/{request_id}/status → {"status": "queued"|"in_progress"|"completed"|"failed"|"nsfw"|"canceled", ...}
- Deterministic idempotency keys covering all request params
"""

import os
import uuid
import hashlib
from pathlib import Path
from typing import Any

import httpx

from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


class HiggsfieldStillProvider(GenerationProvider):
    """
    Higgsfield still generation provider.
    
    Uses documented API with Key authentication.
    """
    
    def __init__(
        self,
        api_key: str | None = None,
        model_path: str | None = None,
        base_url: str | None = None,
    ):
        """
        Initialize Higgsfield still provider.
        
        Args:
            api_key: Higgsfield API key in format 'id:secret' (default: from HIGGSFIELD_API_KEY)
            model_path: Model path (default: from MODEL_PATH_STILL or xai/grok-imagine-image-2.0)
            base_url: API base URL (default: from HIGGSFIELD_BASE_URL or https://api.higgsfield.ai)
        """
        api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        if not api_key:
            raise ValueError("HIGGSFIELD_API_KEY not set. Still generation will fail.")
        
        if ":" not in api_key:
            raise ValueError(
                "HIGGSFIELD_API_KEY must be in format 'id:secret' "
                "(e.g. 'hf_abc123:sk_xyz789')"
            )
        
        self.api_key_id, self.api_key_secret = api_key.split(":", 1)
        
        self.model_path = model_path or os.getenv(
            "MODEL_PATH_STILL", "xai/grok-imagine-image-2.0"
        )
        
        self.base_url = base_url or os.getenv(
            "HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai"
        )
        
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Key {self.api_key_id}:{self.api_key_secret}",
                "Content-Type": "application/json",
            },
            timeout=60.0,
        )
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
    
    def _generate_idempotency_key(
        self,
        prompt: str,
        model: str,
        resolution: str,
        quality: str,
        references: list[str] | None,
    ) -> str:
        """
        Generate deterministic idempotency key covering all request params.
        
        Format: hf-still-{sha256(model|prompt|resolution|quality|refs)[:16]}
        """
        refs_str = ",".join(sorted(references or []))
        key_input = f"{model}|{prompt}|{resolution}|{quality}|{refs_str}"
        hash_digest = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        return f"hf-still-{hash_digest}"
    
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
        
        API: POST /{model_path}
        Response: {"request_id": "..."}
        
        Args:
            prompt: Generation prompt
            model: Model path (default: self.model_path)
            resolution: Resolution (1k, 2k)
            quality: Quality (medium, high)
            references: List of reference image paths or media IDs
            idempotency_key: Idempotency key (default: deterministic based on params)
        
        Returns:
            request_id for polling
        """
        model = model or self.model_path
        
        # Generate deterministic idempotency key if not provided
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                prompt, model, resolution, quality, references
            )
        
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
        
        # POST /{model_path} per documented API
        response = await self.client.post(
            f"/{model}",
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
            "cost": 2.5,
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

    async def get_balance(self) -> float:
        """Get current Higgsfield credit balance."""
        response = await self.client.get("/v1/account/balance")
        response.raise_for_status()
        data = response.json()
        return data.get("balance", 0.0)
