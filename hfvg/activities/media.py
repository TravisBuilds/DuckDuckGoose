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
EDIT_FPS_ALLOWED = (24, 25, 30)  # GAP-33: allowed edit frame rates
EDIT_FORMAT_VERSION = 2  # bump when the normalization below changes (invalidates cached edits); 2 = GAP-47 lanczos
EDIT_SCALE_FLAGS = "lanczos"  # GAP-47: upscale filter (720p Seedance -> 1080x1920); ffmpeg default was bicubic
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
        raw = json.loads(path.read_text(encoding="utf-8"))
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
    try:
        out, err = await proc.communicate()
    except asyncio.CancelledError:
        # GAP-70 (r12): a cancelled / timed-out activity attempt must not leave its child running.
        # 548 attempt 1's rife-ncnn kept rendering after the cancel (CPU contention with attempt 2,
        # whose heartbeats then lapsed) and its TemporaryDirectory cleanup failed "Directory not empty".
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.shield(proc.wait())
            except asyncio.CancelledError:
                pass
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed ({proc.returncode}): {err.decode(errors='replace')[-800:]}")
    return out.decode(errors="replace")


def _beat(details: dict[str, Any]) -> None:
    """Heartbeat when running inside an activity (no-op for the local preview / CLI)."""
    try:
        activity.heartbeat(details)
    except RuntimeError:
        pass


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


def edit_fps(edit_plan: dict[str, Any] | None) -> int:
    """GAP-33 (ep05v2 G3 r2, Studio: judder): the edit frame rate. Default EDIT_FPS (30); an episode
    edit plan may set "fps": 24 to conform the edit to the 24 fps generated clips instead of repeating
    every 4th frame to reach 30 (visible judder on slow pans). Anything else refuses the edit."""
    if not edit_plan or edit_plan.get("fps") is None:
        return EDIT_FPS
    v = edit_plan.get("fps")
    if isinstance(v, bool) or not isinstance(v, int) or v not in EDIT_FPS_ALLOWED:
        raise ApplicationError(f"Edit plan fps {v!r} not allowed (use one of {EDIT_FPS_ALLOWED})",
                               type="EditPlanMismatch", non_retryable=True)
    return v


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
    merged, skipped, superseded = [], [], []
    for sid in order:
        if sid in reuse and sid in gen:
            # GAP-44: a reuse slot adopted and re-shot through the workflow: its COMPLETED generated clip
            # replaces the locked reuse clip (recorded); without one the reuse clip stays
            merged.append(gen[sid])
            superseded.append(sid)
        elif sid in reuse:
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
    notes = {"skipped_shots": skipped, "dropped_dissolves": dropped}
    if superseded:
        notes["reuse_superseded"] = superseded
    return merged, dissolves, notes


