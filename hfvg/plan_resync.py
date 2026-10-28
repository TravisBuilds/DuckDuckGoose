"""GAP-28: re-sync the corrected Studio package into a running episode at G2 (no spend).

The episode parses BEATMAP.md + shots.json ONCE at start; every later still re-roll, clip, clip QC
and clip retry used that parse. A package corrected at G2 (ep05v2 2026-10-27: the duck vest wording
and qc_fail_if) therefore never reached the prompts the harness actually sends. This module diffs the
re-parsed package against the plans in force: text fields (prompt, end_prompt, clip_prompt,
qc_fail_if) may change; structural fields (shot set, shot_meta, ref_keys, trim, model/duration/
end frame/aspect/resolution) may not, because the G1 plan, estimates and refs were approved on them.
"""
from __future__ import annotations

TEXT_FIELDS = ("prompt", "end_prompt", "clip_prompt", "qc_fail_if")
CLIP_FIELDS = ("clip_prompt", "qc_fail_if")  # what a parked still child's clip phase picks up
FIXED_FIELDS = ("shot_meta", "ref_keys", "trim")
FIXED_PARAMS = ("model", "duration", "end_frame", "aspect_ratio", "resolution")


def plan_resync(current_shots: list[dict], new_shots: list[dict]) -> dict:
    """{"changes": {shot_id: {field: new value}}, "problems": [...]} (problems = refuse the resync)."""
    cur = {s["shot_id"]: s for s in current_shots or []}
    new = {s["shot_id"]: s for s in new_shots or []}
    problems: list[str] = []
    added, removed = sorted(set(new) - set(cur)), sorted(set(cur) - set(new))
    if added or removed:
        problems.append(f"shot set changed (added: {', '.join(added) or '-'}; removed: "
                        f"{', '.join(removed) or '-'}); that needs a new G1 plan")
    changes: dict[str, dict] = {}
    for sid in sorted(set(cur) & set(new)):
        c, n = cur[sid], new[sid]
        for f in FIXED_FIELDS:
            if c.get(f) != n.get(f):
                problems.append(f"{sid}: {f} changed (structural; needs a new G1 plan)")
        cp, np_ = c.get("params") or {}, n.get("params") or {}
        for p in FIXED_PARAMS:
            if cp.get(p) != np_.get(p):
                problems.append(f"{sid}: params.{p} changed {cp.get(p)!r} -> {np_.get(p)!r} (needs a new G1 plan)")
        ch = {f: n.get(f) for f in TEXT_FIELDS if c.get(f) != n.get(f)}
        if "prompt" in ch and not str(ch["prompt"] or "").strip():
            problems.append(f"{sid}: new prompt is empty")
        if ch:
            changes[sid] = ch
    return {"changes": changes, "problems": problems}


def clip_fields(change: dict) -> dict:
    return {k: change[k] for k in CLIP_FIELDS if k in change}
