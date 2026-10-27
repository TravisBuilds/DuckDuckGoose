"""ElevenLabs provider adapter with dry-run mode.

Live calls use the documented REST endpoints directly (httpx), so every request is explicit
and testable without network:

- TTS:   POST /v1/text-to-speech/{voice_id}   (body: text, model_id, voice_settings, seed)
- SFX:   POST /v1/sound-generation             (body: text, duration_seconds, loop, model_id)
- Music: POST /v1/music                        (body: prompt, music_length_ms, model_id,
                                                force_instrumental, seed)
- Balance: GET /v1/user/subscription           (character_limit - character_count)

Each endpoint returns the audio file; it is written to a durable path on disk and the job
result carries a ``file://`` URI, so results survive across activities and workers.

Costs are USD, from the official per-unit API prices in hfvg/data/model_caps.json
(TTS per 1k characters, SFX and music per minute) and are never 0.0 for a billable job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import uuid
from pathlib import Path
from typing import Any

import httpx

from hfvg.config import config
from hfvg.providers.base import GenerationProvider, ProviderJob, ProviderJobStatus

API_BASE = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")

# Official API USD prices (https://elevenlabs.io/pricing/api, checked 2026-10-25); the source of
# truth is model_caps.json (cost.usd / cost.unit) - these must match it (tests enforce that).
# unit "1k_chars": TTS billed per character; unit "minute": billed per generated minute.
AUDIO_COSTS: dict[str, dict[str, Any]] = {
    "eleven_v3": {"kind": "tts", "unit": "1k_chars", "usd": 0.08},
    "eleven_multilingual_v2": {"kind": "tts", "unit": "1k_chars", "usd": 0.08},
    "eleven_flash_v2_5": {"kind": "tts", "unit": "1k_chars", "usd": 0.04},
    "sound_generation": {"kind": "sfx", "unit": "minute", "usd": 0.12},
    "music_v1": {"kind": "music", "unit": "minute", "usd": 0.15},
    "music_v2": {"kind": "music", "unit": "minute", "usd": 0.15},
    "music_v2_5": {"kind": "music", "unit": "minute", "usd": 0.15},
}

# Legacy harness ids still accepted from older plans/params; mapped to a real upstream id
# before any call. (eleven_music was never an ElevenLabs model id.)
AUDIO_MODEL_ALIASES = {"eleven_music": "music_v1"}

DEFAULT_TTS_MODEL = "eleven_v3"
DEFAULT_MUSIC_MODEL = "music_v1"
SFX_UPSTREAM_MODEL = "eleven_text_to_sound_v2"
# Billing floor for SFX with no explicit duration (upstream picks one; the cap is 30 s).
SFX_DEFAULT_ESTIMATE_S = 30.0

# Legacy credit estimates kept for the credit-based estimate_cost() contract.
_LEGACY_CREDITS = {"tts_per_min": 30.0, "music_per_95s": 1400.0, "sfx_per_5s": 20.0}

# G-AUD-COST: actual ElevenLabs spend is reported per request in the ``character-cost`` response
# header (credits charged; a live eleven_v3 call sending 71 characters returned 28). Credits are
# converted to USD at ELEVENLABS_USD_PER_CREDIT. The default is the highest per-credit price of
# any paid plan (Starter: $6 / 30,000 credits = $0.0002; Creator $22/121k, Pro $99/600k, Scale
# $299/1.8M and Business $990/6M are all cheaper per credit; https://elevenlabs.io/pricing,
# checked 2026-10-25), so the ledger never under-states spend. Set it to your plan's
# price / included credits (e.g. 0.000182 for Creator).
USD_PER_CREDIT_ENV = "ELEVENLABS_USD_PER_CREDIT"
DEFAULT_USD_PER_CREDIT = "0.0002"

# Documented upper bounds of credits per request (https://elevenlabs.io/pricing and the sound
# effects docs, checked 2026-10-25). Reservations use these, so the committed actual (from the
# header) is <= the reservation: TTS at most 1 credit per character (multilingual v2 / v3; Flash
# and Turbo bill 0.5-1); SFX 40 credits per second when the duration is set, 200 per generation
# when it is automatic; music 900 credits per minute.
MAX_CREDITS_PER_CHAR = 1.0
SFX_CREDITS_PER_SECOND = 40.0
SFX_AUTO_DURATION_CREDITS = 200.0
MUSIC_CREDITS_PER_MINUTE = 900.0

CHARACTER_COST_HEADER = "character-cost"
COST_SOURCE_HEADER = "provider_header"
COST_SOURCE_ESTIMATE = "estimate"


class ElevenLabsError(RuntimeError):
    """A live ElevenLabs call failed (HTTP error or empty audio)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def permanent(self) -> bool:
        """Auth/permission/validation failures: a retry repeats the same refusal."""
        return self.status_code in (400, 401, 403, 404, 422)


