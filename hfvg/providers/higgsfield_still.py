"""
Higgsfield still image provider using xai/grok-imagine-image-2.0.

Model: xai/grok-imagine-image-2.0
- 1k resolution, quality medium
- Up to 3 reference images (duck refs + location plate)
- ~$0.09/still (~$0.04 base + ~$0.01 per ref)
- Idempotency-Key on every call
- Pre-spend budget check via estimate endpoint
"""

import os
import aiohttp
import uuid
from typing import Any
from pathlib import Path


class HiggsfieldStillProvider:
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
    ):
        """
        Initialize Higgsfield still provider.
        
        Args:
            api_key: Higgsfield API key (default: from HIGGSFIELD_API_KEY)
            model_path: Model path (default: xai/grok-imagine-image-2.0)
            fallback_model: Fallback model (default: alibaba/qwen-image-3/edit)
        """
        self.api_key = api_key or os.getenv("HIGGSFIELD_API_KEY")
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
        
        self.base_url = "https://api.higgsfield.ai"
        self.session: aiohttp.ClientSession | None = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session."""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                }
            )
        return self.session
    
    async def close(self):
        """Close HTTP session."""
        if self.session and not self.session.closed:
            await self.session.close()
    
    async def upload_reference(self, image_path: str | Path) -> str:
        """
        Upload reference image and return media ID.
        
        Args:
            image_path: Path to reference image
        
        Returns:
            media_id for use in generation
        """
        session = await self._get_session()
        
        # Get upload URL
        async with session.post(
            f"{self.base_url}/files/generate-upload-url",
            json={"filename": Path(image_path).name}
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to get upload URL: {await resp.text()}")
            data = await resp.json()
            upload_url = data["upload_url"]
            media_id = data["media_id"]
        
        # Upload file
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        
        async with session.put(upload_url, data=image_bytes) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Failed to upload image: {await resp.text()}")
        
        return media_id
    
    async def estimate_cost(
        self,
        prompt: str,
        reference_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Get cost estimate before generation.
        
        Args:
            prompt: Generation prompt
            reference_ids: List of reference media IDs
        
        Returns:
            dict with estimated_usd, estimated_credits
        """
        session = await self._get_session()
        
        payload = {
            "model": self.model_path,
            "prompt": prompt,
            "resolution": "1k",
            "quality": "medium",
        }
        
        if reference_ids:
            payload["references"] = reference_ids[:3]  # Max 3
        
        async with session.post(
            f"{self.base_url}/estimate",
            json=payload
        ) as resp:
            if resp.status != 200:
                # Fallback estimate if API doesn't support estimate endpoint
                base_cost = 0.04
                ref_cost = len(reference_ids or []) * 0.01
                return {
                    "estimated_usd": base_cost + ref_cost,
                    "estimated_credits": 0,
                    "note": "Fallback estimate"
                }
            
            return await resp.json()
    
    async def generate_still(
        self,
        prompt: str,
        reference_ids: list[str] | None = None,
        idempotency_key: str | None = None,
        shot_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Generate still image.
        
        Args:
            prompt: Generation prompt
            reference_ids: List of reference media IDs (max 3)
            idempotency_key: Idempotency key (generated if None)
            shot_id: Shot identifier for tracking
        
        Returns:
            dict with job_id, media_url, cost_usd
        """
        session = await self._get_session()
        
        # Generate idempotency key if not provided
        if idempotency_key is None:
            idempotency_key = f"still-{shot_id or uuid.uuid4().hex}"
        
        payload = {
            "model": self.model_path,
            "prompt": prompt,
            "resolution": "1k",
            "quality": "medium",
        }
        
        if reference_ids:
            payload["references"] = reference_ids[:3]  # Max 3
        
        headers = {
            "Idempotency-Key": idempotency_key,
        }
        
        async with session.post(
            f"{self.base_url}/{self.model_path}",
            json=payload,
            headers=headers
        ) as resp:
            if resp.status not in (200, 201):
                error_text = await resp.text()
                raise RuntimeError(
                    f"Still generation failed ({resp.status}): {error_text}"
                )
            
            result = await resp.json()
            
            return {
                "job_id": result.get("job_id"),
                "media_url": result.get("media_url"),
                "cost_usd": result.get("cost_usd", 0.09),
                "idempotency_key": idempotency_key,
                "model": self.model_path,
            }
    
    async def check_job_status(self, job_id: str) -> dict[str, Any]:
        """
        Check generation job status.
        
        Args:
            job_id: Job identifier
        
        Returns:
            dict with status, media_url (if complete)
        """
        session = await self._get_session()
        
        async with session.get(f"{self.base_url}/jobs/{job_id}") as resp:
            if resp.status != 200:
                raise RuntimeError(f"Failed to check job: {await resp.text()}")
            
            return await resp.json()


async def create_higgsfield_provider(
    api_key: str | None = None,
    model_path: str | None = None,
) -> HiggsfieldStillProvider:
    """
    Create Higgsfield still provider.
    
    Args:
        api_key: Higgsfield API key
        model_path: Model path (default: xai/grok-imagine-image-2.0)
    
    Returns:
        HiggsfieldStillProvider instance
    """
    return HiggsfieldStillProvider(api_key=api_key, model_path=model_path)
