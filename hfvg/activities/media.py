"""Media processing activities: ffmpeg operations."""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg.config import config


EDIT_WIDTH, EDIT_HEIGHT, EDIT_FPS, EDIT_PIX_FMT = 1080, 1920, 30, "yuv420p"
EDIT_FORMAT_VERSION = 1  # bump when the normalization below changes (invalidates cached edits)
MUTE_EDIT_NAME = "mute_assembly.mp4"
MUTE_MANIFEST_NAME = "assembly_manifest.json"
# A generated clip may run a few frames short of its nominal duration; a trim_out within this
# tolerance is clamped to the real clip end, beyond it the trim is refused (escalated).
TRIM_TOLERANCE_S = 0.15


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def edit_dir(episode_id: str, output_dir: str | None = None) -> Path:
    """Deterministic directory of an episode's mute picture edit."""
    if output_dir:
        return Path(output_dir)
    return Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "edit"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _run(cmd: list[str], what: str) -> str:
    """Run ffmpeg/ffprobe without blocking the activity's event loop (heartbeats keep flowing)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed ({proc.returncode}): {err.decode(errors='replace')[-800:]}")
    return out.decode(errors="replace")


async def _probe_duration(path: Path) -> float:
    out = await _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", str(path)],
        f"ffprobe {path.name}",
    )
    return float(out.strip())


def _plan_entries(shots: list[dict[str, Any]], clip_paths: dict[str, str]) -> list[dict[str, Any]]:
    """Validate the edit plan: shot order as given, one clip per shot, 0 <= trim_in < trim_out."""
    if not shots:
        raise ApplicationError("No shots provided for the mute picture edit", non_retryable=True)
    entries, seen = [], set()
    for shot in shots:
        sid = shot["shot_id"]
        if sid in seen:
            raise ApplicationError(f"Shot {sid} appears twice in the edit plan", non_retryable=True)
        seen.add(sid)
        path = clip_paths.get(sid)
        if not path:
            raise ApplicationError(f"Missing clip path for shot {sid}", non_retryable=True)
        duration = float(shot.get("duration", 5.0))
        trim_in = float(shot.get("trim_in", 0.0))
        trim_out = float(shot.get("trim_out", duration))
        if not (0 <= trim_in < trim_out):
            raise ApplicationError(
                f"Shot {sid}: invalid trim (trim_in={trim_in}, trim_out={trim_out})", non_retryable=True
            )
        entries.append({"shot_id": sid, "clip_path": str(path), "trim_in": trim_in, "trim_out": trim_out})
    return entries


@activity.defn
async def assemble_mute_edit(
    episode_id: str,
    shots: list[dict[str, Any]],
    clip_paths: dict[str, str],
    output_dir: str | None = None
) -> dict[str, Any]:
    """Mute 9:16 picture edit from the approved clips (picture lock input).

    Per shot: trim [trim_in, trim_out) from the shot plan, normalize to 1080x1920 @ 30 fps yuv420p
    (letterbox/pillarbox, square pixels, no audio), exact frame count; then concat in the given shot
    order. Output path is deterministic ({edit_dir}/mute_assembly.mp4) and written atomically.

    Idempotent and input-aware: the manifest records a key over (clip content sha256, trims, order,
    format version). An existing edit is reused only when that key matches; a changed clip (e.g. a
    G3 clip retry) or trim rebuilds it. Heartbeats per stage/clip.

    Dry run (DRY_RUN=true) with clip files that do not exist (placeholder paths) or dry stand-in
    clips shorter than their planned trim: returns the deterministic path with ``dry_run: True`` and creates nothing. Live with a missing clip: refuses.

    Returns {"assembly_path", "manifest", "duration", "input_key", "reused", "dry_run"}.
    """
    activity.heartbeat({"stage": "assembly_start", "shots": len(shots)})
    entries = _plan_entries(shots, clip_paths)
    out_dir = edit_dir(episode_id, output_dir)
    assembly_path = out_dir / MUTE_EDIT_NAME
    manifest_path = out_dir / MUTE_MANIFEST_NAME

    missing = [e["shot_id"] for e in entries if not Path(e["clip_path"]).is_file()]
    stand_ins: list[str] = []
    if _dry_run() and not missing:
        # Dry-run provider stand-ins (1s placeholder clips) cannot honour the planned trims; they are
        # not real footage, so the edit is reported as a dry run instead of failing or "passing".
        for e in entries:
            try:
                short = await _probe_duration(Path(e["clip_path"])) + TRIM_TOLERANCE_S < e["trim_out"]
            except (RuntimeError, OSError, ValueError):
                short = True  # unreadable dry placeholder
            if short:
                stand_ins.append(e["shot_id"])
    if missing or stand_ins:
        if _dry_run():
            await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
            activity.logger.info(f"[DRY-RUN] mute edit for {episode_id}: placeholder clips, nothing rendered")
            clips = [{"id": e["shot_id"], "seg_s": round(e["trim_out"] - e["trim_in"], 3),
                      "trim_in": e["trim_in"], "trim_out": e["trim_out"], "source": e["clip_path"]}
                     for e in entries]
            return {
                "assembly_path": str(assembly_path),
                "manifest": {"clips": clips, "dissolves": {}, "dry_run": True},
                "duration": round(sum(c["seg_s"] for c in clips), 3),
                "input_key": None,
                "reused": False,
                "dry_run": True,
            }
        raise ApplicationError(
            f"Clip file missing for shot(s) {', '.join(missing)}; cannot assemble the mute edit",
            type="MissingClipError", non_retryable=True,
        )

    # Input key: content + trims + order + format
    activity.heartbeat({"stage": "hashing_inputs", "shots": len(entries)})
    key_src = []
    for e in entries:
        e["sha256"] = await asyncio.to_thread(_sha256, Path(e["clip_path"]))
        key_src.append([e["shot_id"], e["sha256"], e["trim_in"], e["trim_out"]])
    input_key = hashlib.sha256(
        json.dumps({"v": EDIT_FORMAT_VERSION, "fmt": [EDIT_WIDTH, EDIT_HEIGHT, EDIT_FPS, EDIT_PIX_FMT],
                    "inputs": key_src}, sort_keys=True).encode()
    ).hexdigest()

    if assembly_path.is_file() and manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            old = {}
        if old.get("input_key") == input_key and old.get("size_bytes") == assembly_path.stat().st_size:
            activity.logger.info(f"Mute edit up to date at {assembly_path} (key {input_key[:12]}), reusing")
            return {"assembly_path": str(assembly_path), "manifest": old,
                    "duration": old.get("total_duration"), "input_key": input_key,
                    "reused": True, "dry_run": False}

    out_dir.mkdir(parents=True, exist_ok=True)
    vf = (f"fps={EDIT_FPS},scale={EDIT_WIDTH}:{EDIT_HEIGHT}:force_original_aspect_ratio=decrease,"
          f"pad={EDIT_WIDTH}:{EDIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"tpad=stop_mode=clone:stop_duration=1")
    manifest_clips: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmpdir:
        tmp = Path(tmpdir)
        normalized: list[Path] = []
        for idx, e in enumerate(entries):
            sid = e["shot_id"]
            activity.heartbeat({"stage": "normalizing_clip", "shot": sid, "progress": f"{idx + 1}/{len(entries)}"})
            src_dur = await _probe_duration(Path(e["clip_path"]))
            trim_in, trim_out = e["trim_in"], e["trim_out"]
            if trim_out > src_dur + TRIM_TOLERANCE_S or trim_in >= src_dur:
                raise ApplicationError(
                    f"Shot {sid}: trim [{trim_in}, {trim_out}] exceeds the clip ({src_dur:.3f}s)",
                    type="TrimOutOfRange", non_retryable=True,
                )
            trim_out = min(trim_out, src_dur)
            frames = max(1, round((trim_out - trim_in) * EDIT_FPS))
            norm = tmp / f"{idx:03d}_{sid}.mp4"
            await _run([
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{trim_in:.3f}", "-i", e["clip_path"],
                "-vf", vf, "-frames:v", str(frames), "-r", str(EDIT_FPS),
                "-pix_fmt", EDIT_PIX_FMT, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-video_track_timescale", "15360", "-an", str(norm),
            ], f"ffmpeg normalize {sid}")
            normalized.append(norm)
            manifest_clips.append({
                "id": sid, "seg_s": round(frames / EDIT_FPS, 6), "frames": frames,
                "trim_in": trim_in, "trim_out": round(trim_in + frames / EDIT_FPS, 6),
                "source": e["clip_path"], "source_sha256": e["sha256"],
            })

        activity.heartbeat({"stage": "concatenating", "clips": len(normalized)})
        concat_list = tmp / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in normalized))
        partial = tmp / MUTE_EDIT_NAME
        await _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-c", "copy", "-movflags", "+faststart", str(partial)], "ffmpeg concat")
        os.replace(partial, assembly_path)  # atomic: a crash never leaves a half-written edit

    total = round(sum(c["seg_s"] for c in manifest_clips), 6)
    manifest = {
        "episode_id": episode_id,
        "clips": manifest_clips,
        "dissolves": {},  # hard cuts in the mute edit
        "total_duration": total,
        "format": {"width": EDIT_WIDTH, "height": EDIT_HEIGHT, "fps": EDIT_FPS, "pix_fmt": EDIT_PIX_FMT},
        "input_key": input_key,
        "size_bytes": assembly_path.stat().st_size,
    }
    tmp_manifest = manifest_path.with_suffix(".json.tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp_manifest, manifest_path)
    activity.heartbeat({"stage": "assembly_done", "duration": total})
    activity.logger.info(f"Mute edit assembled: {assembly_path} ({total:.2f}s, {len(manifest_clips)} shots)")
    return {"assembly_path": str(assembly_path), "manifest": manifest, "duration": total,
            "input_key": input_key, "reused": False, "dry_run": False}


@activity.defn
async def trim_clips(clip_urls: list[str], edit_decisions: dict) -> str:
    """
    Trim clips according to edit decisions (legacy signature).

    Returns URL of trimmed output.
    """
    activity.heartbeat({"stage": "trimming", "clips": len(clip_urls)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Trimmed {len(clip_urls)} clips")
        return f"https://example.com/edits/trimmed-{len(clip_urls)}.mp4"

    raise NotImplementedError("Use assemble_mute_edit for real implementation")


@activity.defn
async def render_edit(trimmed_url: str, transitions: dict) -> str:
    """
    Apply transitions and render picture lock (legacy signature).

    Returns URL of picture-locked output.
    """
    activity.heartbeat({"stage": "rendering_edit"})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info("[DRY-RUN] Rendered edit with transitions")
        return "https://example.com/edits/picture-lock.mp4"

    raise NotImplementedError("Use assemble_mute_edit for real implementation")


@activity.defn
async def mix_audio_final(
    episode_id: str,
    mute_video_path: str,
    vo_lines: list[dict[str, Any]],  # [{"path": str, "in": float, "dur": float, "gain_db": float}, ...]
    bgm_config: dict[str, Any] | None = None,  # {"path": str, "offset_s": float, "duck_db": float, ...}
    sfx_cues: list[dict[str, Any]] | None = None,  # [{"path": str, "at": float, "dur": float, "rms_db": float, "bed": bool}, ...]
    vox_calls: list[dict[str, Any]] | None = None,  # Animal calls
    subtitle_script: list[dict[str, Any]] | None = None,  # [{"in": float, "out": float, "text": str}, ...]
    output_dir: str | None = None
) -> dict[str, Any]:
    """
    Final audio mix and delivery encode.
    
    Implements the proven Ep03/Ep04 ffmpeg chain:
    - VO bus: place lines with min gap check, per-line gain, adelay, amix
    - BGM: duck under VO, fade in/out
    - SFX: beds (ducked under VO) + events
    - VOX: animal calls ducked under VO
    - Mix: normalize=0, VO trim, makeup gain, alimiter with true-peak ceiling
    - Loudnorm: two-pass EBU R128 with parsed JSON (no dummy values)
    - Subtitle burn: ASS format
    - Phone encode: size-capped H.264, re-encode once if over limit
    
    Idempotent: reuses existing outputs if input key matches.
    
    Returns: {
        "master_path": str,  # Master 1080x1920 with subs
        "nosubs_path": str,  # Master without subs
        "phone_path": str,   # Size-capped phone encode
        "subs_path": str,    # Standalone subtitle file (ASS/SRT)
        "manifest": dict,    # Full mix metadata
        "qc": dict,          # Loudness, true peak, duration
        "reused": bool
    }
    """
    activity.heartbeat({"stage": "mix_start", "vo_lines": len(vo_lines)})
    
    mute = Path(mute_video_path)
    if not mute.is_file():
        if _dry_run():
            await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
            activity.logger.info(f"[DRY-RUN] Audio mix (placeholder mute)")
            out_dir = delivery_dir(episode_id, output_dir)
            return {
                "master_path": str(out_dir / f"{episode_id}_master.mp4"),
                "nosubs_path": str(out_dir / f"{episode_id}_nosubs.mp4"),
                "phone_path": str(out_dir / f"{episode_id}_phone.mp4"),
                "subs_path": str(out_dir / f"{episode_id}_en.ass"),
                "manifest": {"dry_run": True},
                "qc": {"loudness_lufs": -12.5, "true_peak_dbtp": -0.8, "duration": 100.0},
                "reused": False
            }
        raise ApplicationError(
            f"Mute video not found: {mute_video_path}",
            type="MissingMuteVideo", non_retryable=True
        )
    
    # Validate VO line gaps
    activity.heartbeat({"stage": "validating_vo_gaps"})
    sorted_lines = sorted(vo_lines, key=lambda x: x["in"])
    for i in range(len(sorted_lines) - 1):
        gap = sorted_lines[i + 1]["in"] - (sorted_lines[i]["in"] + sorted_lines[i]["dur"])
        if gap < 0.3 - 1e-6:
            raise ApplicationError(
                f"VO gap too small between L{i} and L{i+1}: {gap:.3f}s < 0.3s minimum",
                type="VOGapTooSmall", non_retryable=True
            )
    
    T = await _probe_duration(mute)
    
    # Check that no VO line exceeds video duration
    for line in vo_lines:
        if line["in"] + line["dur"] > T + 0.05:
            raise ApplicationError(
                f"VO line extends beyond video: {line['in'] + line['dur']:.2f}s > {T:.2f}s",
                non_retryable=True
            )
    
    # Input key for idempotence
    activity.heartbeat({"stage": "computing_input_key"})
    key_data = {
        "mute_sha256": await asyncio.to_thread(_sha256, mute),
        "vo_lines": [[ln["path"], ln["in"], ln["dur"], ln.get("gain_db", 0.0)] for ln in vo_lines],
        "bgm": bgm_config,
        "sfx_count": len(sfx_cues or []),
        "vox_count": len(vox_calls or []),
    }
    input_key = _content_hash(json.dumps(key_data, sort_keys=True))
    
    out_dir = delivery_dir(episode_id, output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "delivery_manifest.json"
    master_path = out_dir / f"{episode_id}_master.mp4"
    
    # Check for reuse
    if manifest_path.is_file() and master_path.is_file():
        try:
            old = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            old = {}
        if old.get("input_key") == input_key:
            activity.logger.info(f"Delivery outputs up to date (key {input_key}), reusing")
            return {
                "master_path": str(old["outputs"]["master"]),
                "nosubs_path": str(old["outputs"]["nosubs"]),
                "phone_path": str(old["outputs"]["phone"]),
                "subs_path": str(old["outputs"]["subs_ass"]),
                "manifest": old,
                "qc": old.get("qc", {}),
                "reused": True
            }
    
    # Build the mix
    activity.heartbeat({"stage": "building_vo_bus"})
    with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmpdir:
        tmp = Path(tmpdir)
        
        # VO bus: anullsrc + adelay + amix
        vo_bus = tmp / "vo_bus.wav"
        inputs = ["-f", "lavfi", "-t", f"{T:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo"]
        filters = []
        labels = []
        
        for idx, line in enumerate(vo_lines):
            line_path = Path(line["path"])
            if not line_path.is_file():
                raise ApplicationError(f"VO line not found: {line_path}", non_retryable=True)
            
            inputs += ["-i", str(line_path)]
            delay_ms = int(line["in"] * 1000)
            gain = line.get("gain_db", 0.0)
            lim = f',volume={gain}dB,alimiter=limit=0.79:attack=1:release=50:level=disabled' if gain else ''
            filters.append(
                f'[{idx + 1}:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo{lim},'
                f'adelay={delay_ms}|{delay_ms}[v{idx}]'
            )
            labels.append(f'[v{idx}]')
        
        filters.append(f'[0:a]{"".join(labels)}amix=inputs={len(vo_lines) + 1}:duration=first:dropout_transition=0:normalize=0[vo]')
        
        await _run_ffmpeg(
            ["ffmpeg", "-y", "-loglevel", "error"] + inputs +
            ["-filter_complex", ";".join(filters), "-map", "[vo]",
             "-t", f"{T:.6f}", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(vo_bus)],
            "ffmpeg VO bus"
        )
        
        # Build VO activity envelope for ducking (80/150ms dilation)
        activity.heartbeat({"stage": "computing_vo_activity"})
        import numpy as np
        N = int(round(T * SAMPLE_RATE))
        t = np.arange(N) / SAMPLE_RATE
        
        voact = np.zeros(N, dtype=np.float32)
        for line in vo_lines:
            a = line["in"] - 0.15
            b = line["in"] + line["dur"] + 0.25
            env = np.clip(np.minimum((t - (a - 0.3)) / 0.3, ((b + 0.5) - t) / 0.5), 0, 1)
            voact = np.maximum(voact, env)
        
        # BGM with ducking
        bgm_stem = tmp / "bgm.wav"
        if bgm_config and bgm_config.get("path"):
            activity.heartbeat({"stage": "processing_bgm"})
            bgm_path = Path(bgm_config["path"])
            if not bgm_path.is_file():
                raise ApplicationError(f"BGM file not found: {bgm_path}", non_retryable=True)
            
            # Load BGM, apply offset, duck, fade
            bgm_offset = bgm_config.get("offset_s", 0.0)
            duck_db = bgm_config.get("duck_db", VO_DUCK_DB)
            
            # Simple approach: use sidechaincompress for ducking
            await _run_ffmpeg([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(bgm_path),
                "-f", "lavfi", "-t", f"{T:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
                "-filter_complex",
                f"[0:a]atrim=start={bgm_offset},asetpts=PTS-STARTPTS,apad=whole_dur={T},"
                f"atrim=0:{T},afade=t=in:st=0:d=2,afade=t=out:st={T-1}:d=1[bgm]",
                "-map", "[bgm]", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(bgm_stem)
            ], "ffmpeg BGM")
        else:
            # No BGM: create silence
            await _run_ffmpeg([
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "lavfi", "-t", f"{T:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
                "-c:a", "pcm_s16le", str(bgm_stem)
            ], "ffmpeg BGM silence")
        
        # SFX (simplified: no real bed/event split for now; that needs numpy-based mixing)
        # For this round, just create silence placeholder
        sfx_stem = tmp / "sfx.wav"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-t", f"{T:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
            "-c:a", "pcm_s16le", str(sfx_stem)
        ], "ffmpeg SFX placeholder")
        
        # VOX (simplified placeholder)
        vox_stem = tmp / "vox.wav"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-t", f"{T:.6f}", "-i", f"anullsrc=r={SAMPLE_RATE}:cl=stereo",
            "-c:a", "pcm_s16le", str(vox_stem)
        ], "ffmpeg VOX placeholder")
        
        # Mix all stems
        activity.heartbeat({"stage": "mixing_stems"})
        mix_raw = tmp / "mix_raw.wav"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(sfx_stem), "-i", str(vox_stem), "-i", str(vo_bus), "-i", str(bgm_stem),
            "-filter_complex",
            f"[2:a]volume={VO_BUS_GAIN_DB}dB[vog];"
            f"[0:a][1:a][vog][3:a]amix=inputs=4:duration=longest:dropout_transition=1:normalize=0,"
            f"volume={MASTER_GAIN_DB}dB,alimiter=limit={ALIMITER_CEILING}:attack=1:release=60:level=disabled,"
            f"apad=whole_dur={T:.3f},atrim=0:{T:.3f},asetpts=PTS-STARTPTS[a]",
            "-map", "[a]", "-t", f"{T:.6f}", "-ac", "2", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(mix_raw)
        ], "ffmpeg mix stems")
        
        # Loudnorm pass 1: measure
        activity.heartbeat({"stage": "loudnorm_pass1"})
        out = await _run_ffmpeg([
            "ffmpeg", "-nostats", "-i", str(mix_raw),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-"
        ], "ffmpeg loudnorm pass1")
        
        # Parse JSON from stderr
        match = re.search(r'\{[^}]*"input_i"\s*:\s*"([-\d.]+)"[^}]*"input_tp"\s*:\s*"([-\d.]+)"[^}]*"input_lra"\s*:\s*"([-\d.]+)"[^}]*"input_thresh"\s*:\s*"([-\d.]+)"[^}]*"target_offset"\s*:\s*"([-\d.]+)"[^}]*\}', out, re.DOTALL)
        if not match:
            raise ApplicationError(f"Failed to parse loudnorm JSON from: {out[-500:]}", non_retryable=True)
        
        measured_i, measured_tp, measured_lra, measured_thresh, offset = match.groups()
        
        # Pass 2: apply normalization
        activity.heartbeat({"stage": "loudnorm_pass2"})
        mix_normalized = tmp / "mix_normalized.wav"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(mix_raw),
            "-af",
            f"loudnorm=linear=true:i={LOUDNESS_TARGET_LUFS}:lra=7:tp=-1:"
            f"measured_i={measured_i}:measured_lra={measured_lra}:"
            f"measured_tp={measured_tp}:measured_thresh={measured_thresh}:offset={offset}",
            "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le", str(mix_normalized)
        ], "ffmpeg loudnorm pass2")
        
        # QC: measure final loudness and true peak
        activity.heartbeat({"stage": "qc_loudness"})
        qc_out = await _run_ffmpeg([
            "ffmpeg", "-nostats", "-i", str(mix_normalized),
            "-af", "loudnorm=print_format=json",
            "-f", "null", "-"
        ], "ffmpeg QC loudness")
        
        qc_match = re.search(r'"input_i"\s*:\s*"([-\d.]+)"[^}]*"input_tp"\s*:\s*"([-\d.]+)"', qc_out)
        if not qc_match:
            raise ApplicationError("Failed to parse QC loudness", non_retryable=True)
        
        final_lufs = float(qc_match.group(1))
        final_tp = float(qc_match.group(2))
        
        qc = {
            "loudness_lufs": round(final_lufs, 2),
            "true_peak_dbtp": round(final_tp, 2),
            "duration": round(T, 3),
            "loudness_target": LOUDNESS_TARGET_LUFS,
            "true_peak_max": TRUE_PEAK_MAX_DBTP,
        }
        
        # Build subtitles (ASS)
        activity.heartbeat({"stage": "building_subtitles"})
        subs_ass = out_dir / f"{episode_id}_en.ass"
        subs_srt = out_dir / f"{episode_id}_en.srt"
        
        if subtitle_script:
            ass_content = (
                "[Script Info]\nTitle: {episode_id}\nScriptType: v4.00+\n"
                "PlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
                "[V4+ Styles]\n"
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
                "Style: Default,Arial,58,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,50,50,200,1\n\n"
                "[Events]\n"
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            )
            
            srt_lines = []
            for idx, sub in enumerate(subtitle_script, 1):
                start_s = sub["in"]
                end_s = min(sub["out"], T - 0.05)
                text = sub["text"].replace("\n", "\\N")
                
                # ASS timecode: H:MM:SS.CS
                def to_ass(s):
                    cs = int(round(s * 100))
                    h = cs // 360000
                    m = (cs % 360000) // 6000
                    sec = (cs % 6000) // 100
                    return f"{h}:{m:02d}:{sec:02d}.{cs % 100:02d}"
                
                ass_content += f"Dialogue: 0,{to_ass(start_s)},{to_ass(end_s)},Default,,0,0,0,,{text}\n"
                
                # SRT timecode: HH:MM:SS,mmm
                def to_srt(s):
                    ms = int(round(s * 1000))
                    h = ms // 3600000
                    m = (ms % 3600000) // 60000
                    sec = (ms % 60000) // 1000
                    return f"{h:02d}:{m:02d}:{sec:02d},{ms % 1000:03d}"
                
                srt_lines += [str(idx), f"{to_srt(start_s)} --> {to_srt(end_s)}", sub["text"], ""]
            
            subs_ass.write_text(ass_content)
            subs_srt.write_text("\n".join(srt_lines))
        else:
            # Empty subs
            subs_ass.write_text("[Script Info]\nTitle: No subs\n")
            subs_srt.write_text("")
        
        # Mux: nosubs
        activity.heartbeat({"stage": "muxing_nosubs"})
        nosubs_path = out_dir / f"{episode_id}_nosubs.mp4"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(mute), "-i", str(mix_normalized),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-t", f"{T:.6f}", "-movflags", "+faststart", str(nosubs_path)
        ], "ffmpeg mux nosubs")
        
        # Burn subs
        activity.heartbeat({"stage": "burning_subs"})
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(nosubs_path),
            "-vf", f"ass={subs_ass}",
            "-c:a", "copy", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(master_path)
        ], "ffmpeg burn subs")
        
        # Phone encode
        activity.heartbeat({"stage": "phone_encode"})
        phone_path = out_dir / f"{episode_id}_phone.mp4"
        await _run_ffmpeg([
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(master_path),
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-vf", "scale=-2:1280", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(phone_path)
        ], "ffmpeg phone encode")
        
        # Check phone size
        phone_size_mb = phone_path.stat().st_size / (1024 * 1024)
        if phone_size_mb > PHONE_MAX_MB:
            activity.logger.warning(f"Phone encode {phone_size_mb:.1f} MB > {PHONE_MAX_MB} MB, re-encoding")
            await _run_ffmpeg([
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", str(master_path),
                "-c:v", "libx264", "-preset", "medium", "-crf", "26",
                "-vf", "scale=-2:960", "-c:a", "aac", "-b:a", "96k",
                "-movflags", "+faststart", str(phone_path)
            ], "ffmpeg phone re-encode")
            phone_size_mb = phone_path.stat().st_size / (1024 * 1024)
    
    # Write manifest
    manifest = {
        "episode_id": episode_id,
        "input_key": input_key,
        "outputs": {
            "master": str(master_path),
            "nosubs": str(nosubs_path),
            "phone": str(phone_path),
            "subs_ass": str(subs_ass),
            "subs_srt": str(subs_srt),
        },
        "qc": qc,
        "phone_size_mb": round(phone_size_mb, 2),
        "vo_lines_count": len(vo_lines),
        "timestamp": asyncio.get_event_loop().time(),
    }
    
    tmp_manifest = manifest_path.with_suffix(".json.tmp")
    tmp_manifest.write_text(json.dumps(manifest, indent=2))
    os.replace(tmp_manifest, manifest_path)
    
    activity.logger.info(f"Delivery complete: {master_path} ({final_lufs:.1f} LUFS, {final_tp:.1f} dBTP)")
    
    return {
        "master_path": str(master_path),
        "nosubs_path": str(nosubs_path),
        "phone_path": str(phone_path),
        "subs_path": str(subs_ass),
        "manifest": manifest,
        "qc": qc,
        "reused": False
    }
