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
        resolution: str,
        quality: str,
        image_urls: list[str] | None,
    ) -> str:
        """
        Generate deterministic idempotency key covering all request params.
        
        Format: hf-still-{sha256(prompt|resolution|quality|refs)[:16]}
        """
        refs_str = ",".join(sorted(image_urls or []))
        key_input = f"{prompt}|{resolution}|{quality}|{refs_str}"
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
        resolution: str = "1k",
        quality: str = "medium",
        num_refs: int = 0,
    ) -> float:
        """
        Estimate cost before submission via API.
        
        API: POST /estimate/{model_path}
        
        Args:
            prompt: Generation prompt
            resolution: Resolution (1k, 2k)
            quality: Quality (low, medium only - no high)
            num_refs: Number of reference images
        
        Returns:
            Estimated cost in Higgsfield app credits
        """
        # Map quality to allowed values (low, medium)
        if quality not in ("low", "medium"):
            quality = "medium"
        
        # Use the estimate API endpoint
        payload = {
            "prompt": prompt,
            "resolution": resolution,
            "quality": quality,
        }
        if num_refs > 0:
            payload["num_references"] = num_refs
        
        response = await self.client.post(
            f"/estimate/{self.model_path}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        
        # Return estimated cost from API
        return data.get("estimated_cost", 4.0)  # Fallback to 4.0
    
    async def submit_image(
        self,
        prompt: str,
        resolution: str = "1k",
        quality: str = "medium",
        image_urls: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit still image generation job.
        
        API: POST /{model_path}
        Request: {"prompt": "...", "resolution": "1k", "quality": "medium", "image_urls": [...]}
        Response: {"request_id": "..."}
        
        Args:
            prompt: Generation prompt
            resolution: Resolution (1k, 2k)
            quality: Quality (low, medium only - no high)
            image_urls: List of public reference image URLs (max 3)
            idempotency_key: Idempotency key (default: deterministic based on params)
        
        Returns:
            request_id for polling
        """
        # Map quality to allowed values (low, medium)
        if quality not in ("low", "medium"):
            quality = "medium"
        
        # Generate deterministic idempotency key if not provided
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                prompt, resolution, quality, image_urls
            )
        
        # Submit generation with documented API format
        payload = {
            "prompt": prompt,
            "resolution": resolution,
            "quality": quality,
        }
        
        if image_urls:
            # Use image_urls as documented, limit to 3
            payload["image_urls"] = image_urls[:3]
        
        headers = {"Idempotency-Key": idempotency_key}
        
        # POST /{model_path} per documented API (no model field in body)
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
            "images": [{"url": "..."}],
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
        
        # Parse URL from documented response format: images[0].url
        output_url = None
        if "images" in data and isinstance(data["images"], list) and len(data["images"]) > 0:
            first_image = data["images"][0]
            if isinstance(first_image, dict):
                output_url = first_image.get("url")
        
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

    async def get_balance(self) -> float:
        """Get current Higgsfield credit balance."""
        response = await self.client.get("/v1/account/balance")
        response.raise_for_status()
        data = response.json()
        return data.get("balance", 0.0)
