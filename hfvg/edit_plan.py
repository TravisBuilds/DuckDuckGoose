"""Episode edit plan: splice LOCKED reused clips and cross-dissolves into the mute picture edit.

A Studio shots.json may list shots that are NOT generated: a ``reuse`` block points at a locked
clip from an earlier run (``clip`` + ``clip_sha256``), with ``trim_head_s`` / ``screen_s`` giving
its on-screen window, and a top-level ``dissolves`` map ({"A03->F11": 1.0}) asks for a
cross-dissolve between two consecutive shots. The list order is the edit order.

``stage_episode`` splits such a spec into
  - ``shots.json``     only the shots to generate (what the workflow plans, estimates and pays for)
  - ``edit_plan.json`` the full edit order, the reuse clips and the dissolves,
and ``assemble_mute_edit`` reads ``edit_plan.json`` at assembly time (activity side, so the
workflow history is unaffected). Reused clips are checksum-verified before they are spliced; a
mismatch or a missing file refuses the edit (never a silent substitute).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

EDIT_PLAN_NAME = "edit_plan.json"
EDIT_PLAN_VERSION = 1


class EditPlanError(ValueError):
    pass


def edit_fps(edit_plan: dict[str, Any] | None) -> int:
    """GAP-33 (ep05v2 G3 r2, Studio: judder): the edit frame rate. Default EDIT_FPS (30); an episode
    edit plan may set "fps": 24 to conform the edit to the 24 fps generated clips instead of repeating
    every 4th frame to reach 30 (visible judder on slow pans). Anything else refuses the edit."""
    from hfvg.activities.media import EDIT_FPS, EDIT_FPS_ALLOWED
    from temporalio.exceptions import ApplicationError
    if not edit_plan or edit_plan.get("fps") is None:
        return EDIT_FPS
    v = edit_plan.get("fps")
    if isinstance(v, bool) or not isinstance(v, int) or v not in EDIT_FPS_ALLOWED:
        raise ApplicationError(f"Edit plan fps {v!r} not allowed (use one of {EDIT_FPS_ALLOWED})",
                               type="EditPlanMismatch", non_retryable=True)
    return v



def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_dissolves(raw: dict[str, Any] | None) -> list[tuple[str, str, float]]:
    out = []
    for key, dur in (raw or {}).items():
        if "->" not in key:
            raise EditPlanError(f"dissolve key {key!r} must be 'OUT->IN'")
        a, b = (x.strip() for x in key.split("->", 1))
        d = float(dur)
        if not (0 < d):
            raise EditPlanError(f"dissolve {key}: duration must be > 0")
        out.append((a, b, d))
    return out


def edit_plan_from_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Build the edit plan (order, reuse clips, dissolves) from a full Studio shots.json."""
    shots = spec.get("shots") or []
    order = [s["id"] for s in shots]
    if len(set(order)) != len(order):
        raise EditPlanError("duplicate shot ids in the spec")
    reuse: dict[str, dict[str, Any]] = {}
    for s in shots:
        r = s.get("reuse")
        if not r:
            continue
        clip, sha = r.get("clip"), r.get("clip_sha256") or r.get("sha256")
        if not clip or not sha:
            raise EditPlanError(f"{s['id']}: reuse needs clip and clip_sha256")
        trim_in = float(s.get("trim_head_s") or 0.0)
        screen = float(s.get("screen_s") or s.get("duration"))
        reuse[s["id"]] = {
            "clip": clip, "sha256": sha, "trim_in": trim_in, "trim_out": round(trim_in + screen, 6),
            "screen_s": screen,
            "shot_meta": {"count": s.get("count"), "duck": bool(s.get("duck_present")),
                          "characters": s.get("characters"), "qc_fail_if": s.get("qc_fail_if") or []},
            "source": r.get("source") or r.get("status"),
        }
    dissolves = parse_dissolves(spec.get("dissolves"))
    pos = {sid: i for i, sid in enumerate(order)}
    for a, b, d in dissolves:
        if a not in pos or b not in pos or pos[b] != pos[a] + 1:
            raise EditPlanError(f"dissolve {a}->{b}: shots must be consecutive in the edit order")
    return {"version": EDIT_PLAN_VERSION, "episode_id": spec.get("episode_id"), "order": order,
            "reuse": reuse, "dissolves": {f"{a}->{b}": d for a, b, d in dissolves}}


