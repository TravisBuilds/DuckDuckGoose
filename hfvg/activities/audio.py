"""Audio round activities (G-AUD-1b): VO / SFX / music inputs, mix + delivery, delivery QC.

Inputs come from a per-episode audio plan (``{episode}/audio/audio_plan.json``):
- a VO line is a locked/pre-rendered file (``path``) or text generated through ElevenLabs;
- SFX and music are local library files (``path``) or generated from a ``prompt``.

Every live generation is preflighted against model_caps (char / duration limits) BEFORE any
spend, passes the smoke gate, then reserves dollars on a budget line and authorizes the AUDIO
review round before the provider call; the hold is committed with the real cost or released on
failure. Generated files land at deterministic content-hash paths, so a retried activity reuses
the file instead of paying twice. DRY_RUN never calls a provider: missing inputs become
synthetic ffmpeg stand-ins flagged ``dry_run`` (their delivery verdict is DRY_RUN, never PASS).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError

DEFAULT_VO_MODEL = "eleven_v3"
DEFAULT_MUSIC_MODEL = "music_v1"
AUDIO_ROUND = "AUDIO"


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _db_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/studio.db")


def _check_elevenlabs_credentials() -> None:
    """Verify ElevenLabs API key before any spend/reservation. Raises non-retryable error if missing."""
    if _dry_run():
        return  # Dry run doesn't need credentials
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        raise ApplicationError(
            "ELEVENLABS_API_KEY environment variable is required for live ElevenLabs audio generation. "
            "Set the API key before starting live audio activities.",
            type="MissingCredentials",
            non_retryable=True
        )


def _beat(details: dict) -> None:
    try:
        activity.heartbeat(details)
    except RuntimeError:  # not inside an activity (CLI / direct call)
        pass


def _missing(msg: str, kind: str = "MissingAudioInput") -> ApplicationError:
    return ApplicationError(msg, type=kind, non_retryable=True)


def _key(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def generated_path(episode_id: str, kind: str, asset_id: str, key: str, ext: str = "mp3") -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in asset_id)
    return audio_mix.episode_dir(episode_id) / "audio" / "generated" / kind / f"{safe}_{key[:16]}.{ext}"


async def _stand_in(path: Path, seconds: float, freq: int) -> None:
    """Synthetic DRY_RUN stand-in (sine tone) - never a provider call."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".part" + path.suffix)
    await audio_mix.run_cmd(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
         f"sine=frequency={freq}:sample_rate={audio_mix.SR}:duration={seconds:.3f}",
         "-af", "volume=-18dB", "-ac", "2", str(tmp)], "dry-run stand-in")
    os.replace(tmp, path)


def _preflight(model: str, *, text: str | None = None, duration_s: float | None = None,
               music_length_ms: int | None = None) -> None:
    """Refuse before spend when the request breaks a documented model limit (model_caps)."""
    from hfvg import model_caps
    try:
        caps = model_caps.get_registry().resolve(model)
    except model_caps.UnknownModelError as e:
        raise _missing(f"preflight: {e}", "PreflightRefused") from None
    if caps.status in ("deprecated_upstream",):
        raise _missing(f"preflight: {model} is deprecated upstream; use "
                       f"{caps.upstream.get('replacement', 'a current model')}", "PreflightRefused")
    checks = [("text_max_chars", len(text) if text is not None else None, "characters"),
              ("duration_s", duration_s, "s"), ("duration_ms", music_length_ms, "ms")]
    for field, value, unit in checks:
        lim = caps.limit(field)
        if value is None or not lim or lim.unknown:
            continue
        if isinstance(lim.value, dict):
            lo, hi = lim.value.get("min", 0), lim.value.get("max", float("inf"))
        else:
            lo, hi = 0, lim.value
        if value < lo or value > hi:
            raise _missing(f"preflight: {model} {field} is {value} {unit}; allowed {lo}-{hi} {unit} "
                           "(no spend made)", "PreflightRefused")


AUDIO_BUDGET_LINE = {"tts": "el_vo_takes", "sfx": "el_sfx", "music": "el_music_one_take"}


