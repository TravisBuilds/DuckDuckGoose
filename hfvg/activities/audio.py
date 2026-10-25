"""Audio generation and mixing activities."""

import asyncio
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg.config import config


MIN_VO_GAP_S = 0.3  # Minimum gap between VO lines (0.3-0.4s per spec)
VO_DUCK_DB = -4.0  # BGM duck under VO
SFX_BED_DUCK_DB = -3.0  # SFX bed duck under VO
VOX_DUCK_DB = -14.0  # Animal calls duck under VO
VO_BUS_GAIN_DB = -2.5  # VO bus trim
MASTER_GAIN_DB = 4.7  # Makeup gain into limiter
ALIMITER_CEILING = 0.86  # True-peak ceiling (target < 0 dBTP)
LOUDNESS_TARGET_LUFS = -12.5  # EBU R128 target
LOUDNESS_TOLERANCE_LUFS = 1.0  # Acceptable deviation
TRUE_PEAK_MAX_DBTP = -0.5  # Maximum true peak for QC
PHONE_MAX_MB = 50  # Phone encode size limit
SAMPLE_RATE = 48000


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def delivery_dir(episode_id: str, output_dir: str | None = None) -> Path:
    """Deterministic directory for delivery artifacts."""
    if output_dir:
        return Path(output_dir)
    return Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "delivery"


