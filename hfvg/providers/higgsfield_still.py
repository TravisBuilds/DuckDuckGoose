"""
Higgsfield still image provider.

Implements the documented Higgsfield API:
- Authorization: Key <id>:<secret>
- POST /{model_path} → {"request_id": "..."}
- GET /requests/{request_id}/status → {"status": "queued"|"in_progress"|"completed"|"failed"|"nsfw"|"canceled", ...}
- Deterministic idempotency keys covering all request params
"""

import os
import json
import logging
import uuid
import hashlib
from pathlib import Path
from typing import Any

import httpx

from hfvg.config import config, max_refs_for
from hfvg.pricing import estimate_from_response
from hfvg.providers.common import apply_declined_preset
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus

logger = logging.getLogger(__name__)


def provider_quality(quality: str | None) -> str | None:
    """The quality value actually sent: the endpoint accepts low|medium only, anything else is sent as
    medium (harness 'high' is a budget-tier concept: L1_refs / L3_final_stills). None = field omitted."""
    if quality is None:
        return None
    return quality if quality in ("low", "medium") else "medium"
RAW_BODY_LOG_LIMIT = 16_384  # bytes of a provider error body kept in logs / exceptions
_HIDDEN_HEADERS = {"set-cookie", "authorization"}


def response_headers(response: httpx.Response) -> dict[str, str]:
    """All response headers except cookies/auth (X-Correlation-ID, cf-ray, date, ...)."""
    try:
        items = list(response.headers.items())
    except (AttributeError, TypeError):  # test doubles without real headers
        return {}
    return {k: v for k, v in items if k.lower() not in _HIDDEN_HEADERS}


def _is_http_error(response: httpx.Response) -> bool:
    code = getattr(response, "status_code", 200)
    return isinstance(code, int) and code >= 400