async def _guarded_spend(episode_id: str, kind: str, asset_id: str, key: str, model: str,
                         usd_micros: int, call):
    """Ledger reservation + AUDIO round cap around one provider call; commit or release."""
    from hfvg.budget import BudgetLedger, RoundBudgetError
    from hfvg.studio_db import enforce_smoke_test_gate

    await enforce_smoke_test_gate(_db_path(), episode_id, f"generate_{kind}")
    ledger = BudgetLedger(_db_path())
    await ledger.init_db()
    line = AUDIO_BUDGET_LINE[kind]
    await ledger.ensure_audio_line(episode_id, line)
    job_id = f"el-{episode_id}-{kind}-{asset_id}-{key[:12]}"
    try:
        await ledger.authorize_round_spend(episode_id, AUDIO_ROUND, f"{kind}:{asset_id}", job_id, usd_micros,
                                           reason=f"{kind} {asset_id} ({model})")
    except RoundBudgetError as e:
        raise ApplicationError(str(e), type="RoundBudgetError", non_retryable=True) from None
    try:
        ok = await ledger.reserve(episode_id, line, usd_micros, f"{kind} {asset_id} ({model})", job_id=job_id)
    except ValueError as e:
        await ledger.release_round_hold(job_id)
        raise ApplicationError(str(e), type="BudgetExceeded", non_retryable=True) from None
    if not ok:
        await ledger.release_round_hold(job_id)
        raise ApplicationError(f"Budget line {line} would pass its stop threshold", type="BudgetExceeded",
                               non_retryable=True)
    await ledger.record_job(episode_id, job_id, asset_id, model, "final", line, usd_micros)
    try:
        result = await call()
    except BaseException:
        await ledger.release(episode_id, line, usd_micros, f"{kind} {asset_id} failed", job_id=job_id)
        raise
    actual = int(round(result.get("cost_usd", 0) * 1_000_000)) or usd_micros
    await ledger.commit(episode_id, line, usd_micros, actual, job_id, f"{kind} {asset_id}")
    return {**result, "job_id": job_id, "usd_micros": actual, "budget_line": line}


async def _provider_file(job_id: str, provider) -> tuple[str, float]:
    job = await provider.get_job_status(job_id)
    if not job.output_url or not job.output_url.startswith("file://"):
        raise RuntimeError(f"ElevenLabs job {job_id} has no durable file: {job.error or job.output_url}")
    from urllib.parse import unquote, urlparse
    return unquote(urlparse(job.output_url).path), float(job.cost or 0.0)


def _provider(out_dir: Path):
    from hfvg.providers.elevenlabs import ElevenLabsProvider
    return ElevenLabsProvider(dry_run=False, output_dir=out_dir)


def _result(path: Path, *, source: str, dry: bool = False, **extra: Any) -> dict[str, Any]:
    return {"path": str(path), "uri": path.resolve().as_uri(), "sha256": audio_mix.sha256_file(path),
            "source": source, "dry_run": dry, **extra}


@activity.defn
async def generate_voiceover(episode_id: str, script: str, voice_id: str, model: str = DEFAULT_VO_MODEL,
                             line_id: str = "vo", locked_path: str | None = None) -> dict[str, Any]:
    """One VO line: the locked/pre-rendered file when given, else ElevenLabs TTS (guarded)."""
    _beat({"stage": "voiceover", "line": line_id})
    if locked_path:
        p = Path(locked_path)
        if p.is_file():
            return _result(p, source="locked", line_id=line_id)
        if not _dry_run():
            raise _missing(f"Locked VO file for line {line_id} not found: {p}")
    key = _key("tts", script, voice_id, model)
    if _dry_run():
        out = generated_path(episode_id, "vo", line_id, key, "wav")
        if not out.is_file():
            await _stand_in(out, max(0.8, min(12.0, len(script or "") / 15.0)), 440)
        return _result(out, source="dry_run_stand_in", dry=True, line_id=line_id)
    if not script:
        raise _missing(f"VO line {line_id} has neither a locked file nor text")
    if not voice_id:
        raise _missing(f"VO line {line_id} needs a voice_id to generate")
    
    # Check credentials BEFORE preflight/reservation
    _check_elevenlabs_credentials()
    
    out = generated_path(episode_id, "vo", line_id, key)
    if out.is_file():  # content-hash reuse: already paid for
        return _result(out, source="generated_reused", line_id=line_id, model=model)
    _preflight(model, text=script)
    from hfvg.providers.elevenlabs import estimate_usd_micros
    amount = estimate_usd_micros("tts", {"model": model, "text": script})

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_audio(script, voice_id, model=model, out_path=out)
        path, cost = await _provider_file(job, provider)
        return {"path": path, "cost_usd": cost}

    spent = await _guarded_spend(episode_id, "tts", line_id, key, model, amount, call)
    return _result(out, source="generated", line_id=line_id, model=model, job_id=spent["job_id"],
                   usd_micros=spent["usd_micros"], characters=len(script))


