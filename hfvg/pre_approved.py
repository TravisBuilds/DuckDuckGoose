"""Pre-approved start stills: shots whose start still the owner already approved.

Sources (both optional, any number of shots):

1. ``pre_approved_still`` on a shot in shots.json (a path, or {"path", "sha256"?, "approved_by"?}).
2. An anchor row (hfvg.studio_db.set_anchor_still): the anchor shot's own still is approved, so the
   anchor shot itself is pre-approved (it was skipped entirely before, so it never got a clip). Its
   still stays a continuity ref for its target shots through the existing anchor lookup.

A pre-approved shot skips the still submit and its spend. Its still is copied into the episode
outputs, recorded as the shot's G2 still (flagged ``pre_approved``) and shown at G2; the shot then
waits for G2 like every other shot and goes through clip, QC, clip-only retry and assembly normally.
The end still of an end-frame shot is still generated (paid). The G1 plan and the estimates leave
out the start still cost of these shots.

Pure helpers only (safe to import from workflow code).
"""

from __future__ import annotations

from typing import Any

STILL_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


def pre_approved_still_map(shots: list[dict[str, Any]], anchors: list[dict[str, Any]] | None = None
                           ) -> dict[str, dict[str, Any]]:
    """{shot_id: spec} for every shot in ``shots`` with a pre-approved start still.

    spec = {"path", "sha256" (or None), "approved_by", "source": "shots.json" | "anchor"}. A shots.json
    entry wins over an anchor row for the same shot. Anchors whose anchor shot is not in the plan
    (e.g. a subset run) are ignored here (they still act as continuity refs for their targets)."""
    ids = [s["shot_id"] for s in shots]
    out: dict[str, dict[str, Any]] = {}
    for shot in shots:
        spec = shot.get("pre_approved_still")
        if spec:
            out[shot["shot_id"]] = dict(spec)
    for a in anchors or []:
        sid = a.get("anchor_shot_id")
        if sid in ids and sid not in out and a.get("local_path"):
            out[sid] = {
                "path": a["local_path"],
                "sha256": a.get("sha256"),
                "approved_by": a.get("approved_by") or "owner",
                "source": "anchor",
            }
    return {sid: out[sid] for sid in ids if sid in out}


def pre_approved_ids(shots: list[dict[str, Any]], anchors: list[dict[str, Any]] | None = None) -> set[str]:
    return set(pre_approved_still_map(shots, anchors))
