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
async def mix_audio(video_url: str, audio_tracks: list[str]) -> str:
    """
    Mix audio tracks (VO, SFX, music) with video.

    Returns URL of final mixed output.
    """
    activity.heartbeat({"stage": "mixing_audio", "tracks": len(audio_tracks)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Mixed {len(audio_tracks)} audio tracks")
        return "https://example.com/final/mixed.mp4"

    raise NotImplementedError("Real ffmpeg not implemented in milestone 1")