@activity.defn
async def generate_sfx(episode_id: str, description: str, duration: float | None = 5.0,
                       cue_id: str = "sfx", library_path: str | None = None) -> dict[str, Any]:
    """One SFX cue: a library file by path, else ElevenLabs sound generation (guarded)."""
    _beat({"stage": "sfx", "cue": cue_id})
    if library_path:
        p = Path(library_path)
        if p.is_file():
            return _result(p, source="library", cue_id=cue_id)
        if not _dry_run():
            raise _missing(f"SFX library file for cue {cue_id} not found: {p}")
    key = _key("sfx", description, duration)
    if _dry_run():
        out = generated_path(episode_id, "sfx", cue_id, key, "wav")
        if not out.is_file():
            await _stand_in(out, float(duration or 2.0), 1200)
        return _result(out, source="dry_run_stand_in", dry=True, cue_id=cue_id)
    if not description:
        raise _missing(f"SFX cue {cue_id} has neither a library file nor a prompt")
    
    # Check credentials BEFORE preflight/reservation
    _check_elevenlabs_credentials()
    
    out = generated_path(episode_id, "sfx", cue_id, key)
    if out.is_file():
        return _result(out, source="generated_reused", cue_id=cue_id)
    _preflight("sound_generation", duration_s=duration)
    from hfvg.providers.elevenlabs import estimate_usd_micros
    amount = estimate_usd_micros("sfx", {"model": "sound_generation", "duration": duration})

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_sfx(description, duration=duration, out_path=out)
        path, cost = await _provider_file(job, provider)
        return {"path": path, "cost_usd": cost}

    spent = await _guarded_spend(episode_id, "sfx", cue_id, key, "sound_generation", amount, call)
    return _result(out, source="generated", cue_id=cue_id, job_id=spent["job_id"], usd_micros=spent["usd_micros"])


@activity.defn
async def generate_music(episode_id: str, style: str, duration: float, model: str = DEFAULT_MUSIC_MODEL,
                         cue_id: str = "music", library_path: str | None = None) -> dict[str, Any]:
    """Music bed: a library file by path, else ElevenLabs POST /v1/music (guarded)."""
    _beat({"stage": "music", "cue": cue_id})
    if library_path:
        p = Path(library_path)
        if p.is_file():
            return _result(p, source="library", cue_id=cue_id)
        if not _dry_run():
            raise _missing(f"Music library file not found: {p}")
    from hfvg.providers.elevenlabs import canonical_audio_model, estimate_usd_micros
    model = canonical_audio_model(model)
    key = _key("music", style, duration, model)
    if _dry_run():
        out = generated_path(episode_id, "music", cue_id, key, "wav")
        if not out.is_file():
            await _stand_in(out, float(duration), 220)
        return _result(out, source="dry_run_stand_in", dry=True, cue_id=cue_id)
    if not style:
        raise _missing("Music cue has neither a library file nor a prompt")
    
    # Check credentials BEFORE preflight/reservation
    _check_elevenlabs_credentials()
    
    out = generated_path(episode_id, "music", cue_id, key)
    if out.is_file():
        return _result(out, source="generated_reused", cue_id=cue_id, model=model)
    _preflight(model, music_length_ms=int(round(float(duration) * 1000)))
    amount = estimate_usd_micros("music", {"model": model, "duration": duration})

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_music(style, float(duration), model=model, out_path=out)
        path, cost = await _provider_file(job, provider)
        return {"path": path, "cost_usd": cost}

    spent = await _guarded_spend(episode_id, "music", cue_id, key, model, amount, call)
    return _result(out, source="generated", cue_id=cue_id, model=model, job_id=spent["job_id"],
                   usd_micros=spent["usd_micros"])


