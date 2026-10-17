"""Seedance 2.5 image-to-video provider (Higgsfield REST).

Documented endpoint: POST /bytedance/seedance-2.5/image-to-video
(https://docs.higgsfield.ai/docs/models/seedance-2-5/image-to-video)

Input schema (additionalProperties: false): image_url (required), prompt, duration (int 4-30),
resolution (480p|720p|1080p), bitrate_mode (standard|high), end_image_url, generate_audio.
There is NO aspect-ratio parameter (output framing follows image_url).

Policy:
- generate_audio is ALWAYS sent as false (cost predictability, no unwanted audio).
- duration < 4 s is refused here with a clear error (the planner routes those shots to Kling).
- Cost: the Seedance estimate endpoint returns a pricing description, not USD, so the estimate is
  the configured per-second USD rate table (hfvg.config MODEL_USD_PER_SECOND_MICROS; list price
  before discount) feeding the dollar ledger.
- Draft -> final: a `draft_job_id` finalize is UNVERIFIED on REST and is NOT implemented.
  Final = a fresh 1080p submit.
"""

import hashlib
import os
from pathlib import Path
from typing import Any

import httpx

from hfvg.pricing import rate_table_estimate
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus
from hfvg.providers.common import apply_declined_preset

SEEDANCE_MODEL_PATH = "bytedance/seedance-2.5/image-to-video"
SEEDANCE_RATE_KEY = "seedance-2.5"
MIN_DURATION_S = 4
MAX_DURATION_S = 30
RESOLUTIONS = ("480p", "720p", "1080p")
BITRATE_MODES = ("standard", "high")