# ---------------------------------------------------------------- GAP-51 per-shot picture ops
# ep05v2 r9 (Studio): free edit fixes without new generation - cut a bad window out of a clip
# (multi-segment source selection, retimed to the UNCHANGED screen length so picture lock and VO sync
# do not move) and a punch-in crop (source-pixel 9:16 rect, upscaled with the edit's lanczos scale).
# plan["shot_ops"] = {shot_id: {"sha256": <clip the ops were designed on>, "segments": [[in, out], ...],
#                               "interp": "dup"|"blend"|"mci", "crop": [x, y, w, h], "reason": str}}
# The ops are bound to the clip sha256: if the edit would splice a different clip for that shot (a
# recompose, a new reuse clip) the edit REFUSES instead of cropping the wrong picture.
SHOT_OPS_KEYS = {"sha256", "segments", "interp", "crop", "reason"}
SHOT_OPS_INTERP = ("dup", "blend", "mci", "rife")  # GAP-61: rife = RIFE motion interpolation (hfvg.interp_rife)
SHOT_OPS_MIN_SPEED, SHOT_OPS_MAX_SPEED = 0.5, 2.0   # source seconds per screen second
SHOT_OPS_MAX_CROP_UPSCALE = 2.0                      # output width / crop width (sharpness guard)
SHOT_OPS_ASPECT, SHOT_OPS_ASPECT_TOL = 9 / 16, 0.01
SHOT_OPS_MIN_SEGMENT_S = 0.25
# GAP-63 (ep05v2 r12, Travis): slow-downs below ~0.7x read as stutter even interpolated. Still allowed down
# to the 0.5 hard floor, but every one is WARNED (assembly manifest edit_plan.speed_warnings + log), as is
# a slowed shot that is not motion-interpolated (dup/blend repeat or blend frames = visible judder).
SHOT_OPS_WARN_SPEED = 0.7
SHOT_OPS_SMOOTH_INTERP = ("mci", "rife")