@activity.defn
async def load_audio_plan(episode_id: str, plan_path: str | None = None) -> dict[str, Any]:
    """Load + validate the episode audio plan. Missing plan: non-retryable live, empty in dry run."""
    p = Path(plan_path) if plan_path else audio_mix.audio_plan_path(episode_id)
    if not p.is_file():
        if _dry_run():
            plan = audio_mix.normalize_plan({}, audio_mix.episode_dir(episode_id))
            return {**plan, "plan_found": False, "plan_path": str(p)}
        raise _missing(f"No audio plan for {episode_id}: {p}", "MissingAudioPlan")
    try:
        plan = audio_mix.load_plan_file(p)
    except AudioInputError as e:
        raise _missing(str(e), "InvalidAudioPlan") from None
    return {**plan, "plan_found": True, "plan_path": str(p)}


def _dry_manifest(episode_id: str, mute_path: str, reason: str) -> dict[str, Any]:
    paths = audio_mix.delivery_paths(episode_id)
    return {"episode_id": episode_id, "dry_run": True, "dry_reason": reason, "mute_path": mute_path,
            "paths": paths, "outputs": {}, "input_key": None, "reused": False}


@activity.defn
async def mix_and_deliver(episode_id: str, mute_path: str, plan: dict[str, Any],
                          picture_duration: float | None = None) -> dict[str, Any]:
    """Mix the resolved plan under the locked mute picture and write every deliverable.

    Live: a missing picture or input file is a non-retryable error. DRY_RUN with a stand-in
    picture (the dry assembly writes no file) returns the deterministic paths flagged dry_run."""
    _beat({"stage": "mix_start", "episode": episode_id})
    mute = Path(mute_path) if mute_path else None
    if mute is None or not mute.is_file():
        if _dry_run():
            return _dry_manifest(episode_id, mute_path, "picture is a dry-run stand-in (no file)")
        raise _missing(f"Mute picture not found for {episode_id}: {mute_path}", "MissingPicture")
    if not plan.get("vo", {}).get("lines") and not plan.get("music") and not plan.get("sfx"):
        if _dry_run():
            return _dry_manifest(episode_id, mute_path, "no audio plan in dry run")
        raise _missing(f"Audio plan for {episode_id} has no VO, music or SFX", "InvalidAudioPlan")
    try:
        manifest = await audio_mix.mix_and_deliver(episode_id, str(mute), plan,
                                                   picture_duration=picture_duration, heartbeat=_beat)
    except AudioInputError as e:
        raise _missing(str(e), type(e).__name__) from None
    stand_ins = sorted(plan.get("dry_inputs") or [])
    if stand_ins:
        manifest = {**manifest, "dry_run": True, "dry_reason": f"stand-in inputs: {stand_ins}"}
    return manifest


@activity.defn
async def check_delivery_qc(episode_id: str, manifest: dict[str, Any], plan: dict[str, Any],
                            picture_duration: float | None = None) -> dict[str, Any]:
    """Delivery QC on the written files. DRY_RUN manifests get the non-blocking DRY_RUN verdict."""
    _beat({"stage": "delivery_qc", "episode": episode_id})
    if manifest.get("dry_run") and not manifest.get("outputs"):
        return {"verdict": "DRY_RUN", "blocking": False, "checks": [], "failed": [],
                "reason": manifest.get("dry_reason"), "input_key": None}
    qc = await audio_mix.delivery_qc(manifest, plan, picture_duration)
    if manifest.get("dry_run"):
        # Measured for real, but stand-in audio is never a PASS for a live delivery.
        qc = {**qc, "verdict": "DRY_RUN" if qc["verdict"] == "PASS" else qc["verdict"],
              "reason": manifest.get("dry_reason")}
        qc["blocking"] = qc["verdict"] in ("FAIL", "NEEDS_REVIEW")
    return qc
