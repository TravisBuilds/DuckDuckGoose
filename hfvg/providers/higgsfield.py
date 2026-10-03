"""Higgsfield provider adapter with dry-run mode."""

import asyncio
import os
import uuid
from typing import Any

import httpx

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


# Model cost estimates from PIPELINE-LESSONS.md §5.1
MODEL_COSTS = {
    # Images (gpt_image_2)
    ("gpt_image_2", "1k", "low"): 0.5,
    ("gpt_image_2", "1k", "medium"): 1.0,
    ("gpt_image_2", "1k", "high"): 3.5,
    ("gpt_image_2", "2k", "low"): 0.5,
    ("gpt_image_2", "2k", "medium"): 2.0,
    ("gpt_image_2", "2k", "high"): 6.5,
    # Video (Seedance 2.5) - per second
    ("seedance_2.5", "480p", "draft"): 3.0,
    ("seedance_2.5", "720p", "standard"): 7.0,
    ("seedance_2.5", "1080p", "finalize"): 12.0,
    # Video (Kling 3.0) - per second
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
    Higgsfield provider adapter.

    Supports dry-run mode (default) and real API calls when DRY_RUN=false.
    """

    def __init__(self, api_key: str | None = None, dry_run: bool | None = None):
        self.api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.base_url = os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
        self.mock_balance = 1500.0  # Mock balance for dry-run

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

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/images/generate",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "prompt": prompt,
                    "resolution": resolution,
                    "quality": quality,
                    "references": references or [],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["job_id"]

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

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/video/generate",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": model,
                    "start_image": start_image,
                    "prompt": prompt,
                    "duration": duration,
                    "resolution": resolution,
                    "draft": draft,
                    "references": references or [],
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["job_id"]

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

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/v1/jobs/{job_id}",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()

            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus(data["status"]),
                progress=data.get("progress", 0.0),
                output_url=data.get("output", {}).get("url"),
                cost=data.get("cost", 0.0),
                error=data.get("error"),
            )

    async def get_balance(self) -> float:
        """Get current credit balance."""
        if self.dry_run:
            return self.mock_balance

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/v1/account/balance",
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            data = response.json()
            return data["balance"]

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """
        Estimate cost before submitting (0 credits).

        Based on model cost card from PIPELINE-LESSONS.md
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
