"""Base provider interface for generation services."""

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ProviderJobStatus(str, Enum):
    """Status of a provider job."""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"  # Content moderation


class ProviderJob(BaseModel):
    """Provider job result."""

    job_id: str
    status: ProviderJobStatus
    progress: float = 0.0  # 0.0 to 1.0
    output_url: str | None = None
    cost: float = 0.0
    error: str | None = None


class GenerationProvider:
    """Base interface for generation providers."""

    async def submit_image(
        self,
        prompt: str,
        model: str = "gpt_image_2",
        resolution: str = "2k",
        quality: str = "high",
        references: list[str] | None = None,
    ) -> str:
        """
        Submit image generation job.

        Returns:
            provider_job_id: Job ID for polling
        """
        raise NotImplementedError

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
        """
        Submit video generation job.

        Returns:
            provider_job_id: Job ID for polling
        """
        raise NotImplementedError

    async def submit_audio(
        self,
        text: str,
        voice_id: str,
        model: str = "eleven_multilingual_v2",
        settings: dict[str, Any] | None = None,
    ) -> str:
        """
        Submit audio generation job (TTS).

        Returns:
            provider_job_id: Job ID for polling
        """
        raise NotImplementedError

    async def get_job_status(self, job_id: str) -> ProviderJob:
        """Poll job status."""
        raise NotImplementedError

    async def get_balance(self) -> float:
        """Get current credit balance."""
        raise NotImplementedError

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """Estimate cost before submitting (0 credits)."""
        raise NotImplementedError
