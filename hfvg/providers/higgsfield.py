"""Higgsfield Cloud API provider adapter with dry-run mode.

Uses the official higgsfield-client SDK.
Repo: https://github.com/higgsfield-ai/higgsfield-client
"""

import asyncio
import os
import uuid
from typing import Any

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


# Model cost estimates from PIPELINE-LESSONS.md §5.1
# NOTE: These are client-side estimates. No cost estimation endpoint in Higgsfield Cloud API.
MODEL_COSTS = {
    # Images (gpt_image_2) - cost per image
    ("gpt_image_2", "1k", "low"): 0.5,
    ("gpt_image_2", "1k", "medium"): 1.0,
    ("gpt_image_2", "1k", "high"): 3.5,
    ("gpt_image_2", "2k", "low"): 0.5,
    ("gpt_image_2", "2k", "medium"): 2.0,
    ("gpt_image_2", "2k", "high"): 6.5,
    # Video (Seedance 2.5) - cost per second
    ("seedance_2.5", "480p", "draft"): 3.0,
    ("seedance_2.5", "720p", "standard"): 7.0,
    ("seedance_2.5", "1080p", "finalize"): 12.0,
    # Video (Kling 3.0) - cost per second
    ("kling_3.0", "std", "standard"): 1.25,
    ("kling_3.0", "pro", "standard"): 1.5,
    # Upscale
    ("upscale_image", "2k", "standard"): 2.0,
    ("upscale_image", "4k", "standard"): 2.0,
    ("upscale_video", "1080p", "standard"): 0.02,  # per second
    ("upscale_video", "2k", "standard"): 0.04,  # per second
}


