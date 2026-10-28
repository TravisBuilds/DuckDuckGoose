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
    spec = json.loads(Path(spec_path).read_text())
    plan = edit_plan_from_spec(spec)
    verify_reuse(plan)
    ep = Path(episode_dir)
    ep.mkdir(parents=True, exist_ok=True)
    (ep / "shots.json").write_text(json.dumps(generation_spec(spec), indent=1, ensure_ascii=False))
    (ep / EDIT_PLAN_NAME).write_text(json.dumps(plan, indent=1, ensure_ascii=False))
    return plan


def load_edit_plan(episode_id: str) -> dict[str, Any] | None:
    p = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / EDIT_PLAN_NAME
    if not p.is_file():
        return None
    return json.loads(p.read_text())


if __name__ == "__main__":  # python -m hfvg.edit_plan <full shots.json> <episode dir>
    import sys
    plan = stage_episode(sys.argv[1], sys.argv[2])
    print(json.dumps({"order": plan["order"], "reuse": sorted(plan["reuse"]), "dissolves": plan["dissolves"]}))