def validate_shot_ops(sid: str, ops: Any, out_width: int = 1080) -> dict[str, Any]:
    """Static validation of one shot's ops (no file access). Returns a canonical copy.

    Raises EditPlanError on unknown keys, a missing/bad sha256, unordered or overlapping segments,
    a crop that is not 9:16 (within 1%), odd-sized, or would be upscaled more than 2x."""
    if not isinstance(ops, dict):
        raise EditPlanError(f"shot_ops {sid}: must be an object")
    unknown = set(ops) - SHOT_OPS_KEYS
    if unknown:
        raise EditPlanError(f"shot_ops {sid}: unknown key(s) {sorted(unknown)}")
    sha = ops.get("sha256")
    if not (isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha.lower())):
        raise EditPlanError(f"shot_ops {sid}: sha256 of the clip the ops were made for is required")
    if not (str(ops.get("reason") or "").strip()):
        raise EditPlanError(f"shot_ops {sid}: a reason is required")
    out: dict[str, Any] = {"sha256": sha.lower(), "reason": str(ops["reason"]).strip()}
    if "segments" in ops:
        segs = ops["segments"]
        if not isinstance(segs, list) or not segs:
            raise EditPlanError(f"shot_ops {sid}: segments must be a non-empty list of [in, out]")
        canon, last = [], -1.0
        for seg in segs:
            if not (isinstance(seg, (list, tuple)) and len(seg) == 2
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in seg)):
                raise EditPlanError(f"shot_ops {sid}: bad segment {seg!r}")
            a, b = float(seg[0]), float(seg[1])
            if a < 0 or b - a < SHOT_OPS_MIN_SEGMENT_S:
                raise EditPlanError(f"shot_ops {sid}: segment {seg!r} must start >= 0 and last >= "
                                    f"{SHOT_OPS_MIN_SEGMENT_S}s")
            if a < last:
                raise EditPlanError(f"shot_ops {sid}: segments must be in order and not overlap")
            canon.append([round(a, 6), round(b, 6)])
            last = b
        out["segments"] = canon
        interp = ops.get("interp", "blend")
        if interp not in SHOT_OPS_INTERP:
            raise EditPlanError(f"shot_ops {sid}: interp must be one of {SHOT_OPS_INTERP}")
        out["interp"] = interp
    elif "interp" in ops:
        raise EditPlanError(f"shot_ops {sid}: interp needs segments")
    if "crop" in ops:
        c = ops["crop"]
        if not (isinstance(c, (list, tuple)) and len(c) == 4
                and all(isinstance(v, int) and not isinstance(v, bool) for v in c)):
            raise EditPlanError(f"shot_ops {sid}: crop must be [x, y, w, h] integers (source pixels)")
        x, y, w, h = c
        if x < 0 or y < 0 or w < 2 or h < 2 or w % 2 or h % 2:
            raise EditPlanError(f"shot_ops {sid}: crop {c} must be non-negative with even w/h")
        if abs((w / h) - SHOT_OPS_ASPECT) / SHOT_OPS_ASPECT > SHOT_OPS_ASPECT_TOL:
            raise EditPlanError(f"shot_ops {sid}: crop {c} is not 9:16 (would letterbox)")
        if out_width / w > SHOT_OPS_MAX_CROP_UPSCALE + 1e-9:
            raise EditPlanError(f"shot_ops {sid}: crop {c} needs a {out_width / w:.2f}x upscale "
                                f"(max {SHOT_OPS_MAX_CROP_UPSCALE}x)")
        out["crop"] = [x, y, w, h]
    if "segments" not in out and "crop" not in out:
        raise EditPlanError(f"shot_ops {sid}: nothing to do (segments and/or crop)")
    return out


def shot_ops_speed(ops: dict[str, Any], screen_s: float) -> float:
    """Source seconds per screen second for the segments (1.0 without segments). Bounded."""
    if not ops.get("segments"):
        return 1.0
    src = sum(b - a for a, b in ops["segments"])
    speed = src / float(screen_s)
    if not (SHOT_OPS_MIN_SPEED - 1e-9 <= speed <= SHOT_OPS_MAX_SPEED + 1e-9):
        raise EditPlanError(f"segments total {src:.3f}s for a {screen_s:.3f}s slot = speed {speed:.3f} "
                            f"(allowed {SHOT_OPS_MIN_SPEED}-{SHOT_OPS_MAX_SPEED})")
    return speed