def _body_text(response: httpx.Response) -> str:
    try:
        text = response.text
    except Exception:  # undecodable body
        text = repr(response.content[:RAW_BODY_LOG_LIMIT])
    return text[:RAW_BODY_LOG_LIMIT]


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
    
    async def upload_reference(self, image_path: str | Path, version: int = 1) -> dict:
        """
        Upload reference image and return upload result with URL.
        
        Before upload, normalizes the image (JPEG, downscale, compress).
        Uses upload cache to avoid re-uploading the same file.
        
        Args:
            image_path: Path to reference image (will be normalized before upload)
            version: Version number for cache key (default: 1)
        
        Returns:
            dict with 'public_url' (the CDN URL for the uploaded file)
        """
        import mimetypes
        from hfvg.upload_cache import UploadCache
        from hfvg.image_normalize import normalize_reference_image
        
        # Normalize image first (convert to JPEG, downscale, compress)
        # This ensures we upload optimized images regardless of source format
        normalized_path, original_sha = normalize_reference_image(Path(image_path))
        
        # Use normalized path for upload
        upload_path = normalized_path
        
        # Initialize cache
        cache = UploadCache()
        await cache.init_db()
        
        # Check cache first (quick path) - cache key is the normalized path
        cached_url = await cache.get_cached_url(str(upload_path), version)
        if cached_url:
            return {"public_url": cached_url}
        
        # Try to claim upload slot (concurrent deduplication)
        claimed, sha256 = await cache.try_claim_upload_slot(str(upload_path), version)
        
        if not claimed:
            # Another upload is in progress for this file - wait for it
            waited_url = await cache.wait_for_concurrent_upload(str(upload_path), version, sha256, timeout=30.0)
            if waited_url:
                return {"public_url": waited_url}
            # Timeout or failed - fall through to retry upload ourselves
            claimed, sha256 = await cache.try_claim_upload_slot(str(upload_path), version)
            if not claimed:
                # Still can't claim - return cached or fail
                cached_url = await cache.get_cached_url(str(upload_path), version)
                if cached_url:
                    return {"public_url": cached_url}
                raise ValueError(f"Concurrent upload timeout for {upload_path}")
        
        # We claimed the slot - proceed with upload
        
        # Normalized images are always JPEG
        content_type = "image/jpeg"
        
        # Get upload URL and metadata (REQUIRES content_type)
        response = await self.client.post(
            "/files/generate-upload-url",
            json={"content_type": content_type}
        )
        response.raise_for_status()
        data = response.json()
        
        upload_url = data["upload_url"]
        public_url = data["public_url"]
        upload_headers = data["upload_headers"]
        
        # Upload normalized file with exact headers from API (includes x-amz-tagging)
        with open(upload_path, "rb") as f:
            image_bytes = f.read()
        
        # Use a plain httpx client for the PUT (no auth, just upload_headers)
        async with httpx.AsyncClient() as plain_client:
            upload_response = await plain_client.put(
                upload_url, 
                content=image_bytes,
                headers=upload_headers
            )
            upload_response.raise_for_status()
        
        # Cache the uploaded URL (keyed by normalized path)
        await cache.cache_url(str(upload_path), version, public_url)
        
        return {"public_url": public_url}
    
    async def _estimate_response(
        self,
        prompt: str,
        resolution: str,
        quality: str | None,
        aspect_ratio: str | None,
    ) -> dict:
        """POST /estimate/{model_path}; returns the parsed JSON (fail closed on any error)."""
        payload = {
            "prompt": prompt,
            "resolution": resolution,
        }
        
        # Only include quality if provided (SOUL V2 doesn't use quality field)
        if quality is not None:
            payload["quality"] = provider_quality(quality)
        
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio

        try:
            response = await self.client.post(
                f"/estimate/{self.model_path}",
                json=payload,
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            # Fail closed: never submit without a valid estimate
            raise ValueError(f"Failed to get estimate for still generation: {e}") from e

    async def estimate_cost(
        self,
        prompt: str,
        resolution: str = "1k",
        quality: str | None = None,
        aspect_ratio: str | None = None,
    ) -> float:
        """
        Estimate cost in Higgsfield API credits (informational; the ledger uses USD).

        API: POST /estimate/{model_path}
        Response: {"credits": "<str>", "usd": "<str>"}

        Raises:
            ValueError: If estimate fails or response is invalid (fail closed)
        """
        data = await self._estimate_response(prompt, resolution, quality, aspect_ratio)
        try:
            if "credits" not in data:
                raise ValueError(f"Estimate response missing 'credits' field: {data}")
            credits_str = data["credits"]
            if not isinstance(credits_str, str):
                raise ValueError(f"Estimate 'credits' must be string, got {type(credits_str)}")
            return float(credits_str)
        except (ValueError, KeyError) as e:
            raise ValueError(f"Failed to get estimate for still generation: {e}") from e

    async def estimate_usd_micros(
        self,
        prompt: str,
        resolution: str = "1k",
        quality: str | None = None,
        aspect_ratio: str | None = None,
    ) -> int:
        """
        Estimate cost in integer micro-dollars from the provider's own USD value.

        Stills have no per-second rate table: a response without ``usd`` is refused (fail closed).
        """
        data = await self._estimate_response(prompt, resolution, quality, aspect_ratio)
        try:
            usd_micros = estimate_from_response(data)
            if usd_micros is None or usd_micros <= 0:
                raise ValueError(f"Estimate response has no positive 'usd' value: {data}")
            return usd_micros
        except (ValueError, TypeError) as e:
            raise ValueError(f"Failed to get USD estimate for still generation: {e}") from e

    async def estimate_usd_and_credits(
        self,
        prompt: str,
        resolution: str = "1k",
        quality: str | None = None,
        aspect_ratio: str | None = None,
    ) -> tuple[int, float]:
        """
        Estimate cost in both USD micro-dollars and credits from the provider's estimate.

        Returns:
            (usd_micros, credits) tuple
        """
        data = await self._estimate_response(prompt, resolution, quality, aspect_ratio)
        try:
            usd_micros = estimate_from_response(data)
            if usd_micros is None or usd_micros <= 0:
                raise ValueError(f"Estimate response has no positive 'usd' value: {data}")
            
            # Extract credits from estimate response
            credits_str = data.get("credits")
            if credits_str is None:
                raise ValueError(f"Estimate response has no 'credits' value: {data}")
            credits = float(credits_str)
            if credits <= 0:
                raise ValueError(f"Estimate response has non-positive credits: {data}")
            
            return (usd_micros, credits)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Failed to get USD/credits estimate for still generation: {e}") from e


    async def submit_image(
        self,
        prompt: str,
        resolution: str = "1k",
        quality: str | None = None,
        image_urls: list[str] | None = None,
        idempotency_key: str | None = None,
        aspect_ratio: str | None = None,
    ) -> str:
        """
        Submit still image generation job.
        
        API: POST /{model_path}
        Request: {"prompt": "...", "resolution": "1k", "quality": "medium", 
                  "image_urls": [...], "aspect_ratio": "9:16"}
        Response: {"request_id": "..."}
        
        Args:
            prompt: Generation prompt
            resolution: Resolution (1k, 2k, 720p, 1080p)
            quality: Quality (low, medium only - no high). Omit for SOUL V2.
            image_urls: List of public reference image URLs (max 3)
            idempotency_key: Idempotency key (default: deterministic based on params)
            aspect_ratio: Aspect ratio (e.g. "9:16", "16:9", "1:1")
        
        Returns:
            request_id for polling
        """
        # Generate deterministic idempotency key if not provided
        if not idempotency_key:
            idempotency_key = self._generate_idempotency_key(
                prompt, resolution, quality or "medium", image_urls
            )
        
        # Submit generation with documented API format
        payload = {
            "prompt": prompt,
            "resolution": resolution,
        }
        
        # Only include quality if provided (SOUL V2 doesn't use quality field)
        if quality is not None:
            payload["quality"] = provider_quality(quality)
        
        if image_urls:
            # Documented limit: never silently truncate a shot's ref list, refuse instead
            max_refs = max_refs_for(self.model_path)
            if len(image_urls) > max_refs:
                raise ValueError(
                    f"{len(image_urls)} reference images exceed the {self.model_path} limit of "
                    f"{max_refs}; refusing instead of dropping refs"
                )
            payload["image_urls"] = list(image_urls)
        
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        
        apply_declined_preset(payload)
        
        headers = {"Idempotency-Key": idempotency_key}
        
        # POST /{model_path} per documented API (no model field in body)
        response = await self.client.post(
            f"/{self.model_path}",
            json=payload,
            headers=headers,
        )
        summary = {
            "model_path": self.model_path,
            "n_image_urls": len(payload.get("image_urls") or []),
            "image_urls": payload.get("image_urls") or [],
            "prompt_chars": len(prompt),
            "prompt_sha8": hashlib.sha256(prompt.encode()).hexdigest()[:8],
            "params": {k: v for k, v in payload.items() if k not in ("prompt", "image_urls")},
            "idempotency_key": idempotency_key,
        }
        hdrs = response_headers(response)
        if _is_http_error(response):
            body = _body_text(response)
            logger.error(
                "Higgsfield submit rejected: HTTP %s body=%s headers=%s request=%s",
                response.status_code, body, json.dumps(hdrs), json.dumps(summary),
            )
            raise httpx.HTTPStatusError(
                f"Higgsfield submit {self.model_path} -> HTTP {response.status_code} "
                f"(x-correlation-id {hdrs.get('x-correlation-id')}): {body}",
                request=response.request, response=response,
            )
        data = response.json()
        logger.info(
            "Higgsfield submit accepted: request_id=%s x-correlation-id=%s request=%s",
            data.get("request_id"), hdrs.get("x-correlation-id"), json.dumps(summary),
        )
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
        hdrs = response_headers(response)
        if _is_http_error(response):
            body = _body_text(response)
            logger.error("Higgsfield status %s -> HTTP %s body=%s headers=%s",
                         request_id, response.status_code, body, json.dumps(hdrs))
            raise httpx.HTTPStatusError(
                f"Higgsfield status {request_id} -> HTTP {response.status_code} "
                f"(x-correlation-id {hdrs.get('x-correlation-id')}): {body}",
                request=response.request, response=response,
            )
        
        data = response.json()
        status_str = data.get("status", "queued").lower()
        if status_str in ("failed", "nsfw", "blocked", "canceled"):
            # Terminal failure: keep the FULL raw body and headers (support needs X-Correlation-ID)
            logger.error("Higgsfield request %s terminal %s: body=%s headers=%s",
                         request_id, status_str, json.dumps(data), json.dumps(hdrs))
        
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
            raw=data,
            response_headers=hdrs,
        )

    async def get_balance(self) -> float:
        """Get current Higgsfield credit balance."""
        response = await self.client.get("/v1/account/balance")
        response.raise_for_status()
        data = response.json()
        return data.get("balance", 0.0)