def canonical_audio_model(model: str) -> str:
    return AUDIO_MODEL_ALIASES.get(model, model)


def usd_per_credit() -> float:
    """USD per ElevenLabs credit from ELEVENLABS_USD_PER_CREDIT (default: Starter plan rate).

    Fails closed: a non-numeric, zero or negative value raises ValueError (a bad rate would make
    every reservation and commit wrong)."""
    raw = os.getenv(USD_PER_CREDIT_ENV, DEFAULT_USD_PER_CREDIT).strip()
    try:
        rate = float(raw)
    except ValueError:
        raise ValueError(f"{USD_PER_CREDIT_ENV} must be a number (USD per credit), got {raw!r}") from None
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"{USD_PER_CREDIT_ENV} must be > 0, got {raw!r}")
    return rate


def credits_to_usd_micros(credits: float) -> int:
    """ElevenLabs credits -> integer micro-dollars at usd_per_credit(), rounded up (never below 1)."""
    if credits < 0:
        raise ValueError(f"credits must be >= 0, got {credits}")
    return max(1, math.ceil(credits * usd_per_credit() * 1_000_000 - 1e-9))


def max_credits(job_type: str, params: dict[str, Any]) -> float:
    """Documented upper bound of the credits one request can be charged (see constants above)."""
    if job_type == "tts":
        chars = len(params.get("text") or "")
        if chars == 0:
            raise ValueError("TTS credit bound needs the text (cost is per character)")
        return MAX_CREDITS_PER_CHAR * chars
    if job_type == "sfx":
        duration = params.get("duration")
        if duration is None:
            return SFX_AUTO_DURATION_CREDITS
        return SFX_CREDITS_PER_SECOND * float(duration)
    if job_type == "music":
        seconds = float(params.get("duration") or 0.0)
        if seconds <= 0:
            raise ValueError("music credit bound needs a duration")
        return MUSIC_CREDITS_PER_MINUTE * seconds / 60.0
    raise ValueError(f"unknown ElevenLabs job type {job_type!r}")


def reservation_usd_micros(job_type: str, params: dict[str, Any]) -> int:
    """What to reserve before a call: the larger of the API USD estimate and the documented
    credit upper bound at usd_per_credit(), so the actual from the header never exceeds it."""
    return max(estimate_usd_micros(job_type, params),
               credits_to_usd_micros(max_credits(job_type, params)))


def parse_character_cost(headers: Any) -> float | None:
    """Credits charged, from the ``character-cost`` response header (case-insensitive).

    None when the header is absent or not a finite non-negative number (the caller then falls
    back to the estimate and flags it)."""
    if headers is None:
        return None
    value = None
    for k, v in headers.items():
        if str(k).lower() == CHARACTER_COST_HEADER:
            value = v
            break
    if value is None:
        return None
    try:
        credits = float(str(value).strip())
    except ValueError:
        return None
    if not math.isfinite(credits) or credits < 0:
        return None
    return credits


def estimate_usd(job_type: str, params: dict[str, Any]) -> float:
    """USD estimate from official unit prices: characters for TTS, minutes for SFX/music.

    Raises KeyError for an unpriced model (never guesses, never returns 0 for billable work)."""
    model = canonical_audio_model(params.get("model") or {
        "tts": DEFAULT_TTS_MODEL, "music": DEFAULT_MUSIC_MODEL, "sfx": "sound_generation"}[job_type])
    rate = AUDIO_COSTS[model]
    if job_type == "tts":
        chars = len(params.get("text") or "")
        if chars == 0:
            raise ValueError("TTS estimate needs the text (cost is per character)")
        return rate["usd"] * chars / 1000.0
    if job_type == "sfx":
        seconds = float(params.get("duration") or SFX_DEFAULT_ESTIMATE_S)
        return rate["usd"] * seconds / 60.0
    if job_type == "music":
        seconds = float(params.get("duration") or 0.0)
        if seconds <= 0:
            raise ValueError("music estimate needs a duration")
        return rate["usd"] * seconds / 60.0
    raise ValueError(f"unknown ElevenLabs job type {job_type!r}")


def estimate_usd_micros(job_type: str, params: dict[str, Any]) -> int:
    """estimate_usd() in integer micro-dollars, rounded up and never below 1."""
    return max(1, math.ceil(estimate_usd(job_type, params) * 1_000_000))


