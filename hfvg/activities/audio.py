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
    if not os.getenv("ELEVENLABS_API_KEY", "").strip():
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


DRY_VO_CHARS_PER_S = 15.0  # dry stand-in VO length: the planned text at a typical speaking rate


def dry_vo_seconds(script: str | None) -> float:
    """B7: the dry stand-in VO line length estimated from the line's characters (0.8-12 s)."""
    return round(max(0.8, min(12.0, len(script or "") / DRY_VO_CHARS_PER_S)), 3)


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


def _reservation(fn, kind: str, params: dict[str, Any]) -> int:
    """The pre-call reservation; a bad ELEVENLABS_USD_PER_CREDIT is a non-retryable refusal."""
    try:
        return fn(kind, params)
    except ValueError as e:
        raise _missing(f"{kind} cost estimate refused: {e} (no spend made)", "InvalidCostConfig") from None


AUDIO_BUDGET_LINE = {"tts": "el_vo_takes", "sfx": "el_sfx", "music": "el_music_one_take"}


async def _guarded_spend(episode_id: str, kind: str, asset_id: str, key: str, model: str,
                         usd_micros: int, call, *, estimate_micros: int | None = None):
    """Ledger reservation + AUDIO round cap around one provider call; commit the actual or release.

    usd_micros is the reservation (provider.reservation_usd_micros: an upper bound). The commit is
    the actual from the provider's character-cost header (credits x ELEVENLABS_USD_PER_CREDIT);
    without the header it falls back to estimate_micros (API price estimate) and the job row is
    flagged cost_source="estimate". The unused part of the hold is released by commit()."""
    from hfvg.budget import BudgetLedger, RoundBudgetError
    from hfvg.providers.elevenlabs import COST_SOURCE_HEADER, credits_to_usd_micros
    from hfvg.studio_db import enforce_smoke_test_gate

    await enforce_smoke_test_gate(_db_path(), episode_id, f"generate_{kind}")
    ledger = BudgetLedger(_db_path())
    await ledger.init_db()
    line = AUDIO_BUDGET_LINE[kind]
    await ledger.ensure_audio_line(episode_id, line)
    job_id = f"el-{episode_id}-{kind}-{asset_id}-{key[:12]}"
    
    # Defect 8: Balance preflight gate before any reserve/submit
    # No-op in DRY_RUN mode or with unknown/missing balance - only blocks on known balance below need
    if not _dry_run():
        from hfvg.balance_monitor import BalanceMonitor, ELEVENLABS_AUTO_TOPUP_ENABLED
        balance_monitor = BalanceMonitor(_db_path())
        passed, reason = await balance_monitor.check_preflight_balance(
            provider="elevenlabs",
            estimate_usd_micros=usd_micros,
            auto_topup_enabled=ELEVENLABS_AUTO_TOPUP_ENABLED,
        )
        if not passed:
            raise ApplicationError(
                f"Balance check failed: {reason}",
                type="InsufficientBalance",
                non_retryable=True,
            )
        activity.logger.info(f"Balance preflight passed for {kind} {asset_id}: {reason}")
    
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
    # The job row exists (status reserved) for the whole provider call, as for every paid job.
    if not await ledger.record_job(episode_id, job_id, asset_id, model, "final", line, usd_micros):
        await ledger.reopen_released_job(job_id)  # retry of a failed attempt: track this hold too
    try:
        result = await call()
    except BaseException as e:
        await ledger.release(episode_id, line, usd_micros, f"{kind} {asset_id} failed", job_id=job_id)
        from hfvg.providers.elevenlabs import ElevenLabsError
        if isinstance(e, ElevenLabsError) and e.permanent:
            raise ApplicationError(str(e), type="ElevenLabsPermanentError", non_retryable=True) from None
        raise

    credits = result.get("credits")
    if result.get("cost_source") == COST_SOURCE_HEADER and credits is not None:
        actual = credits_to_usd_micros(float(credits))
        cost_source = COST_SOURCE_HEADER
    else:
        actual = estimate_micros if estimate_micros is not None else usd_micros
        cost_source = "estimate"
        activity.logger.warning(f"{kind} {asset_id}: no character-cost header; committing the estimate "
                                f"({actual} usd_micros), flagged cost_source=estimate")
    over = actual > usd_micros
    if over:  # never hidden: commit() spends the actual and logs an overage transaction
        activity.logger.warning(f"{kind} {asset_id}: actual {actual} usd_micros exceeds the reservation "
                                f"{usd_micros}; check ELEVENLABS_USD_PER_CREDIT / upstream pricing")
    await ledger.commit(episode_id, line, usd_micros, actual, job_id, f"{kind} {asset_id}",
                        characters=result.get("characters"), credits=credits, cost_source=cost_source)
    return {**result, "job_id": job_id, "usd_micros": actual, "reserved_usd_micros": usd_micros,
            "budget_line": line, "cost_source": cost_source, "over_reservation": over}


