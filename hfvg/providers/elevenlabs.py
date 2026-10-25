"""ElevenLabs provider adapter with dry-run mode.

Uses the official elevenlabs SDK.
Docs: https://docs.elevenlabs.io/api-reference/authentication
"""

import asyncio
import hashlib
import os
import uuid
from pathlib import Path
from typing import Any

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus


# Character-based cost from official pricing (https://elevenlabs.io/pricing/api, 2026-10-19)
# eleven_v3, eleven_multilingual_v2: $0.08 per 1k chars
# eleven_flash_v2_5: $0.04 per 1k chars (formerly eleven_turbo_v2_5)
# sound_generation: $0.12 per minute (billed per generation)
# music_v1/v2: $0.15 per minute (billed per generation)
AUDIO_COSTS = {
    "eleven_v3": 0.08,  # USD per 1k chars (from model_caps.json el_pricing)
    "eleven_multilingual_v2": 0.08,  # USD per 1k chars
    "eleven_flash_v2_5": 0.04,  # USD per 1k chars
    "sound_generation": 0.12,  # USD per minute
    "music_v1": 0.15,  # USD per minute
    "music_v2": 0.15,  # USD per minute (assumed same as v1)
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
        model: str,  # Required, no default
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
        model: str = "eleven_v3",
        settings: dict[str, Any] | None = None,
    ) -> str:
        """
        Submit TTS generation job.
        
        NOTE: ElevenLabs TTS is synchronous - audio returns immediately.
        Audio is written to a durable path and a file:// URI is returned.
        """
        if self.dry_run:
            return self._mock_job_id("aud")

        # Real API call
        # ElevenLabs text_to_speech.convert() returns a generator of audio chunks
        audio_generator = self.client.text_to_speech.convert(
            voice_id=voice_id,
            text=text,
            model_id=model,
            voice_settings=settings or {}
        )
        
        # Collect audio bytes
        audio_bytes = b"".join(audio_generator)
        
        # Write to durable artifact path
        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "./data/artifacts/audio"))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        
        # Content-based filename for deduplication
        content_hash = hashlib.sha256(audio_bytes).hexdigest()[:16]
        output_path = artifacts_dir / f"tts_{model}_{content_hash}.mp3"
        
        if not output_path.exists():
            output_path.write_bytes(audio_bytes)
        
        # Calculate cost
        char_count = len(text)
        cost_usd = (char_count / 1000.0) * AUDIO_COSTS.get(model, 0.08)
        
        # Store job result with file URI
        job_id = self._mock_job_id("aud_real")
        self._cached_results = getattr(self, "_cached_results", {})
        self._cached_results[job_id] = {
            "output_url": f"file://{output_path}",
            "cost": cost_usd,
            "char_count": char_count,
        }
        
        return job_id
    
    async def submit_music(
        self,
        prompt: str,
        duration_ms: int,
        model: str = "music_v1",
        composition_plan: dict[str, Any] | None = None,
    ) -> str:
        """
        Submit music generation job via the /v1/music endpoint.
        
        Returns job_id for polling.
        """
        if self.dry_run:
            return self._mock_job_id("mus")
        
        # Use the music endpoint (NOT text_to_speech.convert)
        result = self.client.music.compose(
            prompt=prompt if not composition_plan else None,
            composition_plan=composition_plan,
            music_length_ms=duration_ms,
            model_id=model,
        )
        
        # Download and write to durable path
        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "./data/artifacts/audio"))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        
        content_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        output_path = artifacts_dir / f"music_{model}_{content_hash}_{duration_ms}.mp3"
        
        # The result is typically a generator or direct bytes
        audio_bytes = b"".join(result) if hasattr(result, '__iter__') else result
        output_path.write_bytes(audio_bytes)
        
        # Calculate cost: billed per generation, ~$0.15 per minute
        duration_min = duration_ms / 60000.0
        cost_usd = duration_min * AUDIO_COSTS.get(model, 0.15)
        
        job_id = self._mock_job_id("mus_real")
        self._cached_results = getattr(self, "_cached_results", {})
        self._cached_results[job_id] = {
            "output_url": f"file://{output_path}",
            "cost": cost_usd,
            "duration_ms": duration_ms,
        }
        
        return job_id
    
    async def submit_sfx(
        self,
        text: str,
        duration: float = 5.0,
        prompt_influence: float = 0.3,
    ) -> str:
        """
        Submit sound effects generation via /v1/sound-generation.
        
        Returns job_id for polling.
        """
        if self.dry_run:
            return self._mock_job_id("sfx")
        
        # Use the sound generation endpoint
        result = self.client.sound_generation.convert(
            text=text,
            duration_seconds=duration,
            prompt_influence=prompt_influence,
        )
        
        # Write to durable path
        artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "./data/artifacts/audio"))
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        
        content_hash = hashlib.sha256(text.encode()).hexdigest()[:16]
        output_path = artifacts_dir / f"sfx_{content_hash}_{int(duration*10)}.mp3"
        
        audio_bytes = b"".join(result) if hasattr(result, '__iter__') else result
        output_path.write_bytes(audio_bytes)
        
        # Cost: billed per generation, ~$0.12 per minute
        duration_min = duration / 60.0
        cost_usd = duration_min * AUDIO_COSTS.get("sound_generation", 0.12)
        
        job_id = self._mock_job_id("sfx_real")
        self._cached_results = getattr(self, "_cached_results", {})
        self._cached_results[job_id] = {
            "output_url": f"file://{output_path}",
            "cost": cost_usd,
            "duration_s": duration,
        }
        
        return job_id


    async def get_job_status(self, job_id: str) -> ProviderJob:
        """
        Poll job status.
        
        NOTE: ElevenLabs audio is synchronous, so status is always complete.
        Returns file:// URIs for durable artifact paths.
        """
        if self.dry_run:
            await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=f"file:///tmp/dry_{job_id}.mp3",
                cost=0.0,
            )

        # Real API: audio already generated in submit_* methods
        # Check cached results
        self._cached_results = getattr(self, "_cached_results", {})
        if job_id in self._cached_results:
            result = self._cached_results[job_id]
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                progress=1.0,
                output_url=result["output_url"],  # file:// URI
                cost=result.get("cost", 0.0),
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
        Get current balance in USD.
        
        NOTE: ElevenLabs uses character-based quota. Returns mock for now.
        Real implementation would use client.user.get_subscription().
        """
        if self.dry_run:
            return self.mock_balance
        
        # Would use client.user.get_subscription() to check character quota
        # and convert to USD equivalent
        # Returning mock for now
        return self.mock_balance

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """
        Estimate cost in USD before submitting.

        Based on official ElevenLabs pricing (el_pricing source, 2026-10-19):
        - TTS: character-based ($0.04-$0.08 per 1k chars)
        - Music: $0.15 per minute
        - SFX: $0.12 per minute
        """
        model = params.get("model", "eleven_v3")
        
        if job_type == "tts":
            text = params.get("text", "")
            char_count = len(text)
            cost_per_1k = AUDIO_COSTS.get(model, 0.08)
            return (char_count / 1000.0) * cost_per_1k

        elif job_type == "music":
            duration_ms = params.get("duration_ms", 0)
            duration_min = duration_ms / 60000.0
            return duration_min * AUDIO_COSTS.get(model, 0.15)

        elif job_type == "sfx":
            duration_s = params.get("duration", 0.0)
            duration_min = duration_s / 60.0
            return duration_min * AUDIO_COSTS.get("sound_generation", 0.12)

        return 0.0

    def _mock_job_id(self, prefix: str) -> str:
        """Generate mock job ID for dry-run."""
        return f"{prefix}_mock_{uuid.uuid4().hex[:12]}"