class ElevenLabsProvider(GenerationProvider):
    """ElevenLabs provider adapter. Dry run (default) never touches the network."""

    def __init__(self, api_key: str | None = None, dry_run: bool | None = None,
                 output_dir: str | Path | None = None, api_base: str | None = None):
        self.dry_run = dry_run if dry_run is not None else config.DRY_RUN
        self.mock_balance = 5000.0  # Mock balance for dry-run
        self.api_key = api_key or os.getenv("ELEVENLABS_API_KEY")
        self.api_base = (api_base or API_BASE).rstrip("/")
        self.output_dir = Path(output_dir or os.getenv("ELEVENLABS_OUTPUT_DIR") or "./data/audio_cache")
        self._jobs: dict[str, dict[str, Any]] = {}
        if not self.dry_run and not self.api_key:
            raise ValueError("ELEVENLABS_API_KEY is required for live ElevenLabs calls")

    # ------------------------------------------------------------------ unsupported kinds

    async def submit_image(self, prompt: str, model: str, resolution: str = "2k",
                           quality: str = "high", references: list[str] | None = None) -> str:
        """Not supported by ElevenLabs (use Higgsfield)."""
        raise NotImplementedError("Image generation not supported by ElevenLabs")

    async def submit_video(self, start_image: str, prompt: str, model: str = "kling_3.0",
                           duration: float = 5.0, resolution: str = "1080p", draft: bool = False,
                           references: list[str] | None = None) -> str:
        """Not supported by ElevenLabs (use Higgsfield)."""
        raise NotImplementedError("Video generation not supported by ElevenLabs")

    # ------------------------------------------------------------------ live HTTP

    def _headers(self) -> dict[str, str]:
        return {"xi-api-key": self.api_key or "", "Content-Type": "application/json"}

    async def _post_audio(self, path: str, body: dict[str, Any], params: dict[str, Any] | None,
                          out_path: Path) -> httpx.Headers:
        """POST, write the audio durably to out_path, return the response headers (cost metadata)."""
        async with httpx.AsyncClient(base_url=self.api_base, timeout=httpx.Timeout(300.0)) as client:
            resp = await client.post(path, json=body, params=params or None, headers=self._headers())
        if resp.status_code != 200:
            raise ElevenLabsError(f"ElevenLabs {path} returned HTTP {resp.status_code}: {resp.text[:300]}",
                                 status_code=resp.status_code)
        if not resp.content:
            raise ElevenLabsError(f"ElevenLabs {path} returned no audio")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".part")
        tmp.write_bytes(resp.content)
        os.replace(tmp, out_path)
        return resp.headers

    def _out(self, out_path: str | Path | None, kind: str, key: dict[str, Any]) -> Path:
        if out_path:
            return Path(out_path)
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
        return self.output_dir / f"{kind}_{digest}.mp3"

    def _record(self, prefix: str, path: Path, cost_usd: float, meta: dict[str, Any]) -> str:
        job_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
        self._jobs[job_id] = {"path": str(path.resolve()), "cost": cost_usd, **meta}
        return job_id
    
    def _cost_meta(self, headers: Any, job_type: str, params: dict[str, Any],
                   characters: int | None) -> tuple[float, dict[str, Any]]:
        """(cost_usd, metadata) for one finished request.

        With the character-cost header: cost is credits x usd_per_credit(), cost_source
        "provider_header". Without it: the API USD estimate, cost_source "estimate" (flagged)."""
        credits = parse_character_cost(headers)
        if credits is not None:
            cost = credits_to_usd_micros(credits) / 1_000_000
            source = COST_SOURCE_HEADER
        else:
            cost = estimate_usd(job_type, params)
            source = COST_SOURCE_ESTIMATE
        return cost, {"credits": credits, "characters": characters, "cost_source": source}

    # ------------------------------------------------------------------ public API

    async def submit_audio(self, text: str, voice_id: str, model: str = "eleven_multilingual_v2",
                           settings: dict[str, Any] | None = None, out_path: str | Path | None = None,
                           seed: int | None = None) -> str:
        """TTS. Synchronous upstream; the audio file is written to disk and a job id returned."""
        if self.dry_run:
            return self._mock_job_id("aud")
        model_id = canonical_audio_model(model)
        if AUDIO_COSTS.get(model_id, {}).get("kind") != "tts":
            raise ValueError(f"{model!r} is not a priced ElevenLabs TTS model")
        body: dict[str, Any] = {"text": text, "model_id": model_id}
        if settings:
            body["voice_settings"] = settings
        if seed is not None:
            body["seed"] = seed
        path = self._out(out_path, "tts", {"t": text, "v": voice_id, "m": model_id, "s": settings, "seed": seed})
        headers = await self._post_audio(f"/v1/text-to-speech/{voice_id}", body,
                                         {"output_format": "mp3_44100_128"}, path)
        cost, meta = self._cost_meta(headers, "tts", {"model": model_id, "text": text}, len(text))
        return self._record("aud", path, cost, {"kind": "tts", "model": model_id, **meta})

    async def submit_sfx(self, text: str, duration: float | None = None, loop: bool = False,
                         prompt_influence: float | None = None, out_path: str | Path | None = None) -> str:
        """Sound effect via POST /v1/sound-generation (duration 0.5-30 s, or upstream's choice)."""
        if self.dry_run:
            return self._mock_job_id("sfx")
        body: dict[str, Any] = {"text": text, "model_id": SFX_UPSTREAM_MODEL, "loop": bool(loop)}
        if duration is not None:
            body["duration_seconds"] = float(duration)
        if prompt_influence is not None:
            body["prompt_influence"] = float(prompt_influence)
        path = self._out(out_path, "sfx", body)
        headers = await self._post_audio("/v1/sound-generation", body, {"output_format": "mp3_44100_128"}, path)
        cost, meta = self._cost_meta(headers, "sfx", {"model": "sound_generation", "duration": duration}, None)
        return self._record("sfx", path, cost, {"kind": "sfx", "model": "sound_generation", **meta})

    async def submit_music(self, prompt: str, duration: float, model: str = DEFAULT_MUSIC_MODEL,
                           force_instrumental: bool = True, seed: int | None = None,
                           out_path: str | Path | None = None) -> str:
        """Music via the real compose endpoint POST /v1/music (prompt + music_length_ms)."""
        if self.dry_run:
            return self._mock_job_id("mus")
        model_id = canonical_audio_model(model)
        if AUDIO_COSTS.get(model_id, {}).get("kind") != "music":
            raise ValueError(f"{model!r} is not an ElevenLabs music model")
        body: dict[str, Any] = {"prompt": prompt, "music_length_ms": int(round(duration * 1000)),
                                "model_id": model_id, "force_instrumental": bool(force_instrumental)}
        if seed is not None:
            body["seed"] = seed
        path = self._out(out_path, "music", body)
        headers = await self._post_audio("/v1/music", body, None, path)
        cost, meta = self._cost_meta(headers, "music", {"model": model_id, "duration": duration}, None)
        return self._record("mus", path, cost, {"kind": "music", "model": model_id, **meta})

    async def get_job_status(self, job_id: str) -> ProviderJob:
        """Audio is synchronous upstream: a known job is complete with a durable file:// URI."""
        if self.dry_run:
            await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
            return ProviderJob(job_id=job_id, status=ProviderJobStatus.COMPLETED, progress=1.0,
                               output_url=f"https://mock.elevenlabs.io/output/{job_id}.mp3", cost=30.0)
        job = self._jobs.get(job_id)
        if not job or not Path(job["path"]).is_file():
            return ProviderJob(job_id=job_id, status=ProviderJobStatus.FAILED, progress=0.0,
                               error="Unknown job or its audio file is missing")
        # G-AUD-COST: the actual-cost metadata travels with the job (raw)
        return ProviderJob(job_id=job_id, status=ProviderJobStatus.COMPLETED, progress=1.0,
                           output_url=Path(job["path"]).as_uri(), cost=job["cost"],
                           raw={"credits": job.get("credits"), "characters": job.get("characters"),
                                "cost_source": job.get("cost_source", COST_SOURCE_ESTIMATE)})

    async def get_balance(self) -> float:
        """Remaining credits this period (character_limit - character_count)."""
        if self.dry_run:
            return self.mock_balance
        async with httpx.AsyncClient(base_url=self.api_base, timeout=30.0) as client:
            resp = await client.get("/v1/user/subscription", headers=self._headers())
        if resp.status_code != 200:
            raise ElevenLabsError(f"ElevenLabs subscription returned HTTP {resp.status_code}")
        data = resp.json()
        return float(data["character_limit"]) - float(data["character_count"])

    async def estimate_cost(self, job_type: str, params: dict[str, Any]) -> float:
        """Legacy credit estimate (kept for the credit ledger). Use estimate_usd() for USD."""
        duration = params.get("duration", 0.0)
        if job_type == "tts":
            minutes = len(params.get("text", "")) / 1000.0 * 2.0  # ~1k chars = 2 min spoken
            return _LEGACY_CREDITS["tts_per_min"] * minutes
        if job_type == "music":
            return (duration / 95.0) * _LEGACY_CREDITS["music_per_95s"]
        if job_type == "sfx":
            return _LEGACY_CREDITS["sfx_per_5s"] * (duration / 5.0)
        return 0.0

    def _mock_job_id(self, prefix: str) -> str:
        """Generate mock job ID for dry-run."""
        return f"{prefix}_mock_{uuid.uuid4().hex[:12]}"