class HiggsfieldProvider(GenerationProvider):
    """
    Higgsfield Cloud API provider adapter.

    Supports dry-run mode (default) and real API calls when DRY_RUN=false.
    
    NOTE: Requires separate Higgsfield Cloud account at cloud.higgsfield.ai
    (separate from MCP app credits).
    """

    def __init__(self, api_key: str | None = None, dry_run: bool | None = None):
        """
        Initialize Higgsfield provider.
        
        Args:
            api_key: API key in format "key-id:key-secret", or None to use env
            dry_run: Override DRY_RUN config, or None to use config default
        """
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.mock_balance = 1500.0  # Mock balance for dry-run
        
        if not self.dry_run:
            # Get credentials from environment (HIGGSFIELD_API_KEY format: id:secret)
            self.api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
            if not self.api_key:
                raise ValueError(
                    "HIGGSFIELD_API_KEY not set. Required for live mode."
                )
            if ":" not in self.api_key:
                raise ValueError(
                    "HIGGSFIELD_API_KEY must be in format 'id:secret'"
                )
            
            # Import HTTP client for REST API calls with custom headers
            try:
                import httpx
                self.http_client = httpx.AsyncClient(timeout=60.0)
            except ImportError:
                raise ImportError("httpx not installed. Run: pip install httpx")

    async def submit_image(
        self,
        prompt: str,
        model: str = "gpt_image_2",
        resolution: str = "2k",
        quality: str = "high",
        references: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit image generation job.
        
        Args:
            idempotency_key: Optional idempotency key for safe retries.
                            If not provided, a random UUID is generated.
        
        Returns:
            Provider request_id
        """
        if self.dry_run:
            return self._mock_job_id("img")

        # Real API call
        # Model path from config (must be verified in cloud.higgsfield.ai)
        if model == "gpt_image_2":
            model_path = config.MODEL_PATH_GPT_IMAGE_2
            if not model_path:
                raise ValueError(
                    "MODEL_PATH_GPT_IMAGE_2 not configured. "
                    "Set environment variable to verified model path from cloud.higgsfield.ai. "
                    "See docs/PROVIDERS.md for known paths."
                )
        else:
            model_path = model  # Use as-is if not a known alias
        
        # Build request body
        body = {
            "prompt": prompt,
            "resolution": resolution,
            "quality": quality,
        }
        
        if references:
            body["references"] = references
        
        # Generate idempotency key if not provided
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        
        # Make REST API call with Authorization and Idempotency-Key headers
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        
        url = f"https://api.higgsfield.ai/{model_path}"
        response = await self.http_client.post(url, json=body, headers=headers)
        response.raise_for_status()
        
        result = response.json()
        return result["request_id"]

    async def submit_video(
        self,
        start_image: str,
        prompt: str,
        model: str = "kling_3.0",
        duration: float = 5.0,
        resolution: str = "1080p",
        draft: bool = False,
        references: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit video generation job.
        
        Args:
            idempotency_key: Optional idempotency key for safe retries.
                            If not provided, a random UUID is generated.
        
        Returns:
            Provider request_id
        """
        if self.dry_run:
            return self._mock_job_id("vid")

        # Real API call
        # Model path from config (documented defaults)
        if model == "seedance_2.5":
            model_path = config.MODEL_PATH_SEEDANCE
        elif model == "kling_3.0":
            model_path = config.MODEL_PATH_KLING
        else:
            model_path = model  # Use as-is if not a known alias
        
        # Build request body per model schema
        body = {
            "image_url": start_image,  # Both models use image_url (must be public URL)
            "prompt": prompt,
            "duration": int(duration),  # Both models: integer duration
        }
        
        # Seedance-specific: resolution field (480p/720p/1080p)
        if model == "seedance_2.5":
            body["resolution"] = resolution
            # draft parameter is not in the API - remove it
        
        # Kling-specific: no resolution field (always native 1080p)
        # draft parameter is not in the API - ignore it
        
        if references:
            body["references"] = references
        
        # Generate idempotency key if not provided
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        
        # Make REST API call with Authorization and Idempotency-Key headers
        headers = {
            "Authorization": f"Key {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        
        url = f"https://api.higgsfield.ai/{model_path}"
        response = await self.http_client.post(url, json=body, headers=headers)
        response.raise_for_status()
        
        result = response.json()
        return result["request_id"]

    async def submit_audio(
        self,
        text: str,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        settings: dict[str, Any] | None = None,
    ) -> str:
        """Not supported by Higgsfield (use ElevenLabs)."""
        raise NotImplementedError("Audio generation not supported by Higgsfield")

    async def get_job_status(self, job_id: str) -> ProviderJob:
        """Poll job status."""
        if self.dry_run:
            await asyncio.sleep(config.DRY_RUN_STILL_DELAY)
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=f"https://mock.higgsfield.ai/output/{job_id}.png",
                cost=6.5,
            )

        # Real API call
        headers = {
            "Authorization": f"Key {self.api_key}",
        }
        
        url = f"https://api.higgsfield.ai/requests/{job_id}/status"
        response = await self.http_client.get(url, headers=headers)
        response.raise_for_status()
        
        result = response.json()
        status_str = result.get("status", "").lower()
        
        # Map Higgsfield status to our enum
        if status_str == "queued":
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.PENDING,
                progress=0.0,
            )
        elif status_str in ("processing", "in_progress"):
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.PROCESSING,
                progress=0.5,
            )
        elif status_str == "completed":
            # Extract output URL from result (varies by model)
            output_url = None
            if "images" in result and result["images"]:
                output_url = result["images"][0].get("url")
            elif "video" in result and result["video"]:
                output_url = result["video"].get("url")
            
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=output_url,
                cost=0.0,  # No cost in response
            )
        elif status_str == "nsfw":
            # Moderation block - map to BLOCKED status
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.BLOCKED,
                progress=0.0,
                error="Content moderation (NSFW)",
            )
        elif status_str == "failed":
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.FAILED,
                progress=0.0,
                error=result.get("error", "Generation failed"),
            )
        elif status_str == "canceled":
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.FAILED,
                progress=0.0,
                error="Job canceled",
            )
        
        # Unknown status
        return ProviderJob(
            job_id=job_id,
            status=ProviderJobStatus.PROCESSING,
            progress=0.0,
        )

    async def get_balance(self) -> float:
        """
        Get current credit balance.
        
        NOTE: No balance endpoint in SDK. Returns mock in both modes.
        Travis must check balance via cloud.higgsfield.ai dashboard.
        """
        if self.dry_run:
            return self.mock_balance
        
        # No balance endpoint - return mock with warning
        return self.mock_balance

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """
        Estimate cost before submitting (0 credits).

        Based on client-side model cost card from PIPELINE-LESSONS.md.
        No cost estimation endpoint in Higgsfield Cloud API.
        """
        model = params.get("model", "gpt_image_2")
        resolution = params.get("resolution", "2k")
        quality = params.get("quality", "high")
        duration = params.get("duration", 5.0)

        if model.startswith("gpt_image"):
            key = (model, resolution, quality)
            return MODEL_COSTS.get(key, 6.5)

        elif model.startswith("seedance"):
            tier = "draft" if params.get("draft") else "standard"
            if resolution == "1080p" and params.get("finalize"):
                tier = "finalize"
            key = (model, resolution, tier)
            per_second = MODEL_COSTS.get(key, 7.0)
            return per_second * duration

        elif model.startswith("kling"):
            quality_tier = params.get("quality", "pro")
            key = (model, quality_tier, "standard")
            per_second = MODEL_COSTS.get(key, 1.5)
            return per_second * duration

        elif model.startswith("upscale"):
            key = (model, resolution, "standard")
            base_cost = MODEL_COSTS.get(key, 2.0)
            if "video" in model:
                return base_cost * duration
            return base_cost

        return 0.0

    def _mock_job_id(self, prefix: str) -> str:
        """Generate mock job ID for dry-run."""
        return f"{prefix}_mock_{uuid.uuid4().hex[:12]}"