def edit_normalize_vf(fps: int) -> str:
    """The per-shot normalize chain (fps, lanczos scale-to-fit, pad, square pixels, end hold)."""
    return (f"fps={fps},scale={EDIT_WIDTH}:{EDIT_HEIGHT}:force_original_aspect_ratio=decrease:flags={EDIT_SCALE_FLAGS},"
            f"pad={EDIT_WIDTH}:{EDIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"tpad=stop_mode=clone:stop_duration=1")


async def render_shot_preview(clip_path: str, screen_s: float, out_path: str, fps: int = 24,
                              ops: dict[str, Any] | None = None, trim_in: float = 0.0,
                              extra_frames: int = 0) -> dict[str, Any]:
    """GAP-51: render ONE shot exactly as assemble_mute_edit would normalize it (same filters, same
    ops validation) to ``out_path`` - for reviewing a crop/segment plan before it goes in the edit
    plan. Free, local, writes only ``out_path`` (and, for interp "rife", the shared RIFE cache, so a
    preview pre-warms the assembly). ``extra_frames`` = a GAP-62 transition tail (rendered past the slot
    exactly as the assembler would)."""
    from hfvg.edit_plan import shot_ops_speed, speed_warnings, validate_shot_ops
    src = Path(clip_path)
    e = {"shot_id": "preview", "clip_path": str(src)}
    src_dur = await _probe_duration(src)
    slot_frames = max(1, round(float(screen_s) * fps))
    frames = slot_frames + int(extra_frames)
    warnings: list[dict[str, Any]] = []
    if ops:
        canon = validate_shot_ops("preview", {"reason": "preview", **ops, "sha256": ops.get("sha256") or _sha256(src)}, EDIT_WIDTH)
        canon["speed"] = shot_ops_speed(canon, float(screen_s))
        warnings = speed_warnings("preview", canon, canon["speed"])
        render_ops = _extended_ops(canon, slot_frames, frames, fps)
        args = await _shot_ops_input_args("preview", e, render_ops, src_dur, trim_in, fps, edit_normalize_vf(fps),
                                          frames=frames)
    else:
        canon = None
        if extra_frames and trim_in + frames / fps > src_dur + 0.5 / fps:
            raise ApplicationError(f"preview: no source handle for {extra_frames} transition frames",
                                   type="TransitionOutOfRange", non_retryable=True)
        args = ["-ss", f"{trim_in:.3f}", "-i", str(src), "-vf", edit_normalize_vf(fps)]
    await _run(["ffmpeg", "-y", "-v", "error", *args, "-frames:v", str(frames), "-r", str(fps),
                "-pix_fmt", EDIT_PIX_FMT, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-video_track_timescale", "15360", "-an", str(out_path)], "ffmpeg preview")
    return {"path": str(out_path), "frames": frames, "ops": canon, "warnings": warnings, "rife": e.get("rife")}


def _extended_ops(ops: dict[str, Any], slot_frames: int, frames: int, fps: int) -> dict[str, Any]:
    """GAP-62: a retimed shot that renders a transition tail spreads its segments over slot+tail frames
    (speed recomputed; the tail is real retimed picture, never a held frame)."""
    if frames == slot_frames or not ops.get("segments"):
        return ops
    src = sum(b - a for a, b in ops["segments"])
    return {**ops, "speed": round(src / (frames / fps), 6), "slot_speed": ops.get("speed")}


async def _probe_dims(path: Path) -> tuple[int, int]:
    out = await _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                      "stream=width,height", "-of", "csv=p=0:s=x", str(path)], "ffprobe dims")
    w, h = out.strip().splitlines()[0].split("x")[:2]
    return int(w), int(h)


async def _shot_ops_input_args(sid: str, e: dict[str, Any], ops: dict[str, Any], src_dur: float,
                               trim_in: float, fps: int, vf: str, frames: int | None = None) -> list[str]:
    """GAP-51: ffmpeg input + filter args for a shot with picture ops (fails closed on bad ranges).
    GAP-61: interp "rife" renders the retime with RIFE first (cached, heartbeating) - needs ``frames``."""
    from hfvg.edit_plan import shot_ops_filter
    if ops.get("segments"):
        for a, b in ops["segments"]:
            if b > src_dur + TRIM_TOLERANCE_S or a >= src_dur:
                raise ApplicationError(f"Shot {sid}: segment [{a}, {b}] exceeds the clip ({src_dur:.3f}s)",
                                       type="TrimOutOfRange", non_retryable=True)
    if ops.get("crop"):
        x, y, w, h = ops["crop"]
        sw, sh = await _probe_dims(Path(e["clip_path"]))
        if x + w > sw or y + h > sh:
            raise ApplicationError(f"Shot {sid}: crop {ops['crop']} outside the {sw}x{sh} clip",
                                   type="EditPlanMismatch", non_retryable=True)
    if ops.get("segments") and ops.get("interp") == "rife":
        from hfvg.interp_rife import RifeBadOutput, RifeUnavailable, rife_retime
        if not frames:
            raise ApplicationError(f"Shot {sid}: rife needs the output frame count", type="EditPlanMismatch",
                                   non_retryable=True)
        try:
            r = await rife_retime(e["clip_path"], e.get("sha256") or ops["sha256"], ops["segments"], int(frames),
                                  fps, _run, _beat)
        except RifeUnavailable as err:
            raise ApplicationError(f"Shot {sid}: {err}", type="RifeUnavailable", non_retryable=True)
        except RifeBadOutput as err:  # GAP-81: broken render, fail closed (never assemble it)
            raise ApplicationError(f"Shot {sid}: {err}", type="RifeBadOutput", non_retryable=True)
        e["rife"] = {"key": r["key"][:16], "cached": r["cached"], "per_segment": r["per_segment"]}
        filt = shot_ops_filter(ops, float(ops.get("speed", 1.0)), fps, vf, pre_retimed=True)
        return ["-i", r["path"], "-filter_complex", filt, "-map", "[out]"]
    filt = shot_ops_filter(ops, float(ops.get("speed", 1.0)), fps, vf)
    if ops.get("segments"):
        return ["-i", e["clip_path"], "-filter_complex", filt, "-map", "[out]"]
    return ["-ss", f"{trim_in:.3f}", "-i", e["clip_path"], "-filter_complex", filt, "-map", "[out]"]


@activity.defn
async def assemble_mute_edit(
    episode_id: str,
    shots: list[dict[str, Any]],
    clip_paths: dict[str, str],
    output_dir: str | None = None
) -> dict[str, Any]:
    """Mute 9:16 picture edit from the approved clips (picture lock input).

    Per shot: trim [trim_in, trim_out) from the shot plan, normalize to 1080x1920 @ the edit fps (30, or the edit plan's "fps"; GAP-33) yuv420p
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
    # GAP-76: shot metadata (object_contact tag / motion text) for the slow-action rule; reuse shot_meta wins
    shot_meta_of: dict[str, dict] = {}
    try:
        _sj = json.loads((Path("data/episodes") / episode_id / "shots.json").read_text(encoding="utf-8"))
        for _s in (_sj.get("shots") if isinstance(_sj, dict) else None) or []:
            if isinstance(_s, dict) and _s.get("id"):
                shot_meta_of[_s["id"]] = _s
    except (OSError, ValueError, AttributeError):
        pass
    for _s in shots:
        if isinstance(_s, dict) and "shot_id" in _s:
            shot_meta_of[_s["shot_id"]] = {**shot_meta_of.get(_s["shot_id"], {}), **_s}
    for _sid, _r in ((edit_plan or {}).get("reuse") or {}).items():
        if isinstance(_r, dict) and isinstance(_r.get("shot_meta"), dict):
            shot_meta_of[_sid] = {**shot_meta_of.get(_sid, {}), **_r["shot_meta"]}
    dissolves: list[tuple[str, str, float]] = []
    plan_notes: dict = {}
    if edit_plan:
        entries, dissolves, plan_notes = _apply_edit_plan(entries, edit_plan)
    fps = edit_fps(edit_plan)  # GAP-33: 24 = conform to the 24 fps sources (no 24->30 frame repeats)
    # GAP-58: slot time moved between shots (zero-sum, frame-exact) before the per-shot ops see the slot
    if edit_plan and (edit_plan.get("slots") or edit_plan.get("removed_dissolves") or edit_plan.get("absorbed_drops")):
        from hfvg.edit_plan import EditPlanError as _EPE, absorbed_drop_frames, apply_slots, removed_dissolve_frames, validate_slots
        try:
            _net = -removed_dissolve_frames(edit_plan, fps)  # GAP-80
            _absorb = absorbed_drop_frames(edit_plan, fps, [e["shot_id"] for e in entries])  # GAP-83
            _net += _absorb
            if _absorb:
                plan_notes["absorbed_drops"] = {k: dict(v) for k, v in edit_plan["absorbed_drops"].items()}
            slot_moves = apply_slots(entries, validate_slots(edit_plan, fps), fps, net_frames=_net)
            if edit_plan.get("removed_dissolves"):
                plan_notes["removed_dissolves"] = dict(edit_plan["removed_dissolves"])
        except _EPE as e:
            raise ApplicationError(f"Edit plan slots: {e}", type="EditPlanMismatch", non_retryable=True)
        plan_notes["slot_moves"] = slot_moves
    # GAP-51: per-shot picture ops (multi-segment cut + retime, punch-in crop), bound to a clip sha256
    from hfvg.edit_plan import EditPlanError, parse_shot_ops, shot_ops_speed
    try:
        shot_ops = parse_shot_ops(edit_plan, EDIT_WIDTH) if edit_plan else {}
    except EditPlanError as e:
        raise ApplicationError(f"Edit plan shot_ops: {e}", type="EditPlanMismatch", non_retryable=True)
    # GAP-62: timeline-neutral transitions (outgoing shot renders a tail blended over the incoming head)
    from hfvg.edit_plan import parse_transitions, speed_warnings
    try:
        transitions = parse_transitions(edit_plan) if edit_plan else {}
    except EditPlanError as e:
        raise ApplicationError(f"Edit plan transitions: {e}", type="EditPlanMismatch", non_retryable=True)
    present = [e["shot_id"] for e in entries]
    adjacent = set(zip(present, present[1:]))
    dropped_tr = [f"{a}->{b}" for (a, b) in transitions if (a, b) not in adjacent]
    transitions = {k: v for k, v in transitions.items() if k in adjacent}
    if dropped_tr:
        plan_notes["dropped_transitions"] = dropped_tr
    tail_of = {a: t["frames"] for (a, _b), t in transitions.items()}
    out_dir = edit_dir(episode_id, output_dir)
    assembly_path = out_dir / MUTE_EDIT_NAME
    manifest_path = out_dir / MUTE_MANIFEST_NAME

    recipe = load_edit_recipe(out_dir / EDIT_RECIPE_NAME)
    if recipe and recipe["xfade_s"] > 0:
        shortest = min((e["trim_out"] - e["trim_in"]) * recipe["speed"] for e in entries)
        if recipe["xfade_s"] * 2 >= shortest:
            raise ApplicationError(f"edit recipe xfade_s {recipe['xfade_s']} too long for a {shortest:.2f}s shot",
                                   type="InvalidEditRecipe", non_retryable=True)
    # GAP-33: edit plan fps (default 30, may be 24 to match generated clips without judder)
    from hfvg.edit_plan import edit_fps as get_edit_fps
    fps = recipe["fps"] if recipe else get_edit_fps(edit_plan)


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
            # Task 5: dry-run return must honor the recipe
            duration = sum(c["seg_s"] for c in clips) - sum(d for *_x, d in dissolves)
            if recipe:
                # Apply recipe speed and tail_hold
                duration = duration / recipe["speed"] + recipe.get("tail_hold_s", 0.0)
            return {
                "assembly_path": str(assembly_path),
                "manifest": {
                    "clips": clips,
                    "dissolves": {a: d for a, _b, d in dissolves},
                    "recipe": recipe,  # Task 5: include recipe
                    "tail_hold_s": recipe.get("tail_hold_s", 0.0) if recipe else 0.0,  # Task 5
                    "dry_run": True
                },
                "duration": round(duration, 3),
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
        row = [e["shot_id"], e["sha256"], e["trim_in"], e["trim_out"]]
        ops = shot_ops.get(e["shot_id"])
        if ops:
            if ops["sha256"] != e["sha256"]:
                raise ApplicationError(
                    f"Shot {e['shot_id']}: shot_ops were made for clip {ops['sha256'][:12]} but the edit "
                    f"uses {e['sha256'][:12]} ({e['clip_path']}); update or remove the ops",
                    type="EditPlanMismatch", non_retryable=True)
            try:
                speed = shot_ops_speed(ops, e["trim_out"] - e["trim_in"])
            except EditPlanError as err:
                raise ApplicationError(f"Shot {e['shot_id']}: {err}", type="EditPlanMismatch", non_retryable=True)
            e["shot_ops"] = {**ops, "speed": round(speed, 6)}
            row.append({k: v for k, v in e["shot_ops"].items() if k != "reason"})  # only when present
            _meta = shot_meta_of.get(e["shot_id"])
            warns = speed_warnings(e["shot_id"], e["shot_ops"], speed, _meta)
            if (edit_plan or {}).get("slow_action") == "refuse":   # GAP-76 strict mode
                bad = [w for w in warns if w["kind"] == "slow_action"]
                if bad:
                    raise ApplicationError(bad[0]["msg"], type="EditPlanMismatch", non_retryable=True)
            if warns:
                plan_notes.setdefault("speed_warnings", []).extend(warns)
                for w in warns:
                    activity.logger.warning(f"GAP-63 {w['msg']}")
        if e["shot_id"] in tail_of:
            row.append({"tail_frames": tail_of[e["shot_id"]]})
        key_src.append(row)
    key_obj = {"v": EDIT_FORMAT_VERSION, "fmt": [EDIT_WIDTH, EDIT_HEIGHT, fps, EDIT_PIX_FMT],
               "inputs": key_src}
    if dissolves:
        key_obj["dissolves"] = [[a, b, d] for a, b, d in dissolves]
    if recipe:
        key_obj["recipe"] = recipe
    if transitions:
        key_obj["transitions"] = [[a, b, t["kind"], t["frames"]] for (a, b), t in transitions.items()]
    input_key = hashlib.sha256(json.dumps(key_obj, sort_keys=True).encode()).hexdigest()

    if assembly_path.is_file() and manifest_path.is_file():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        if old.get("input_key") == input_key and old.get("size_bytes") == assembly_path.stat().st_size:
            activity.logger.info(f"Mute edit up to date at {assembly_path} (key {input_key[:12]}), reusing")
            return {"assembly_path": str(assembly_path), "manifest": old,
                    "duration": old.get("total_duration"), "input_key": input_key,
                    "reused": True, "dry_run": False}

    out_dir.mkdir(parents=True, exist_ok=True)
    # same chain as edit_normalize_vf (the GAP-51 preview uses that helper; kept inline here so the
    # r8 lanczos source check still sees the scale flags in the assembler itself)
    vf = (f"fps={fps},scale={EDIT_WIDTH}:{EDIT_HEIGHT}:force_original_aspect_ratio=decrease:flags={EDIT_SCALE_FLAGS},"
          f"pad={EDIT_WIDTH}:{EDIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
          f"tpad=stop_mode=clone:stop_duration=1")
    if recipe:
        vf = (f"setpts={recipe['speed']:g}*PTS,fps={fps},"
              f"scale={EDIT_WIDTH}:{EDIT_HEIGHT}:flags={recipe['scale_flags']}:force_original_aspect_ratio=decrease,"
              f"pad={EDIT_WIDTH}:{EDIT_HEIGHT}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
              + (f"{recipe['post_vf']}," if recipe["post_vf"] else "")
              + "tpad=stop_mode=clone:stop_duration=1")
    else:
        assert vf == edit_normalize_vf(fps)
    manifest_clips: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=str(out_dir)) as tmpdir:
        tmp = Path(tmpdir)
        normalized: list[Path] = []
        for idx, e in enumerate(entries):
            sid = e["shot_id"]
            activity.heartbeat({"stage": "normalizing_clip", "shot": sid, "progress": f"{idx + 1}/{len(entries)}"})
            src_dur = await _probe_duration(Path(e["clip_path"]))
            trim_in, trim_out = e["trim_in"], e["trim_out"]
            ops = e.get("shot_ops")
            # GAP-58: with shot_ops segments the slot [trim_in, trim_out) is screen time only (the source
            # comes from the segments, range-checked in _shot_ops_input_args), so a slot may be longer
            # than the clip (retimed). Without segments the slot is a source range and must fit.
            retimed = bool(ops and ops.get("segments"))
            if (trim_out > src_dur + TRIM_TOLERANCE_S and not retimed) or trim_in >= src_dur:
                raise ApplicationError(
                    f"Shot {sid}: trim [{trim_in}, {trim_out}] exceeds the clip ({src_dur:.3f}s)"
                    + ("" if retimed else "; a longer slot needs shot_ops segments (retime)"),
                    type="TrimOutOfRange", non_retryable=True,
                )
            if not retimed:
                trim_out = min(trim_out, src_dur)
            frames = max(1, round((trim_out - trim_in) * (recipe["speed"] if recipe else 1.0) * fps))
            tail = tail_of.get(sid, 0)
            if tail and not retimed and trim_in + (frames + tail) / fps > src_dur + 0.5 / fps:
                raise ApplicationError(
                    f"Transition {sid}->: {tail} tail frames need source past {trim_in + (frames + tail) / fps:.3f}s "
                    f"but the clip ends at {src_dur:.3f}s; retime {sid} with shot_ops segments or shorten the "
                    f"transition (never a frozen hold)", type="TransitionOutOfRange", non_retryable=True)
            norm = tmp / f"{idx:03d}_{sid}.mp4"
            if ops:
                render_ops = _extended_ops(ops, frames, frames + tail, fps)
                src_args = await _shot_ops_input_args(sid, e, render_ops, src_dur, trim_in, fps, vf,
                                                      frames=frames + tail)
            else:
                src_args = ["-ss", f"{trim_in:.3f}", "-i", e["clip_path"], "-vf", vf]
            await _run([
                "ffmpeg", "-y", "-v", "error",
                *src_args, "-frames:v", str(frames + tail), "-r", str(fps),
                "-pix_fmt", EDIT_PIX_FMT, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-video_track_timescale", "15360", "-an", str(norm),
            ], f"ffmpeg normalize {sid}")
            normalized.append(norm)
            clip_row = {
                "id": sid, "seg_s": round(frames / fps, 6), "frames": frames,
                "trim_in": trim_in,
                "trim_out": round(trim_in + frames / fps / (recipe["speed"] if recipe else 1.0), 6),
                "source": e["clip_path"], "source_sha256": e["sha256"],
            }
            if e.get("reused"):
                clip_row["reused"] = True
                clip_row["shot_meta"] = e.get("shot_meta") or {}
            if ops:
                clip_row["shot_ops"] = ops  # GAP-51: segments/speed/interp/crop actually applied
                if e.get("rife"):
                    clip_row["rife"] = e["rife"]  # GAP-61
            if tail:
                clip_row["tail_frames"] = tail  # GAP-62: rendered past the slot, blended under the next shot
            if e.get("slot_move"):
                clip_row["slot_move"] = e["slot_move"]  # GAP-58
            manifest_clips.append(clip_row)

        # Cross-dissolves (edit plan): merge OUT and IN into one xfade segment, chain-safe.
        # (skipped when recipe drives crossfades)
        if not recipe:
            pairs = {(a, b): d for a, b, d in dissolves}
            segments: list[list] = []  # [path, frames, last_shot_id, tail_frames]
            for idx, (norm, c) in enumerate(zip(normalized, manifest_clips)):
                c_total = c["frames"] + c.get("tail_frames", 0)
                if segments and (segments[-1][2], c["id"]) in transitions:
                    # GAP-62: blend the outgoing tail over the incoming head; shot starts do not move
                    prev_path, prev_total, prev_id, prev_tail = segments[-1]
                    n = transitions[(prev_id, c["id"])]["frames"]
                    if prev_tail != n or not (0 < n < c["frames"]):
                        raise ApplicationError(f"Transition {prev_id}->{c['id']}: {n} frames vs tail {prev_tail} / "
                                               f"{c['frames']}-frame shot", type="TransitionOutOfRange", non_retryable=True)
                    visible = prev_total - prev_tail
                    activity.heartbeat({"stage": "transition", "pair": f"{prev_id}->{c['id']}"})
                    merged = tmp / f"t{idx:03d}_{prev_id}_{c['id']}.mp4"
                    out_frames = visible + c_total
                    await _run([
                        "ffmpeg", "-y", "-v", "error", "-i", str(prev_path), "-i", str(norm),
                        "-filter_complex",
                        # the outgoing file holds exactly visible + n frames (rendered with -frames:v)
                        f"[0:v][1:v]xfade=transition=fade:duration={n / fps:.6f}:offset={visible / fps:.6f},"
                        f"format={EDIT_PIX_FMT}",
                        "-frames:v", str(out_frames), "-r", str(fps), "-pix_fmt", EDIT_PIX_FMT,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-video_track_timescale", "15360", "-an", str(merged),
                    ], f"ffmpeg transition {prev_id}->{c['id']}")
                    segments[-1] = [merged, out_frames, c["id"], c.get("tail_frames", 0)]
                elif segments and (segments[-1][2], c["id"]) in pairs:
                    d = pairs[(segments[-1][2], c["id"])]
                    prev_path, prev_frames, prev_id, _prev_tail = segments[-1]
                    d_frames = round(d * fps)
                    if not (0 < d_frames < min(prev_frames, c["frames"])) or _prev_tail:
                        raise ApplicationError(f"Dissolve {prev_id}->{c['id']} ({d}s) longer than a shot",
                                               type="DissolveOutOfRange", non_retryable=True)
                    activity.heartbeat({"stage": "dissolve", "pair": f"{prev_id}->{c['id']}"})
                    out_frames = prev_frames + c_total - d_frames
                    merged = tmp / f"x{idx:03d}_{prev_id}_{c['id']}.mp4"
                    offset = (prev_frames - d_frames) / fps
                    await _run([
                        "ffmpeg", "-y", "-v", "error", "-i", str(prev_path), "-i", str(norm),
                        "-filter_complex",
                        f"[0:v][1:v]xfade=transition=fade:duration={d_frames / fps:.6f}:offset={offset:.6f},"
                        f"format={EDIT_PIX_FMT}",
                        "-frames:v", str(out_frames), "-r", str(fps), "-pix_fmt", EDIT_PIX_FMT,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-video_track_timescale", "15360", "-an", str(merged),
                    ], f"ffmpeg dissolve {prev_id}->{c['id']}")
                    segments[-1] = [merged, out_frames, c["id"], c.get("tail_frames", 0)]
                else:
                    segments.append([norm, c_total, c["id"], c.get("tail_frames", 0)])
            if any(seg[3] for seg in segments):
                raise ApplicationError("Transition tail left unblended (outgoing shot without its next shot)",
                                       type="TransitionOutOfRange", non_retryable=True)
            normalized = [seg[0] for seg in segments]

        activity.heartbeat({"stage": "concatenating", "clips": len(normalized)})
        concat_list = tmp / "concat.txt"
        concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in normalized))
        partial = tmp / MUTE_EDIT_NAME
        if recipe and (recipe["xfade_s"] > 0 or recipe["tail_hold_s"] > 0):
            segs = [c["seg_s"] for c in manifest_clips]
            x = recipe["xfade_s"]
            chain, last = [], "[0:v]"
            if x > 0:
                for i, off in enumerate(xfade_offsets(segs, x), 1):
                    chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={x:g}:offset={off:.6f}[x{i}]")
                    last = f"[x{i}]"
                inputs = normalized
            else:
                inputs = [partial.with_name("hardcut.mp4")]
                await _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                            "-c", "copy", str(inputs[0])], "ffmpeg concat")
            chain.append(f"{last}tpad=stop_mode=clone:stop_duration={recipe['tail_hold_s']:g},"
                         f"fps={fps},format={EDIT_PIX_FMT}[out]")
            cmd = ["ffmpeg", "-y", "-v", "error"]
            for p in inputs:
                cmd += ["-i", str(p)]
            total_frames = round(recipe_total_s(segs, recipe) * fps)
            await _run(cmd + ["-filter_complex", ";".join(chain), "-map", "[out]", "-frames:v", str(total_frames),
                              "-r", str(fps), "-c:v", "libx264", "-profile:v", "high", "-preset", "medium",
                              "-b:v", f"{recipe['video_kbps']}k", "-maxrate", f"{recipe['max_video_kbps']}k",
                              "-bufsize", f"{recipe['max_video_kbps'] * 2}k", "-pix_fmt", EDIT_PIX_FMT,
                              "-an", "-movflags", "+faststart", str(partial)], "ffmpeg crossfade stitch")
        else:
            await _run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list),
                        "-c", "copy", "-movflags", "+faststart", str(partial)], "ffmpeg concat")
        os.replace(partial, assembly_path)  # atomic: a crash never leaves a half-written edit

    total = round(sum(c["seg_s"] for c in manifest_clips)
                  - sum(round(d * fps) / fps for *_x, d in dissolves), 6)
    manifest_dissolves: dict[str, float] = {}
    if recipe:
        total = recipe_total_s([c["seg_s"] for c in manifest_clips], recipe)
        if recipe["xfade_s"] > 0:
            manifest_dissolves = {c["id"]: recipe["xfade_s"] for c in manifest_clips[:-1]}
        t = 0.0
        for c in manifest_clips:
            c["start_s"] = round(t, 6)
            c["end_s"] = round(t + c["seg_s"], 6)
            t = c["end_s"] - manifest_dissolves.get(c["id"], 0.0)
    else:
        manifest_dissolves = {a: round(round(d * fps) / fps, 6) for a, _b, d in dissolves}
    
    manifest = {
        "episode_id": episode_id,
        "clips": manifest_clips,
        # {outgoing shot id: seconds} (hfvg.qc.motion timeline format); hard cuts when empty
        "dissolves": manifest_dissolves,
        "dissolve_pairs": {f"{a}->{b}": d for a, b, d in dissolves},
        # GAP-62: timeline-neutral transitions (NOT subtracted from the timeline: shot starts unchanged)
        "transitions": {f"{a}->{b}": dict(t) for (a, b), t in transitions.items()},
        "edit_plan": ({"reused_shots": [c["id"] for c in manifest_clips if c.get("reused")], **plan_notes}
                      if edit_plan else None),
        "total_duration": total,
        "format": {"width": EDIT_WIDTH, "height": EDIT_HEIGHT, "fps": fps, "pix_fmt": EDIT_PIX_FMT},  # Task 5: use fps from recipe
        "recipe": recipe,  # Task 5: include recipe in manifest
        "tail_hold_s": recipe.get("tail_hold_s", 0.0) if recipe else 0.0,  # Task 5: include tail_hold_s
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