async def _provider_file(job_id: str, provider) -> dict[str, Any]:
    """The finished job's durable file path, cost and cost metadata (credits/characters/cost_source)."""
    job = await provider.get_job_status(job_id)
    if not job.output_url or not job.output_url.startswith("file://"):
        raise RuntimeError(f"ElevenLabs job {job_id} has no durable file: {job.error or job.output_url}")
    from urllib.parse import unquote, urlparse
    raw = job.raw or {}
    return {"path": unquote(urlparse(job.output_url).path), "cost_usd": float(job.cost or 0.0),
            "credits": raw.get("credits"), "characters": raw.get("characters"),
            "cost_source": raw.get("cost_source")}


def _provider(out_dir: Path):
    from hfvg.providers.elevenlabs import ElevenLabsProvider
    return ElevenLabsProvider(dry_run=False, output_dir=out_dir)


def _result(path: Path, *, source: str, dry: bool = False, **extra: Any) -> dict[str, Any]:
    return {"path": str(path), "uri": path.resolve().as_uri(), "sha256": audio_mix.sha256_file(path),
            "source": source, "dry_run": dry, **extra}


@activity.defn
async def generate_voiceover(episode_id: str, script: str, voice_id: str, model: str = DEFAULT_VO_MODEL,
                             line_id: str = "vo", locked_path: str | None = None,
                             take: int = 1) -> dict[str, Any]:
    """One VO line: the locked/pre-rendered file when given, else ElevenLabs TTS (guarded).
    ``take`` > 1 is a deliberate re-roll of the same text: it gets its own content key (so it is
    a new guarded generation, then reused by hash); take 1 keeps the original key."""
    _beat({"stage": "voiceover", "line": line_id})
    if locked_path:
        p = Path(locked_path)
        if p.is_file():
            return _result(p, source="locked", line_id=line_id)
        if not _dry_run():
            raise _missing(f"Locked VO file for line {line_id} not found: {p}")
    take = int(take or 1)
    key = _key("tts", script, voice_id, model) if take <= 1 else _key("tts", script, voice_id, model, take)
    if _dry_run():
        out = generated_path(episode_id, "vo", line_id, key, "wav")
        if not out.is_file():
            await _stand_in(out, dry_vo_seconds(script), 440)
        return _result(out, source="dry_run_stand_in", dry=True, line_id=line_id)
    if not script:
        raise _missing(f"VO line {line_id} has neither a locked file nor text")
    if not voice_id:
        raise _missing(f"VO line {line_id} needs a voice_id to generate")
    
    out = generated_path(episode_id, "vo", line_id, key)
    if out.is_file():  # content-hash reuse: already paid for (needs no key, spends nothing)
        return _result(out, source="generated_reused", line_id=line_id, model=model)
    _check_elevenlabs_credentials()  # G-AUD-KEY tts: before preflight, override log, holds, reservation
    _preflight(model, text=script)
    from hfvg.providers.elevenlabs import estimate_usd_micros, reservation_usd_micros
    params = {"model": model, "text": script}
    amount = _reservation(reservation_usd_micros, "tts", params)

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_audio(script, voice_id, model=model, out_path=out)
        return await _provider_file(job, provider)

    spent = await _guarded_spend(episode_id, "tts", line_id, key, model, amount, call,
                                 estimate_micros=estimate_usd_micros("tts", params))
    return _result(out, source="generated", line_id=line_id, model=model, job_id=spent["job_id"],
                   usd_micros=spent["usd_micros"], reserved_usd_micros=spent["reserved_usd_micros"],
                   characters=len(script), credits=spent.get("credits"), cost_source=spent["cost_source"],
                   over_reservation=spent["over_reservation"])


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
    
    out = generated_path(episode_id, "sfx", cue_id, key)
    if out.is_file():
        return _result(out, source="generated_reused", cue_id=cue_id)
    _check_elevenlabs_credentials()  # G-AUD-KEY sfx: before preflight, override log, holds, reservation
    _preflight("sound_generation", duration_s=duration)
    from hfvg.providers.elevenlabs import estimate_usd_micros, reservation_usd_micros
    params = {"model": "sound_generation", "duration": duration}
    amount = _reservation(reservation_usd_micros, "sfx", params)

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_sfx(description, duration=duration, out_path=out)
        return await _provider_file(job, provider)

    spent = await _guarded_spend(episode_id, "sfx", cue_id, key, "sound_generation", amount, call,
                                 estimate_micros=estimate_usd_micros("sfx", params))
    return _result(out, source="generated", cue_id=cue_id, job_id=spent["job_id"],
                   usd_micros=spent["usd_micros"], reserved_usd_micros=spent["reserved_usd_micros"],
                   credits=spent.get("credits"), cost_source=spent["cost_source"],
                   over_reservation=spent["over_reservation"])


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
    
    out = generated_path(episode_id, "music", cue_id, key)
    if out.is_file():
        return _result(out, source="generated_reused", cue_id=cue_id, model=model)
    _check_elevenlabs_credentials()  # G-AUD-KEY music: before preflight, override log, holds, reservation
    _preflight(model, music_length_ms=int(round(float(duration) * 1000)))
    from hfvg.providers.elevenlabs import reservation_usd_micros
    params = {"model": model, "duration": duration}
    amount = _reservation(reservation_usd_micros, "music", params)

    async def call():
        provider = _provider(out.parent)
        job = await provider.submit_music(style, float(duration), model=model, out_path=out)
        return await _provider_file(job, provider)

    spent = await _guarded_spend(episode_id, "music", cue_id, key, model, amount, call,
                                 estimate_micros=estimate_usd_micros("music", params))
    return _result(out, source="generated", cue_id=cue_id, model=model, job_id=spent["job_id"],
                   usd_micros=spent["usd_micros"], reserved_usd_micros=spent["reserved_usd_micros"],
                   credits=spent.get("credits"), cost_source=spent["cost_source"],
                   over_reservation=spent["over_reservation"])


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
        if plan.get("timeline_ref"):
            # GAP-23: the plan's times follow the CURRENT edit (a shot trimmed / dropped at G_FINAL_CUT)
            from hfvg.activities.media import MUTE_MANIFEST_NAME
            edit_manifest = audio_mix.episode_dir(episode_id) / "edit" / MUTE_MANIFEST_NAME
            if edit_manifest.is_file():
                current = audio_mix.timeline_from_manifest(json.loads(edit_manifest.read_text()))
                plan = audio_mix.remap_plan_to_edit(plan, current)
            elif not _dry_run():
                raise AudioInputError(f"audio plan has a timeline_ref but no edit manifest: {edit_manifest}")
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
    picture_stand_in = False
    if mute is None or not mute.is_file():
        if not _dry_run():
            raise _missing(f"Mute picture not found for {episode_id}: {mute_path}", "MissingPicture")
        if not picture_duration or picture_duration <= 0:
            return _dry_manifest(episode_id, mute_path, "picture is a dry-run stand-in (no file, no length)")
    if not plan.get("vo", {}).get("lines") and not plan.get("music") and not plan.get("sfx"):
        if _dry_run():
            return _dry_manifest(episode_id, mute_path, "no audio plan in dry run")
        raise _missing(f"Audio plan for {episode_id} has no VO, music or SFX", "InvalidAudioPlan")
    if mute is None or not mute.is_file():
        # B7: the dry assembly renders nothing; a black stand-in picture of the PLANNED length lets
        # VO placement, ducking, subtitles and loudness run for real (manifest stays dry_run).
        mute = await _stand_in_picture(episode_id, float(picture_duration))
        picture_stand_in = True
    try:
        manifest = await audio_mix.mix_and_deliver(episode_id, str(mute), plan,
                                                   picture_duration=picture_duration, heartbeat=_beat)
    except AudioInputError as e:
        raise _missing(str(e), type(e).__name__) from None
    stand_ins = sorted(plan.get("dry_inputs") or []) + (["picture"] if picture_stand_in else [])
    if stand_ins:
        manifest = {**manifest, "dry_run": True, "dry_reason": f"stand-in inputs: {stand_ins}"}
    return manifest


async def _stand_in_picture(episode_id: str, seconds: float) -> Path:
    """DRY_RUN black mute picture (540x960, 24 fps) of exactly the planned picture length."""
    out = audio_mix.episode_dir(episode_id) / "dry" / f"mute_standin_{int(round(seconds * 1000))}ms.mp4"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".part" + out.suffix)
    await audio_mix.run_cmd(
        ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i",
         f"color=c=black:s=540x960:r=24:d={seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-an", str(tmp)], "dry-run stand-in picture")
    os.replace(tmp, out)
    return out


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
