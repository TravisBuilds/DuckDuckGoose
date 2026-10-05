"""ElevenLabs provider adapter with dry-run mode.

Uses the official elevenlabs SDK.
Docs: https://docs.elevenlabs.io/api-reference/authentication
"""

import asyncio
import os
import uuid
from typing import Any

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


# Model cost estimates from PIPELINE-LESSONS.md (UNVERIFIED from docs)
# ElevenLabs uses character-based pricing: 1 credit ≈ 1000 characters
AUDIO_COSTS = {
    "eleven_multilingual_v2": 30.0,  # ~30cr per minute (estimated)
    "eleven_turbo_v2_5": 20.0,  # ~20cr per minute (estimated)
    "eleven_music": 14.7,  # ~1,400cr per 95s (from PIPELINE-LESSONS)
    "sound_generation": 20.0,  # ~100cr per 5s (from PIPELINE-LESSONS)
}


class ElevenLabsProvider(GenerationProvider):
    """
    ElevenLabs provider adapter.

    Supports dry-run mode (default) and real API calls when DRY_RUN=false.
    SDK auto-detects ELEVENLABS_API_KEY environment variable.
    """

    def __init__(self, api_key: str | None = None, dry_run: bool | None = None):
        """
        Initialize ElevenLabs provider.
        
        Args:
            api_key: API key, or None to use ELEVENLABS_API_KEY from env
            dry_run: Override DRY_RUN config, or None to use config default
        """
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.mock_balance = 5000.0  # Mock balance for dry-run
        
        if not self.dry_run:
            # Only import real SDK if not in dry-run mode
            try:
                from elevenlabs.client import ElevenLabs
                # SDK auto-detects ELEVENLABS_API_KEY from env
                self.client = ElevenLabs(api_key=api_key) if api_key else ElevenLabs()
            except ImportError:
                raise ImportError(
                    "elevenlabs not installed. Run: pip install elevenlabs"
                )

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
        """
        Submit audio generation job (TTS).
        
        NOTE: ElevenLabs TTS is synchronous - audio returns immediately.
        We generate a job ID for consistency with Higgsfield's async model.
        """
        if self.dry_run:
            return self._mock_job_id("aud")

        # Real API call
        # ElevenLabs text_to_speech.convert() returns a generator of audio chunks
        # We'll store the result and return a job ID
        
        audio_generator = self.client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=model,
            voice_settings=settings or {}
        )
        
        # Collect audio bytes (in real use, would stream to file)
        audio_bytes = b"".join(audio_generator)
        
        # Generate job ID and cache result
        # In production, this would be stored in a temp location or S3
        job_id = self._mock_job_id("aud_real")
        self._cached_results = getattr(self, "_cached_results", {})
        self._cached_results[job_id] = {
            "audio_bytes": audio_bytes,
            "format": "mp3"
        }
        
        return job_id


    async def get_job_status(self, job_id: str) -> ProviderJob:
        """
        Poll job status.
        
        NOTE: ElevenLabs audio is synchronous, so status is always complete.
        """
        if self.dry_run:
            await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=f"https://mock.elevenlabs.io/output/{job_id}.mp3",
                cost=30.0,
            )

        # Real API: audio already generated in submit_audio
        # Check cached results
        self._cached_results = getattr(self, "_cached_results", {})
        if job_id in self._cached_results:
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=f"cached://{job_id}",  # Would be S3 URL in production
                cost=0.0,  # Cost would come from usage API
            )
        
        # Job not found
        return ProviderJob(
            job_id=job_id,
            status=ProviderJobStatus.FAILED,
            progress=0.0,
            error="Job not found in cache",
        )

    async def get_balance(self) -> float:
        """
        Get current credit balance.
        
        NOTE: ElevenLabs uses character-based quota. Returns mock for now.
        Use client.user endpoint to check subscription/usage in production.
        """
        if self.dry_run:
            return self.mock_balance
        
        # Would use client.user.get_subscription() or client.usage endpoints
        # Returning mock for now as implementation details need verification
        return self.mock_balance

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