def speed_warnings(sid: str, ops: dict[str, Any] | None, speed: float,
                   shot: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """GAP-63: warnings for one shot's retime (empty = fine). Pure.
    GAP-76: with the shot's metadata, an object-contact shot below 0.85x also gets a slow_action warning."""
    out: list[dict[str, Any]] = []
    if not ops or not ops.get("segments"):
        return out
    if speed < SHOT_OPS_WARN_SPEED - 1e-9:
        out.append({"id": sid, "kind": "slow", "speed": round(speed, 4),
                    "msg": f"{sid}: {speed:.3f}x is below the {SHOT_OPS_WARN_SPEED}x speed floor (reads as slow-mo/stutter)"})
    if abs(speed - 1.0) > 1e-6 and ops.get("interp", "blend") not in SHOT_OPS_SMOOTH_INTERP:
        out.append({"id": sid, "kind": "interp", "speed": round(speed, 4), "interp": ops.get("interp", "blend"),
                    "msg": f"{sid}: retimed {speed:.3f}x with interp {ops.get('interp', 'blend')!r} "
                           f"(repeated/blended frames judder; use rife or mci)"})
    if shot is not None:
        from hfvg.motion_qc import slow_action_warnings
        out.extend(slow_action_warnings(sid, speed, shot))
    return out


def parse_shot_ops(plan: dict[str, Any] | None, out_width: int = 1080) -> dict[str, dict[str, Any]]:
    """All shot_ops of an edit plan, validated (shot ids must be in the edit order)."""
    raw = (plan or {}).get("shot_ops") or {}
    if not isinstance(raw, dict):
        raise EditPlanError("shot_ops must be an object {shot_id: ops}")
    order = set((plan or {}).get("order") or [])
    out = {}
    for sid, ops in raw.items():
        if sid not in order:
            raise EditPlanError(f"shot_ops {sid}: not in the edit order")
        out[sid] = validate_shot_ops(sid, ops, out_width)
    return out


def shot_ops_filter(ops: dict[str, Any], speed: float, fps: int, tail_vf: str, pre_retimed: bool = False) -> str:
    """ffmpeg -filter_complex for one shot: [segments -> concat -> retime/interp] -> crop -> tail_vf.
    pre_retimed (GAP-61 rife): input 0 is already the retimed segments at ``fps`` -> crop -> tail_vf only."""
    parts, chain = [], "[0:v]"
    if ops.get("segments") and pre_retimed:
        filt = []
    elif ops.get("segments"):
        segs = ops["segments"]
        for i, (a, b) in enumerate(segs):
            parts.append(f"[0:v]trim=start={a:.6f}:end={b:.6f},setpts=PTS-STARTPTS[s{i}]")
        if len(segs) > 1:
            parts.append("".join(f"[s{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=0[cat]")
            chain = "[cat]"
        else:
            chain = "[s0]"
        filt = [f"setpts={1.0 / speed:.6f}*PTS"]
        interp = ops.get("interp", "blend")
        if abs(speed - 1.0) < 1e-6 or interp == "dup":
            filt.append(f"fps={fps}")
        elif interp == "blend":
            filt.append(f"framerate=fps={fps}")
        else:
            filt.append(f"minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:vsbmc=1")
    else:
        filt = []
    if ops.get("crop"):
        x, y, w, h = ops["crop"]
        filt.append(f"crop={w}:{h}:{x}:{y}")
    filt.append(tail_vf)
    parts.append(chain + ",".join(filt) + "[out]")
    return ";".join(parts)


# ---------------------------------------------------------------- GAP-62 timeline-neutral dissolves
# ep05v2 r12 (Travis: "transitions don't work"): the legacy ``dissolves`` overlap two shots and SHORTEN
# the cut (every later shot and VO line moves earlier). plan["transitions"] = {"A->B": {"kind":
# "dissolve", "frames": n, "reason": str}} keeps every shot start (and so VO sync and the runtime): the
# outgoing shot A renders n EXTRA frames past its slot (from its own source handle, or, when A is
# retimed by shot_ops segments, by spreading its segments over slot+n frames) and they are blended
# over the first n frames of B. A without enough handle and no retime is refused (never a frozen hold).
TRANSITION_KINDS = ("dissolve",)
TRANSITION_MIN_FRAMES, TRANSITION_MAX_FRAMES = 2, 24
TRANSITION_KEYS = {"kind", "frames", "reason"}


def parse_transitions(plan: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Static validation of plan["transitions"]. Returns {(a, b): {kind, frames, reason}}."""
    raw = (plan or {}).get("transitions") or {}
    if not isinstance(raw, dict):
        raise EditPlanError("transitions must be an object {\"A->B\": {kind, frames, reason}}")
    order = list((plan or {}).get("order") or [])
    pos = {sid: i for i, sid in enumerate(order)}
    legacy = {(a, b) for a, b, _d in parse_dissolves((plan or {}).get("dissolves"))}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    outgoing: set[str] = set()
    for pair, t in raw.items():
        a, sep, b = str(pair).partition("->")
        a, b = a.strip(), b.strip()
        if not sep or not a or not b:
            raise EditPlanError(f"transition key {pair!r} must look like 'A->B'")
        if a not in pos or b not in pos or pos[b] != pos[a] + 1:
            raise EditPlanError(f"transition {a}->{b}: shots must be consecutive in the edit order")
        if (a, b) in legacy:
            raise EditPlanError(f"transition {a}->{b}: also listed in dissolves (pick one)")
        if not isinstance(t, dict):
            raise EditPlanError(f"transition {a}->{b}: must be an object")
        unknown = set(t) - TRANSITION_KEYS
        if unknown:
            raise EditPlanError(f"transition {a}->{b}: unknown key(s) {sorted(unknown)}")
        kind = t.get("kind", "dissolve")
        if kind not in TRANSITION_KINDS:
            raise EditPlanError(f"transition {a}->{b}: kind must be one of {TRANSITION_KINDS}")
        n = t.get("frames")
        if not isinstance(n, int) or isinstance(n, bool) or not TRANSITION_MIN_FRAMES <= n <= TRANSITION_MAX_FRAMES:
            raise EditPlanError(f"transition {a}->{b}: frames must be an integer "
                                f"{TRANSITION_MIN_FRAMES}-{TRANSITION_MAX_FRAMES}")
        if not str(t.get("reason") or "").strip():
            raise EditPlanError(f"transition {a}->{b}: a reason is required")
        outgoing.add(a)
        out[(a, b)] = {"kind": kind, "frames": n, "reason": str(t["reason"]).strip()}
    return out


# ---------------------------------------------------------------- GAP-58 slot time moves
# ep05v2 r11 (Studio): a clip whose clean window is too short for its slot even at the 0.5x floor is
# shortened, and the time goes to a neighbour (slowed with shot_ops segments) so the cut length and the
# absolute VO times do not move. plan["slots"] = {shot_id: {"screen_s": <new slot>, "reason": str}}.
# Slots are frame-exact at the edit fps and ZERO-SUM over all overridden shots (the picture total
# never changes). A slot longer than the clip's remaining source needs shot_ops segments (retime);
# otherwise the assembler refuses it (TrimOutOfRange). Applied in assemble_mute_edit after the reuse
# merge, so it works for generated AND reused shots; recorded per clip ("slot_move") and in the notes.
SLOTS_KEYS = {"screen_s", "reason"}


def validate_slots(plan: dict[str, Any] | None, fps: int) -> dict[str, dict[str, Any]]:
    """Static validation of plan["slots"] (no file access). Returns {sid: {screen_s, frames, reason}}.
    The zero-sum check needs the current slot lengths: see apply_slots()."""
    raw = (plan or {}).get("slots") or {}
    if not isinstance(raw, dict):
        raise EditPlanError("slots must be an object {shot_id: {screen_s, reason}}")
    order = set((plan or {}).get("order") or [])
    out = {}
    for sid, s in raw.items():
        if sid not in order:
            raise EditPlanError(f"slots {sid}: not in the edit order")
        if not isinstance(s, dict):
            raise EditPlanError(f"slots {sid}: must be an object")
        unknown = set(s) - SLOTS_KEYS
        if unknown:
            raise EditPlanError(f"slots {sid}: unknown key(s) {sorted(unknown)}")
        if not str(s.get("reason") or "").strip():
            raise EditPlanError(f"slots {sid}: a reason is required")
        v = s.get("screen_s")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v > 0:
            raise EditPlanError(f"slots {sid}: screen_s must be a number > 0")
        frames = round(float(v) * fps)
        if frames < 1 or abs(float(v) * fps - frames) > 1e-3:
            raise EditPlanError(f"slots {sid}: screen_s {v} is not a whole number of frames at {fps} fps")
        out[sid] = {"screen_s": round(frames / fps, 6), "frames": frames, "reason": str(s["reason"]).strip()}
    return out


# ---------------------------------------------------------------- GAP-83 absorbed drops
# ep05v2 r15 (Studio/Travis v13): a shot dropped from the edit (GAP-22 set_shot_edit drop) shortens the
# cut by its slot. plan["absorbed_drops"] = {shot_id: {"screen_s": <its slot>, "reason": str}} declares
# that the slot moves give that time to its neighbours, so the cut length (and its lock) stays the
# same: the slot moves must then net exactly +frames. Fail-closed: the shot must be in the order, a
# whole number of frames, and really absent from the assembled edit (checked by absorbed_drop_frames).
ABSORBED_DROPS_KEYS = {"screen_s", "reason"}


def absorbed_drop_frames(plan: dict[str, Any] | None, fps: int, present: list[str] | None = None) -> int:
    """Frames of the declared absorbed drops (GAP-83). ``present`` = the shot ids in this edit; a
    declared shot that is still present refuses (its time would be counted twice)."""
    raw = (plan or {}).get("absorbed_drops") or {}
    if not isinstance(raw, dict):
        raise EditPlanError("absorbed_drops must be an object {shot_id: {screen_s, reason}}")
    order = set((plan or {}).get("order") or [])
    total = 0
    for sid, d in raw.items():
        if sid not in order:
            raise EditPlanError(f"absorbed_drops {sid}: not in the edit order")
        if not isinstance(d, dict) or set(d) - ABSORBED_DROPS_KEYS:
            raise EditPlanError(f"absorbed_drops {sid}: must be an object with keys {sorted(ABSORBED_DROPS_KEYS)}")
        if not str(d.get("reason") or "").strip():
            raise EditPlanError(f"absorbed_drops {sid}: a reason is required")
        v = d.get("screen_s")
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not v > 0:
            raise EditPlanError(f"absorbed_drops {sid}: screen_s must be a number > 0")
        f = round(float(v) * fps)
        if abs(float(v) * fps - f) > 1e-3:
            raise EditPlanError(f"absorbed_drops {sid}: {v} s is not a whole number of frames at {fps} fps")
        if present is not None and sid in present:
            raise EditPlanError(f"absorbed_drops {sid}: shot is still in this edit (drop it first)")
        total += f
    return total


def removed_dissolve_frames(plan: dict[str, Any] | None, fps: int) -> int:
    """GAP-80: plan["removed_dissolves"] = {"A->B": seconds} declares a cross-dissolve that the
    previous cut had and this one replaces with a straight cut. Removing a dissolve lengthens the
    cut by its overlap, so the slot moves must then net exactly -overlap frames to keep the cut
    length (and its lock) unchanged. Fail-closed: the pair must be consecutive in the order, not
    also an active dissolve, and a whole number of frames."""
    raw = (plan or {}).get("removed_dissolves") or {}
    if not isinstance(raw, dict):
        raise EditPlanError("removed_dissolves must be an object {'OUT->IN': seconds}")
    order = list((plan or {}).get("order") or [])
    active = (plan or {}).get("dissolves") or {}
    total = 0
    for key, d in raw.items():
        a, sep, b = str(key).partition("->")
        if not sep or not a or not b:
            raise EditPlanError(f"removed_dissolves key {key!r} must be 'OUT->IN'")
        if a not in order or b not in order or order.index(b) != order.index(a) + 1:
            raise EditPlanError(f"removed_dissolves {key}: not two consecutive shots in the order")
        if key in active:
            raise EditPlanError(f"removed_dissolves {key}: still listed in dissolves")
        if not isinstance(d, (int, float)) or isinstance(d, bool) or not d > 0:
            raise EditPlanError(f"removed_dissolves {key}: duration must be > 0")
        f = round(float(d) * fps)
        if abs(float(d) * fps - f) > 1e-3:
            raise EditPlanError(f"removed_dissolves {key}: {d} s is not a whole number of frames at {fps} fps")
        total += f
    return total


def apply_slots(entries: list[dict[str, Any]], slots: dict[str, dict[str, Any]], fps: int,
                net_frames: int = 0) -> list[dict[str, Any]]:
    """Set each overridden entry's slot (trim_out = trim_in + screen_s) after checking the moves are
    zero-sum in frames over the shots present in the edit (or net exactly ``net_frames``, GAP-80:
    minus the overlap of declared removed dissolves). Mutates entries; returns the move records."""
    if not slots:
        if net_frames:
            raise EditPlanError(f"removed_dissolves need slot moves netting {net_frames:+d} frames")
        return []
    by_id = {e["shot_id"]: e for e in entries}
    absent = [sid for sid in slots if sid not in by_id]
    if absent:
        raise EditPlanError(f"slots {', '.join(absent)}: shot not in this edit (skipped?)")
    moves, delta = [], 0
    for sid, s in slots.items():
        e = by_id[sid]
        old = max(1, round((e["trim_out"] - e["trim_in"]) * fps))
        delta += s["frames"] - old
        moves.append({"id": sid, "from_frames": old, "to_frames": s["frames"], "from_s": round(old / fps, 6),
                      "to_s": s["screen_s"], "reason": s["reason"]})
    if delta != net_frames:
        raise EditPlanError(f"slots must move time between shots without changing the cut length"
                            f"{f' (expected net {net_frames:+d} for removed dissolves)' if net_frames else ''}: "
                            f"net {delta:+d} frames ({delta / fps:+.3f}s)")
    for m in moves:
        e = by_id[m["id"]]
        e["trim_out"] = round(e["trim_in"] + m["to_s"], 6)
        e["slot_move"] = {k: m[k] for k in ("from_s", "to_s", "reason")}
    return moves


def generation_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The spec without reused shots (== jq '.shots |= map(select(.reuse|not))')."""
    out = copy.deepcopy(spec)
    out["shots"] = [s for s in out.get("shots") or [] if not s.get("reuse")]
    return out


def verify_reuse(plan: dict[str, Any]) -> dict[str, str]:
    """Check every reused clip exists and matches its sha256. Returns {shot_id: sha256}."""
    shas = {}
    for sid, r in (plan.get("reuse") or {}).items():
        p = Path(r["clip"])
        if not p.is_file():
            raise EditPlanError(f"{sid}: reused clip missing: {p}")
        got = _sha256(p)
        if got != r["sha256"]:
            raise EditPlanError(f"{sid}: reused clip checksum mismatch ({got[:12]} != {r['sha256'][:12]})")
        shas[sid] = got
    return shas


def stage_episode(spec_path: str | Path, episode_dir: str | Path) -> dict[str, Any]:
    """Write <episode_dir>/shots.json (generation only) and edit_plan.json from a full spec."""
    spec = json.loads(Path(spec_path).read_text(encoding='utf-8'))
    plan = edit_plan_from_spec(spec)
    verify_reuse(plan)
    ep = Path(episode_dir)
    ep.mkdir(parents=True, exist_ok=True)
    (ep / "shots.json").write_text(json.dumps(generation_spec(spec), indent=1, ensure_ascii=False), encoding='utf-8')
    (ep / EDIT_PLAN_NAME).write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding='utf-8')
    return plan


def load_edit_plan(episode_id: str) -> dict[str, Any] | None:
    p = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / EDIT_PLAN_NAME
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding='utf-8'))


if __name__ == "__main__":  # python -m hfvg.edit_plan <full shots.json> <episode dir>
    import sys
    plan = stage_episode(sys.argv[1], sys.argv[2])
    print(json.dumps({"order": plan["order"], "reuse": sorted(plan["reuse"]), "dissolves": plan["dissolves"]}))
