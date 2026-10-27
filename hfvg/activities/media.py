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
# Optional per-episode edit recipe ({edit_dir}/edit_recipe.json), e.g. the Ep1-11 "mute stitch":
# slow every clip (setpts), output fps, lanczos upscale + post filters (unsharp / grain), soft
# crossfades between shots and a held last frame. Absent -> the plain hard-cut edit below.
EDIT_RECIPE_NAME = "edit_recipe.json"


def load_edit_recipe(path: Path) -> dict[str, Any] | None:
    """Validate an edit recipe file; None when absent. Bad values are a non-retryable error."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ApplicationError(f"edit recipe {path} unreadable: {e}", type="InvalidEditRecipe",
                               non_retryable=True) from None

    def num(key: str, default: float, lo: float, hi: float) -> float:
        v = raw.get(key, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = float("nan")
        if not (lo <= v <= hi):
            raise ApplicationError(f"edit recipe {key}={raw.get(key)!r} outside [{lo}, {hi}]",
                                   type="InvalidEditRecipe", non_retryable=True)
        return v

    fps = int(num("fps", EDIT_FPS, 12, 60))
    post_vf = str(raw.get("post_vf") or "")
    if any(c in post_vf for c in ";[]"):
        raise ApplicationError("edit recipe post_vf must be a simple filter chain (no ; or [labels])",
                               type="InvalidEditRecipe", non_retryable=True)
    flags = str(raw.get("scale_flags") or "bicubic")
    if not flags.replace("+", "").isalpha():
        raise ApplicationError(f"edit recipe scale_flags {flags!r} invalid", type="InvalidEditRecipe",
                               non_retryable=True)
    return {
        "fps": fps,
        "speed": num("speed", 1.0, 0.5, 2.0),
        "xfade_s": num("xfade_s", 0.0, 0.0, 2.0),
        "tail_hold_s": num("tail_hold_s", 0.0, 0.0, 10.0),
        "scale_flags": flags,
        "post_vf": post_vf,
        "video_kbps": int(num("video_kbps", 14000, 1000, 50000)),
        "max_video_kbps": int(num("max_video_kbps", 16000, 1000, 60000)),
    }


def xfade_offsets(segs: list[float], xfade_s: float) -> list[float]:
    """Offset of each crossfade (into the running joined stream) for clips of length segs."""
    offsets, length = [], segs[0] if segs else 0.0
    for seg in segs[1:]:
        offsets.append(round(length - xfade_s, 6))
        length = length + seg - xfade_s
    return offsets


def recipe_total_s(segs: list[float], recipe: dict[str, Any]) -> float:
    """Final duration of the edit: sum(segs) minus xfade overlaps plus tail hold."""
    if not segs:
        return 0.0
    return round(sum(segs) - recipe["xfade_s"] * (len(segs) - 1) + recipe["tail_hold_s"], 6)


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


def _apply_edit_plan(entries: list[dict[str, Any]], plan: dict[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str, float]], dict]:
    """Merge the workflow's generated entries with an episode edit plan (hfvg.edit_plan).

    Edit order = plan["order"]; reused LOCKED clips are inserted with their trims and expected
    sha256 (verified after hashing); a generated shot that was not produced (rejected/failed) is
    skipped and any dissolve touching it falls back to a hard cut (recorded). A generated entry
    missing from the plan order refuses the edit (the plan and the shot list disagree)."""
    from hfvg.edit_plan import parse_dissolves
    gen = {e["shot_id"]: e for e in entries}
    order = list(plan.get("order") or [])
    reuse = plan.get("reuse") or {}
    stray = [sid for sid in gen if sid not in order]
    if stray:
        raise ApplicationError(f"Edit plan order is missing generated shot(s) {', '.join(stray)}",
                               type="EditPlanMismatch", non_retryable=True)
    merged, skipped = [], []
    for sid in order:
        if sid in reuse:
            r = reuse[sid]
            trim_in, trim_out = float(r.get("trim_in", 0.0)), float(r["trim_out"])
            if not (0 <= trim_in < trim_out):
                raise ApplicationError(f"Reused shot {sid}: invalid trim", non_retryable=True)
            merged.append({"shot_id": sid, "clip_path": str(r["clip"]), "trim_in": trim_in, "trim_out": trim_out,
                           "reused": True, "expected_sha256": r["sha256"], "shot_meta": r.get("shot_meta") or {}})
        elif sid in gen:
            merged.append(gen[sid])
        else:
            skipped.append(sid)
    present = [e["shot_id"] for e in merged]
    dissolves, dropped = [], []
    for a, b, d in parse_dissolves(plan.get("dissolves")):
        if a in present and b in present and present.index(b) == present.index(a) + 1:
            dissolves.append((a, b, d))
        else:
            dropped.append(f"{a}->{b}")
    return merged, dissolves, {"skipped_shots": skipped, "dropped_dissolves": dropped}


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
    from hfvg.edit_plan import load_edit_plan
    edit_plan = load_edit_plan(episode_id)
    dissolves: list[tuple[str, str, float]] = []
    plan_notes: dict = {}
    if edit_plan:
        entries, dissolves, plan_notes = _apply_edit_plan(entries, edit_plan)
    out_dir = edit_dir(episode_id, output_dir)
    assembly_path = out_dir / MUTE_EDIT_NAME
    manifest_path = out_dir / MUTE_MANIFEST_NAME

    recipe = load_edit_recipe(out_dir / EDIT_RECIPE_NAME)
    if recipe and recipe["xfade_s"] > 0:
        shortest = min((e["trim_out"] - e["trim_in"]) * recipe["speed"] for e in entries)
        if recipe["xfade_s"] * 2 >= shortest:
            raise ApplicationError(f"edit recipe xfade_s {recipe['xfade_s']} too long for a {shortest:.2f}s shot",
                                   type="InvalidEditRecipe", non_retryable=True)
    fps = recipe["fps"] if recipe else EDIT_FPS


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
                "manifest": {"clips": clips, "dissolves": {a: d for a, _b, d in dissolves}, "dry_run": True},
                "duration": round(sum(c["seg_s"] for c in clips) - sum(d for *_x, d in dissolves), 3),
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
        if e.get("expected_sha256") and e["sha256"] != e["expected_sha256"]:
            raise ApplicationError(
                f"Reused shot {e['shot_id']}: locked clip checksum mismatch "
                f"({e['sha256'][:12]} != {e['expected_sha256'][:12]}); refusing to splice",
                type="ReuseChecksumMismatch", non_retryable=True)
        key_src.append([e["shot_id"], e["sha256"], e["trim_in"], e["trim_out"]])
    key_obj = {"v": EDIT_FORMAT_VERSION, "fmt": [EDIT_WIDTH, EDIT_HEIGHT, EDIT_FPS, EDIT_PIX_FMT],
               "inputs": key_src}
    if dissolves:
        key_obj["dissolves"] = [[a, b, d] for a, b, d in dissolves]
    input_key = hashlib.sha256(json.dumps(key_obj, sort_keys=True).encode()).hexdigest()

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
            frames = max(1, round((trim_out - trim_in) * (recipe["speed"] if recipe else 1.0) * fps))
            norm = tmp / f"{idx:03d}_{sid}.mp4"
            await _run([
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{trim_in:.3f}", "-i", e["clip_path"],
                "-vf", vf, "-frames:v", str(frames), "-r", str(EDIT_FPS),
                "-pix_fmt", EDIT_PIX_FMT, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-video_track_timescale", "15360", "-an", str(norm),
            ], f"ffmpeg normalize {sid}")
            normalized.append(norm)
            clip_row = {
                "id": sid, "seg_s": round(frames / fps, 6), "frames": frames,
                "trim_in": trim_in, "trim_out": round(trim_in + frames / EDIT_FPS, 6),
                "source": e["clip_path"], "source_sha256": e["sha256"],
            }
            if e.get("reused"):
                clip_row["reused"] = True
                clip_row["shot_meta"] = e.get("shot_meta") or {}
            manifest_clips.append(clip_row)

        # Cross-dissolves (edit plan): merge OUT and IN into one xfade segment, chain-safe.
        pairs = {(a, b): d for a, b, d in dissolves}
        segments: list[list] = []  # [path, frames, last_shot_id]
        for idx, (norm, c) in enumerate(zip(normalized, manifest_clips)):
            if segments and (segments[-1][2], c["id"]) in pairs:
                d = pairs[(segments[-1][2], c["id"])]
                prev_path, prev_frames, prev_id = segments[-1]
                d_frames = round(d * EDIT_FPS)
                if not (0 < d_frames < min(prev_frames, c["frames"])):
                    raise ApplicationError(f"Dissolve {prev_id}->{c['id']} ({d}s) longer than a shot",
                                           type="DissolveOutOfRange", non_retryable=True)
                activity.heartbeat({"stage": "dissolve", "pair": f"{prev_id}->{c['id']}"})
                out_frames = prev_frames + c["frames"] - d_frames
                merged = tmp / f"x{idx:03d}_{prev_id}_{c['id']}.mp4"
                offset = (prev_frames - d_frames) / EDIT_FPS
                await _run([
                    "ffmpeg", "-y", "-v", "error", "-i", str(prev_path), "-i", str(norm),
                    "-filter_complex",
                    f"[0:v][1:v]xfade=transition=fade:duration={d_frames / EDIT_FPS:.6f}:offset={offset:.6f},"
                    f"format={EDIT_PIX_FMT}",
                    "-frames:v", str(out_frames), "-r", str(EDIT_FPS), "-pix_fmt", EDIT_PIX_FMT,
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                    "-video_track_timescale", "15360", "-an", str(merged),
                ], f"ffmpeg dissolve {prev_id}->{c['id']}")
                segments[-1] = [merged, out_frames, c["id"]]
            else:
                segments.append([norm, c["frames"], c["id"]])
        normalized = [seg[0] for seg in segments]

        activity.heartbeat({"stage": "concatenating", "clips": len(normalized)})
        concat_list = tmp / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in normalized))
        partial = tmp / MUTE_EDIT_NAME
        await _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-c", "copy", "-movflags", "+faststart", str(partial)], "ffmpeg concat")
        os.replace(partial, assembly_path)  # atomic: a crash never leaves a half-written edit

    total = round(sum(c["seg_s"] for c in manifest_clips)
                  - sum(round(d * EDIT_FPS) / EDIT_FPS for *_x, d in dissolves), 6)
    manifest = {
        "episode_id": episode_id,
        "clips": manifest_clips,
        # {outgoing shot id: seconds} (hfvg.qc.motion timeline format); hard cuts when empty
        "dissolves": {a: round(round(d * EDIT_FPS) / EDIT_FPS, 6) for a, _b, d in dissolves},
        "dissolve_pairs": {f"{a}->{b}": d for a, b, d in dissolves},
        "edit_plan": ({"reused_shots": [c["id"] for c in manifest_clips if c.get("reused")], **plan_notes}
                      if edit_plan else None),
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
