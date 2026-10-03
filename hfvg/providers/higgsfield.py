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
            # Only import real SDK if not in dry-run mode
            try:
                import higgsfield_client
                self.hf = higgsfield_client
                
                # SDK auto-detects HF_KEY or HF_API_KEY + HF_API_SECRET from env
                # Can also pass api_key explicitly
                from higgsfield_client import AsyncClient
                self.client = AsyncClient(api_key=api_key) if api_key else AsyncClient()
            except ImportError:
                raise ImportError(
                    "higgsfield-client not installed. Run: pip install higgsfield-client"
                )

    async def submit_image(
        self,
        prompt: str,
        model: str = "gpt_image_2",
        resolution: str = "2k",
        quality: str = "high",
        references: list[str] | None = None,
    ) -> str:
        """Submit image generation job."""
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
        
        arguments = {
            "prompt": prompt,
            "resolution": resolution,
            "quality": quality,
        }
        
        if references:
            # Upload references first
            reference_urls = []
            for ref in references:
                if ref.startswith("http"):
                    reference_urls.append(ref)
                else:
                    # Local file - upload it
                    url = await self.client.upload_file(ref)
                    reference_urls.append(url)
            arguments["references"] = reference_urls
        
        controller = await self.hf.submit_async(
            model_path,
            arguments=arguments
        )
        
        return controller.request_id

    async def submit_video(
        self,
        start_image: str,
        prompt: str,
        model: str = "kling_3.0",
        duration: float = 5.0,
        resolution: str = "1080p",
        draft: bool = False,
        references: list[str] | None = None,
    ) -> str:
        """Submit video generation job."""
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
        
        # Upload start image if local
        if not start_image.startswith("http"):
            start_image = await self.client.upload_file(start_image)
        
        # Build arguments per model schema
        arguments = {
            "image_url": start_image,  # Both models use image_url
            "prompt": prompt,
            "duration": int(duration),  # Both models: integer duration
        }
        
        # Seedance-specific: resolution field (480p/720p/1080p)
        if model == "seedance_2.5":
            arguments["resolution"] = resolution
            # draft parameter is not in the API - remove it
        
        # Kling-specific: no resolution field (always native 1080p)
        # draft parameter is not in the API - ignore it
        
        if references:
            reference_urls = []
            for ref in references:
                if ref.startswith("http"):
                    reference_urls.append(ref)
                else:
                    url = await self.client.upload_file(ref)
                    reference_urls.append(url)
            arguments["references"] = reference_urls
        
        controller = await self.hf.submit_async(
            model_path,
            arguments=arguments
        )
        
        return controller.request_id

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
        status = await self.hf.status_async(request_id=job_id)
        
        # Map Higgsfield status to our enum
        if isinstance(status, self.hf.Queued):
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.PENDING,
                progress=0.0,
            )
        elif isinstance(status, self.hf.InProgress):
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.PROCESSING,
                progress=0.5,  # SDK doesn't provide progress percentage
            )
        elif isinstance(status, self.hf.Completed):
            # Get result
            result = await self.hf.result_async(request_id=job_id)
            
            # Extract output URL (structure depends on model, unverified)
            output_url = result.get("url") or result.get("output_url")
            if isinstance(output_url, list) and output_url:
                output_url = output_url[0]
            
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=output_url,
                cost=0.0,  # No cost in response, would need dashboard check
            )
        elif isinstance(status, self.hf.NSFW):
            # Moderation block - map to BLOCKED status
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.BLOCKED,
                progress=0.0,
                error="Content moderation (NSFW)",
            )
        elif isinstance(status, self.hf.Failed):
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.FAILED,
                progress=0.0,
                error="Generation failed",
            )
        elif isinstance(status, self.hf.Cancelled):
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.FAILED,
                progress=0.0,
                error="Job cancelled",
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
