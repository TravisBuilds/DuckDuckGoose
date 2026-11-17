"""GAP-28: re-sync the corrected Studio package into a running episode at G2 (no spend).

The episode parses BEATMAP.md + shots.json ONCE at start; every later still re-roll, clip, clip QC
and clip retry used that parse. A package corrected at G2 (ep05v2 2026-10-27: the duck vest wording
and qc_fail_if) therefore never reached the prompts the harness actually sends. This module diffs the
re-parsed package against the plans in force: text fields (prompt, end_prompt, clip_prompt,
qc_fail_if) may change; structural fields (shot set, shot_meta, ref_keys, trim, model/duration/
end frame/aspect/resolution) may not, because the G1 plan, estimates and refs were approved on them.

plan_replan is the G1 counterpart (before approval): besides the text fields, the shots' cast, props
and place may change, with the refs and shot_meta keys they plan to; the rest stays as it was parsed.
"""
from __future__ import annotations

from hfvg.cast_prompt import is_cast_key

TEXT_FIELDS = ("prompt", "end_prompt", "clip_prompt", "qc_fail_if")
CLIP_FIELDS = ("clip_prompt", "qc_fail_if")  # what a parked still child's clip phase picks up
FIXED_FIELDS = ("shot_meta", "ref_keys", "trim")
FIXED_PARAMS = ("model", "duration", "end_frame", "aspect_ratio", "resolution")
# G1 re-plan: what picking a shot's cast, props and place changes (hfvg.cast_plan.plan_cast)
PICK_META_KEYS = ("cast", "props", "text_policy")
REPLAN_FIELDS = TEXT_FIELDS + ("shot_meta", "ref_keys", "refs", "missing_refs")
REPLAN_FIXED_FIELDS = ("trim", "tags", "pre_approved_still")


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


def _shot_set_problem(cur: dict, new: dict) -> list[str]:
    added, removed = sorted(set(new) - set(cur)), sorted(set(cur) - set(new))
    if not (added or removed):
        return []
    return [f"shot set changed (added: {', '.join(added) or '-'}; removed: {', '.join(removed) or '-'}); "
            "a different shot list needs a new run"]


def plan_replan(current_shots: list[dict], new_shots: list[dict]) -> dict:
    """G1, before approval: {"changes": {shot_id: {field: new value}}, "problems": [...]} for the
    re-parsed package against the plan waiting for approval (problems = refuse the re-plan).

    May change: the text fields, and the shot's cast, props and place: shot_meta's cast, props and
    text_policy keys, the cast-library ref keys (cast:...) and the refs they resolve to. May not:
    the shot set, trim, tags, params, a pre-approved still, the rest of shot_meta or the package's
    own (refs.json) ref keys."""
    cur = {s["shot_id"]: s for s in current_shots or []}
    new = {s["shot_id"]: s for s in new_shots or []}
    problems = _shot_set_problem(cur, new)
    changes: dict[str, dict] = {}
    for sid in sorted(set(cur) & set(new)):
        c, n = cur[sid], new[sid]
        for f in REPLAN_FIXED_FIELDS:
            if c.get(f) != n.get(f):
                problems.append(f"{sid}: {f} changed (only the prompts and the shot's cast, props and place "
                                "can change before approval)")
        cp, np_ = c.get("params") or {}, n.get("params") or {}
        moved = sorted(k for k in set(cp) | set(np_) if cp.get(k) != np_.get(k))
        if moved:
            problems.append(f"{sid}: params {', '.join(moved)} changed (needs a new run)")
        cm = {k: v for k, v in (c.get("shot_meta") or {}).items() if k not in PICK_META_KEYS}
        nm = {k: v for k, v in (n.get("shot_meta") or {}).items() if k not in PICK_META_KEYS}
        if cm != nm:
            keys = sorted(k for k in set(cm) | set(nm) if cm.get(k) != nm.get(k))
            problems.append(f"{sid}: shot_meta {', '.join(keys)} changed (needs a new run)")
        own = [k for k in c.get("ref_keys") or [] if not is_cast_key(k)]
        if own != [k for k in n.get("ref_keys") or [] if not is_cast_key(k)]:
            problems.append(f"{sid}: the shot's refs.json refs changed (needs a new run)")
        ch = {f: n.get(f) for f in REPLAN_FIELDS if c.get(f) != n.get(f)}
        if "prompt" in ch and not str(ch["prompt"] or "").strip():
            problems.append(f"{sid}: new prompt is empty")
        if ch:
            changes[sid] = ch
    return {"changes": changes, "problems": problems}