class SeedanceVideoProvider(GenerationProvider):
    """Seedance 2.5 image-to-video via Higgsfield."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        api_key = api_key or os.getenv("HIGGSFIELD_API_KEY", "")
        if not api_key:
            raise ValueError("HIGGSFIELD_API_KEY not set. Video generation will fail.")
        if ":" not in api_key:
            raise ValueError("HIGGSFIELD_API_KEY must be in format 'id:secret'")
        self.api_key_id, self.api_key_secret = api_key.split(":", 1)
        self.base_url = base_url or os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
        self.model_path = os.getenv("MODEL_PATH_SEEDANCE", SEEDANCE_MODEL_PATH)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Key {self.api_key_id}:{self.api_key_secret}",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )

    async def close(self):
        await self.client.aclose()

    async def upload_reference(self, image_path: str | Path, version: int = 1) -> dict:
        """
        Upload reference image and return upload result with URL.
        
        Uses upload cache to avoid re-uploading the same file.
        Before reuse, HEAD-checks the cached URL to ensure it's still accessible.
        
        Args:
            image_path: Path to reference image
            version: Version number for cache key (default: 1)
        
        Returns:
            dict with 'public_url' (the CDN URL for the uploaded file)
        """
        import mimetypes
        from hfvg.upload_cache import UploadCache
        from pathlib import Path as PathLib
        
        # Check upload cache first
        cache = UploadCache()
        await cache.init_db()
        
        cached_url = await cache.get_cached_url(str(image_path), version)
        if cached_url:
            return {"public_url": cached_url}
        
        # Not in cache or HEAD check failed - upload the file
        
        # Determine content type from file extension
        content_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        
        # Get upload URL (REQUIRES content_type)
        response = await self.client.post(
            "/files/generate-upload-url",
            json={"content_type": content_type}
        )
        response.raise_for_status()
        data = response.json()
        upload_url = data["upload_url"]
        public_url = data["public_url"]
        upload_headers = data["upload_headers"]
        
        # Upload file with exact headers from API (includes x-amz-tagging)
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        
        # Use a plain httpx client for the PUT (no auth, just upload_headers)
        async with httpx.AsyncClient() as plain_client:
            upload_response = await plain_client.put(
                upload_url,
                content=image_bytes,
                headers=upload_headers
            )
            upload_response.raise_for_status()
        
        # Cache the uploaded URL
        await cache.cache_url(str(image_path), version, public_url)
        
        return {"public_url": public_url}

    @staticmethod
    def validate(duration: int, resolution: str, bitrate_mode: str | None = None) -> None:
        """Refuse parameters outside the documented schema (fail closed, clear errors)."""
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise ValueError(f"Seedance duration must be an integer number of seconds, got {duration!r}")
        if duration < MIN_DURATION_S:
            raise ValueError(
                f"Seedance 2.5 requires duration >= {MIN_DURATION_S}s (got {duration}s); "
                f"route shorter shots to Kling"
            )
        if duration > MAX_DURATION_S:
            raise ValueError(f"Seedance 2.5 supports at most {MAX_DURATION_S}s (got {duration}s)")
        if resolution not in RESOLUTIONS:
            raise ValueError(f"Seedance resolution must be one of {RESOLUTIONS}, got {resolution!r}")
        if bitrate_mode is not None and bitrate_mode not in BITRATE_MODES:
            raise ValueError(f"Seedance bitrate_mode must be one of {BITRATE_MODES}, got {bitrate_mode!r}")

    def _generate_idempotency_key(
        self,
        image_url: str,
        prompt: str,
        duration: int,
        resolution: str,
        end_image_url: str | None = None,
    ) -> str:
        """hf-sd-{sha256(model|resolution|duration|image|end_image|prompt_hash)[:16]}."""
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
        key_input = (
            f"{self.model_path}|{resolution}|{duration}|{image_url}|{end_image_url or ''}|{prompt_hash}"
        )
        return f"hf-sd-{hashlib.sha256(key_input.encode()).hexdigest()[:16]}"

    async def estimate_usd_micros(self, duration: int, resolution: str) -> int:
        """USD estimate in micro-dollars from the configured rate table (no network call).

        List price before discount: conservative for reservation.
        """
        self.validate(duration, resolution)
        estimate = rate_table_estimate(SEEDANCE_RATE_KEY, resolution, duration)
        if estimate.usd_micros <= 0:
            raise ValueError("Non-positive Seedance USD estimate")
        return estimate.usd_micros

    async def estimate_usd_and_credits(self, duration: int, resolution: str) -> tuple[int, float]:
        """
        Estimate cost in both USD micro-dollars and credits from the rate table.

        Returns:
            (usd_micros, credits) tuple
        """
        self.validate(duration, resolution)
        estimate = rate_table_estimate(SEEDANCE_RATE_KEY, resolution, duration)
        if estimate.usd_micros <= 0:
            raise ValueError("Non-positive Seedance USD estimate")
        
        # Calculate credits from rate table
        from hfvg.credit_pricing import estimate_clip_credits
        credits = estimate_clip_credits("seedance_2.5", duration, resolution)
        
        return (estimate.usd_micros, credits)


    def build_payload(
        self,
        image_url: str,
        prompt: str,
        duration: int,
        resolution: str,
        bitrate_mode: str | None = None,
        end_image_url: str | None = None,
    ) -> dict:
        self.validate(duration, resolution, bitrate_mode)
        payload: dict = {
            "image_url": image_url,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
            "generate_audio": False,  # always false
        }
        if bitrate_mode:
            payload["bitrate_mode"] = bitrate_mode
        if end_image_url:
            payload["end_image_url"] = end_image_url
        return apply_declined_preset(payload)

    async def submit_video(
        self,
        image_url: str,
        prompt: str,
        duration: int,
        resolution: str = "480p",
        bitrate_mode: str | None = None,
        end_image_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """POST /{model_path}; returns request_id. Refuses duration < 4 before any request."""
        payload = self.build_payload(
            image_url, prompt, duration, resolution, bitrate_mode, end_image_url
        )
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                image_url, prompt, duration, resolution, end_image_url
            )
        response = await self.client.post(
            f"/{self.model_path}",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        response.raise_for_status()
        return response.json()["request_id"]

    async def get_job_status(self, request_id: str) -> ProviderJob:
        """GET /requests/{request_id}/status (same shared contract as other models)."""
        response = await self.client.get(f"/requests/{request_id}/status")
        response.raise_for_status()
        data = response.json()
        status_str = data.get("status", "queued").lower()
        if status_str == "completed":
            status = ProviderJobStatus.COMPLETED
        elif status_str == "failed":
            status = ProviderJobStatus.FAILED
        elif status_str in ("blocked", "nsfw", "canceled"):
            status = ProviderJobStatus.BLOCKED
        elif status_str == "in_progress":
            status = ProviderJobStatus.PROCESSING
        else:
            status = ProviderJobStatus.PENDING
        output_url = None
        if isinstance(data.get("video"), dict):
            output_url = data["video"].get("url")
        return ProviderJob(
            job_id=request_id,
            status=status,
            progress=data.get("progress", 0.0),
            output_url=output_url,
            cost=data.get("cost"),
            error=data.get("error"),
        )
