"""ElevenLabs provider adapter with dry-run mode."""

import asyncio
import os
import uuid
from typing import Any

import httpx

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


# Model cost estimates from PIPELINE-LESSONS.md
AUDIO_COSTS = {
    "eleven_multilingual_v2": 30.0,  # ~30cr per minute
    "eleven_turbo_v2_5": 20.0,  # ~20cr per minute
    "eleven_music": 14.7,  # ~1,400cr per 95s
    "sound_generation": 20.0,  # ~100cr per 5s
}


class ElevenLabsProvider(GenerationProvider):
    """
    ElevenLabs provider adapter.

    Supports dry-run mode (default) and real API calls when DRY_RUN=false.
    """

    def __init__(self, api_key: str | None = None, dry_run: bool | None = None):
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY") or os.getenv("XI_API_KEY", "")
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.base_url = os.getenv("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")
        self.mock_balance = 5000.0  # Mock balance for dry-run

    async def submit_image(
        self,
        prompt: str,
        model: str = "gpt_image_2",
        resolution: str = "2k",
        quality: str = "high",
        references: list[str] | None = None,
    ) -> str:
        """Not supported by ElevenLabs (use Higgsfield)."""
        raise NotImplementedError("Image generation not supported by ElevenLabs")

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
        """Not supported by ElevenLabs (use Higgsfield)."""
        raise NotImplementedError("Video generation not supported by ElevenLabs")

    async def submit_audio(
        self,
        text: str,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        settings: dict[str, Any] | None = None,
    ) -> str:
        """Submit audio generation job (TTS)."""
        if self.dry_run:
            return self._mock_job_id("aud")

        # Real API call would go here
        settings = settings or {}
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "text": text,
                    "model_id": model,
                    "voice_settings": settings,
                },
            )
            response.raise_for_status()

            # ElevenLabs returns audio directly, not a job ID
            # For consistency, we'd need to wrap in a job-like structure
            # or handle synchronously
            job_id = self._mock_job_id("aud_real")
            return job_id

    async def submit_sound_effect(self, description: str, duration: float = 5.0) -> str:
        """Submit sound effect generation."""
        if self.dry_run:
            return self._mock_job_id("sfx")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/sound-generation",
                headers={"xi-api-key": self.api_key},
                json={
                    "text": description,
                    "duration_seconds": duration,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("generation_id", self._mock_job_id("sfx_real"))

    async def submit_music(
        self, prompt: str, duration: float = 30.0, model: str = "eleven_music"
    ) -> str:
        """Submit music generation."""
        if self.dry_run:
            return self._mock_job_id("mus")

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/v1/music-generation",
                headers={"xi-api-key": self.api_key},
                json={
                    "prompt": prompt,
                    "duration_seconds": duration,
                    "model": model,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("generation_id", self._mock_job_id("mus_real"))

    async def get_job_status(self, job_id: str) -> ProviderJob:
        """Poll job status."""
        if self.dry_run:
            await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=f"https://mock.elevenlabs.io/output/{job_id}.mp3",
                cost=30.0,
            )

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/v1/history/{job_id}",
                headers={"xi-api-key": self.api_key},
            )
            response.raise_for_status()
            data = response.json()

            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED if data.get("state") == "complete" else ProviderJobStatus.PROCESSING,
                progress=1.0 if data.get("state") == "complete" else 0.5,
                output_url=data.get("audio_url"),
                cost=data.get("character_cost", 0.0) / 1000.0,  # Convert to credits
            )

    async def get_balance(self) -> float:
        """Get current credit balance."""
        if self.dry_run:
            return self.mock_balance

        # Real API call would go here
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{self.base_url}/v1/user/subscription",
                headers={"xi-api-key": self.api_key},
            )
            response.raise_for_status()
            data = response.json()
            return data.get("character_limit", 0) / 1000.0  # Convert to credits

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """
        Estimate cost before submitting (0 credits).

        Based on ElevenLabs pricing from PIPELINE-LESSONS.md
        """
        model = params.get("model", "eleven_multilingual_v2")
        duration = params.get("duration", 0.0)
        text_length = len(params.get("text", "")) / 1000.0  # Rough estimate

        if job_type == "tts":
            per_minute = AUDIO_COSTS.get(model, 30.0)
            minutes = text_length * 2.0  # Rough: 1k chars ≈ 2 min spoken
            return per_minute * minutes

        elif job_type == "music":
            per_second = AUDIO_COSTS["eleven_music"]
            return (duration / 95.0) * 1400.0  # 1400cr per 95s

        elif job_type == "sfx":
            return AUDIO_COSTS["sound_generation"] * (duration / 5.0)

        return 0.0

    def _mock_job_id(self, prefix: str) -> str:
        """Generate mock job ID for dry-run."""
        return f"{prefix}_mock_{uuid.uuid4().hex[:12]}"