async def _run_ffmpeg(cmd: list[str], what: str) -> str:
    """Run ffmpeg/ffprobe without blocking, return stdout."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed ({proc.returncode}): {err.decode(errors='replace')[-1000:]}")
    return out.decode(errors="replace")


async def _probe_duration(path: Path) -> float:
    out = await _run_ffmpeg(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
        f"ffprobe duration {path.name}",
    )
    return float(out.strip())


def _content_hash(data: str | bytes) -> str:
    """SHA256 content hash for reuse keys."""
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()[:16]


@activity.defn
async def generate_voiceover(
    episode_id: str,
    script: str | None = None,
    voice_id: str | None = None,
    model: str = "eleven_v3",
    locked_audio_path: str | None = None,
) -> str:
    """
    Generate or use pre-rendered voiceover for the episode.
    
    If locked_audio_path is provided, copies the locked file to the delivery directory.
    Otherwise, generates via ElevenLabs (requires script and voice_id).
    
    Returns: file:// URI of the VO audio.
    """
    activity.heartbeat({"stage": "voiceover_start"})
    
    if locked_audio_path:
        # Use pre-rendered/locked VO
        src = Path(locked_audio_path)
        if not src.is_file():
            if _dry_run():
                await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
                activity.logger.info(f"[DRY-RUN] Locked VO (placeholder): {locked_audio_path}")
                return f"file://{src}"
            raise ApplicationError(
                f"Locked VO file not found: {locked_audio_path}",
                type="MissingLockedVO", non_retryable=True
            )
        
        out_dir = delivery_dir(episode_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"vo_{src.name}"
        
        if not dest.exists():
            activity.heartbeat({"stage": "copying_locked_vo"})
            import shutil
            await asyncio.to_thread(shutil.copy2, src, dest)
        
        activity.logger.info(f"Using locked VO: {dest}")
        return f"file://{dest}"
    
    # Generate via ElevenLabs
    if not script or not voice_id:
        raise ApplicationError(
            "script and voice_id required when locked_audio_path not provided",
            non_retryable=True
        )
    
    if _dry_run():
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated VO with {voice_id}, model {model}")
        return f"file:///tmp/dry_vo_{episode_id}.wav"
    
    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_voiceover")
    
    # Preflight: check character limit
    from hfvg.data.model_caps import get_model_caps
    caps = get_model_caps(model)
    char_limit = caps.get("limits", {}).get("text_max_chars", {}).get("value")
    if char_limit and len(script) > char_limit:
        raise ApplicationError(
            f"Script exceeds {model} character limit ({len(script)} > {char_limit})",
            type="CharLimitExceeded", non_retryable=True
        )
    
    # Reserve budget
    from hfvg.credit_ledger import reserve_credits, commit_credits
    
    # Character-based cost estimation (0.08 USD per 1k chars for eleven_v3)
    chars = len(script)
    cost_usd = (chars / 1000.0) * 0.08  # Conservative estimate
    
    activity.heartbeat({"stage": "reserving_budget", "chars": chars, "est_usd": cost_usd})
    reservation_id = await reserve_credits(episode_id, cost_usd, f"VO generation ({chars} chars)")
    
    try:
        # Call ElevenLabs
        from hfvg.providers.elevenlabs import ElevenLabsProvider
        provider = ElevenLabsProvider(dry_run=False)
        
        activity.heartbeat({"stage": "calling_elevenlabs", "model": model})
        job_id = await provider.submit_audio(text=script, voice_id=voice_id, model=model)
        
        # Poll for completion
        import time
        start = time.time()
        while time.time() - start < 300:  # 5 min timeout
            activity.heartbeat({"stage": "polling_elevenlabs", "job": job_id})
            job = await provider.get_job_status(job_id)
            
            if job.status.value == "completed":
                # Write audio to delivery directory
                out_dir = delivery_dir(episode_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"vo_{episode_id}_{_content_hash(script)}.wav"
                
                # Download and convert to WAV
                # (provider returns audio bytes or URL; adapt as needed)
                audio_bytes = job.audio_bytes if hasattr(job, 'audio_bytes') else b""
                if audio_bytes:
                    out_path.write_bytes(audio_bytes)
                else:
                    # Download from URL
                    import httpx
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(job.output_url)
                        resp.raise_for_status()
                        out_path.write_bytes(resp.content)
                
                # Commit actual cost
                actual_cost = job.cost or cost_usd
                await commit_credits(reservation_id, actual_cost)
                
                activity.logger.info(f"VO generated: {out_path} ({chars} chars, {actual_cost:.2f} USD)")
                return f"file://{out_path}"
            
            elif job.status.value == "failed":
                raise ApplicationError(
                    f"ElevenLabs generation failed: {job.error or 'unknown'}",
                    type="ProviderError", non_retryable=True
                )
            
            await asyncio.sleep(2)
        
        raise ApplicationError("VO generation timed out", type="Timeout", non_retryable=False)
    
    except Exception as e:
        # Release reservation on failure
        from hfvg.credit_ledger import release_credits
        await release_credits(reservation_id)
        raise


@activity.defn
async def generate_sfx(episode_id: str, description: str, duration: float = 5.0) -> str:
    """
    Generate sound effects from description.
    
    Returns: file:// URI of the generated SFX.
    """
    activity.heartbeat({"stage": "generating_sfx", "desc": description[:50]})

    if _dry_run():
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated SFX: {description}")
        return f"file:///tmp/dry_sfx_{episode_id}.wav"

    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_sfx")
    
    # Reserve budget (SFX: ~0.12 USD per minute)
    from hfvg.credit_ledger import reserve_credits, commit_credits
    cost_usd = (duration / 60.0) * 0.12
    reservation_id = await reserve_credits(episode_id, cost_usd, f"SFX generation ({duration:.1f}s)")
    
    try:
        from hfvg.providers.elevenlabs import ElevenLabsProvider
        provider = ElevenLabsProvider(dry_run=False)
        
        # Call sound generation endpoint
        activity.heartbeat({"stage": "calling_elevenlabs_sfx"})
        job_id = await provider.submit_sfx(text=description, duration=duration)
        
        # Poll and download
        import time
        start = time.time()
        while time.time() - start < 300:
            activity.heartbeat({"stage": "polling_sfx", "job": job_id})
            job = await provider.get_job_status(job_id)
            
            if job.status.value == "completed":
                out_dir = delivery_dir(episode_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"sfx_{_content_hash(description)}_{int(duration*10)}.wav"
                
                # Download
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get(job.output_url)
                    resp.raise_for_status()
                    out_path.write_bytes(resp.content)
                
                await commit_credits(reservation_id, job.cost or cost_usd)
                activity.logger.info(f"SFX generated: {out_path}")
                return f"file://{out_path}"
            
            elif job.status.value == "failed":
                raise ApplicationError(f"SFX generation failed: {job.error}", non_retryable=True)
            
            await asyncio.sleep(2)
        
        raise ApplicationError("SFX generation timed out", non_retryable=False)
    
    except Exception as e:
        from hfvg.credit_ledger import release_credits
        await release_credits(reservation_id)
        raise


@activity.defn
async def generate_music(episode_id: str, prompt: str, duration: float, model: str = "music_v1") -> str:
    """
    Generate background music via ElevenLabs music endpoint.
    
    Returns: file:// URI of the generated music.
    """
    activity.heartbeat({"stage": "generating_music", "duration": duration})

    if _dry_run():
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated music: {prompt[:50]}, {duration}s")
        return f"file:///tmp/dry_music_{episode_id}.mp3"

    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_music")
    
    # Check duration limits (max 300s per model_caps)
    if duration > 300:
        raise ApplicationError(
            f"Music duration {duration}s exceeds 300s limit",
            non_retryable=True
        )
    
    # Reserve budget (music: ~0.15 USD per minute)
    from hfvg.credit_ledger import reserve_credits, commit_credits
    cost_usd = (duration / 60.0) * 0.15
    reservation_id = await reserve_credits(episode_id, cost_usd, f"Music generation ({duration:.1f}s)")
    
    try:
        from hfvg.providers.elevenlabs import ElevenLabsProvider
        provider = ElevenLabsProvider(dry_run=False)
        
        activity.heartbeat({"stage": "calling_elevenlabs_music"})
        job_id = await provider.submit_music(prompt=prompt, duration_ms=int(duration * 1000), model=model)
        
        # Poll and download
        import time
        start = time.time()
        while time.time() - start < 600:  # 10 min timeout for music
            activity.heartbeat({"stage": "polling_music", "job": job_id})
            job = await provider.get_job_status(job_id)
            
            if job.status.value == "completed":
                out_dir = delivery_dir(episode_id)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = out_dir / f"music_{_content_hash(prompt)}_{int(duration)}.mp3"
                
                # Download
                import httpx
                async with httpx.AsyncClient() as client:
                    resp = await client.get(job.output_url)
                    resp.raise_for_status()
                    out_path.write_bytes(resp.content)
                
                await commit_credits(reservation_id, job.cost or cost_usd)
                activity.logger.info(f"Music generated: {out_path}")
                return f"file://{out_path}"
            
            elif job.status.value == "failed":
                raise ApplicationError(f"Music generation failed: {job.error}", non_retryable=True)
            
            await asyncio.sleep(3)
        
        raise ApplicationError("Music generation timed out", non_retryable=False)
    
    except Exception as e:
        from hfvg.credit_ledger import release_credits
        await release_credits(reservation_id)
        raise
