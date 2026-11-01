"""GAP-79 (ep05v2 r13): reopen an episode from its SAVED state when the Temporal history is gone.

ep05v2 r13: the v10 run COMPLETED on Oct 29; the dev server's 24 h namespace retention then purged its
history, so GAP-41 (reopen_snapshot query on the closed run) had nothing to query ("No workflow found").
Two layers fix it at the root:

1. Persisted snapshot (prevention). The API saves the workflow's reopen_snapshot to
   <episode>/state/reopen_snapshot.json (write_snapshot) on every G_FINAL_CUT approval and from a
   periodic saver, so a later reopen never depends on retention.
2. Reconstruction (recovery, this case). reconstruct_snapshot() rebuilds a version-1 snapshot from what
   the harness itself recorded outside Temporal: the beatmap parse (shot plans), the accepted edit
   (edit/assembly_manifest.json: the clip of every shot), the delivery manifest, the still/clip files on
   disk, and the studio DB (audit_log rows written by every update route, job_ledger job ids).
   Counters are rebuilt so every guard stays at least as strict as before: clip retries per shot (max
   retry n in the audit / ledger), recompose numbering and versions (max n / version), grants and
   GAP-73 lifts. The spend ledger is never touched (it lives in the DB and is not part of a snapshot).

The workflow restores either snapshot through the same GAP-41 path (reopen_problem + _restore_from_snapshot)
and parks at G_FINAL_CUT with the final cut un-approved. A reconstructed snapshot carries
"recompute_signatures": the restored run sets its edit / delivery signatures from the restored state, so
nothing is re-assembled or re-delivered until a clip actually changes. Pure functions here (no Temporal,
no network); the API does the I/O.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = 1
SNAPSHOT_REL = Path("state") / "reopen_snapshot.json"

_RETRY = re.compile(r"^Shot (\w+) clip retry (\d+) \(([^)]+)\): (.*)$", re.S)
_RECOMPOSE = re.compile(r"^Shot (\w+) recompose (\d+) v(\d+) refs=(\[[^\]]*\])(?: shot_meta=\{.*?\})?"
                        r"(?: clip_route=(\{[^{}]*\}))?(?: plate=\S+ \([^)]*\))?: (?P<reason>.*)$", re.S)
_DECISION = re.compile(r"^Shot (\w+) recompose (approved|rejected): (.*)$", re.S)
_GRANT = re.compile(r"^Shot (\w+) recompose grant: (.*)$", re.S)
_ADOPT = re.compile(r"^Shot (\w+) adopted \(template (\w+)\): (.*)$", re.S)
_ACCEPT = re.compile(r"^Shot (\w+) needs_review clip kept(?: \(take (\d+)\))?: (.*)$", re.S)
_CLIP_JOB = re.compile(r"^clip-(?P<ep>.+)-(?P<sid>[A-Za-z0-9]+)-v(?P<ver>\d+)(?:-c(?P<n>\d+))?$")
_STILL_RC_JOB = re.compile(r"^still-(?P<ep>.+)-(?P<sid>[A-Za-z0-9]+)-rc(?P<n>\d+)$")
_CLIP_FILE = re.compile(r"clip_v(\d+)\.mp4$")
_STILL_FILE = re.compile(r"still_v(\d+)\.(jpg|jpeg|png|webp)$")


SHOT_CHANGING_ACTIONS = ("retry_clip", "recompose_shot", "recompose_decision", "accept_clip", "restore_outcome",
                         "adopt_reuse_slot", "grant_recompose", "grant_recompose_override", "set_shot_edit",
                         "retry_shot", "select_still_version", "resync_shot_plans", "approve_still", "reject_still")


class SavedStateError(ValueError):
    """The saved state is missing or inconsistent: reopen-from-saved fails closed."""


def snapshot_path(episode_dir: str | Path) -> Path:
    return Path(episode_dir) / SNAPSHOT_REL


def write_snapshot(episode_dir: str | Path, snapshot: dict, saved_at: str) -> Path:
    """Persist a workflow reopen_snapshot (atomic replace). Refuses a snapshot for another episode."""
    if not isinstance(snapshot, dict) or snapshot.get("version") != SNAPSHOT_VERSION:
        raise SavedStateError("not a version-1 reopen snapshot")
    ep = Path(episode_dir).name
    if snapshot.get("episode_id") != ep:
        raise SavedStateError(f"snapshot is for {snapshot.get('episode_id')!r}, not {ep!r}")
    p = snapshot_path(episode_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"saved_at": saved_at, "snapshot": snapshot}, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, p)
    return p


def read_persisted(episode_dir: str | Path) -> dict | None:
    p = snapshot_path(episode_dir)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    snap = data.get("snapshot") if isinstance(data, dict) else None
    if not isinstance(snap, dict):
        raise SavedStateError(f"{p} has no snapshot")
    return snap


def load_db_rows(db_path: str | Path, episode_id: str) -> dict:
    """The DB rows reconstruct_snapshot needs (read-only)."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as c:
        audit = [{"id": r[0], "action": r[1], "details": r[2] or "", "timestamp": r[3]} for r in c.execute(
            "SELECT id, action, details, timestamp FROM audit_log WHERE episode_id=? ORDER BY id", (episode_id,))]
        jobs = [{"job_id": r[0], "shot_id": r[1], "status": r[2]} for r in c.execute(
            "SELECT job_id, shot_id, status FROM job_ledger WHERE episode_id=?", (episode_id,))]
        ep = c.execute("SELECT beatmap_path, live_mode FROM episodes WHERE episode_id=?", (episode_id,)).fetchone()
    if ep is None:
        raise SavedStateError(f"episode {episode_id!r} is not in the studio DB")
    return {"audit": audit, "jobs": jobs, "beatmap_path": ep[0], "live_mode": bool(ep[1])}


def _files(d: Path, rx) -> dict[int, Path]:
    out = {}
    if d.is_dir():
        for f in sorted(d.iterdir()):
            m = rx.search(f.name)
            if m:
                out.setdefault(int(m.group(1)), f)
    return out


def reconstruct_snapshot(episode_id: str, episode_dir: str | Path, rows: dict, parsed_beatmap: dict,
                         run_dir: str | Path | None = None) -> dict:
    """Rebuild a version-1 reopen snapshot from the saved artifacts + DB rows (pure; see module doc).

    Fails closed (SavedStateError) unless the episode's final cut was approved after its last reopen,
    the accepted edit exists and every workflow shot in it has its clip and its still on disk."""
    ed = Path(episode_dir)
    run_dir = Path(run_dir) if run_dir else ed.parent.parent.parent
    audit = list(rows.get("audit") or [])
    acts = [r["action"] for r in audit]
    if "approve_g_final_cut" not in acts:
        raise SavedStateError("the final cut was never approved (nothing to reopen)")
    last_approve = max(i for i, a in enumerate(acts) if a == "approve_g_final_cut")
    # a reopen after the approval that recorded no shot change leaves the approved state as it was
    if any(a in SHOT_CHANGING_ACTIONS for a in acts[last_approve + 1:]):
        raise SavedStateError("shot changes were recorded after the last final-cut approval (state unknown)")
    man_p = ed / "edit" / "assembly_manifest.json"
    if not man_p.exists():
        raise SavedStateError("no accepted edit (edit/assembly_manifest.json)")
    manifest = json.loads(man_p.read_text(encoding="utf-8"))
    if manifest.get("episode_id") != episode_id:
        raise SavedStateError("assembly manifest is for another episode")
    dpath = ed / "delivery" / "delivery_manifest.json"
    delivery = json.loads(dpath.read_text(encoding="utf-8")) if dpath.exists() else {}

    shots = [dict(s) for s in (parsed_beatmap or {}).get("shots") or []]
    if not shots:
        raise SavedStateError("the beatmap parse has no shots")
    plans = {s["shot_id"]: s for s in shots}
    order = [s["shot_id"] for s in shots]

    # adopted reuse slots (GAP-44): template plan + the adopt reason, appended like adopt_reuse_slot
    adopted_log = []
    for r in audit:
        m = _ADOPT.match(r["details"]) if r["action"] == "adopt_reuse_slot" else None
        if m and m.group(1) not in plans and m.group(2) in plans:
            sid, tpl, why = m.groups()
            plan = dict(plans[tpl])
            for k in ("clip_only", "pre_approved_still", "recompose", "missing_refs", "workflow_id", "status",
                      "clip_prompt", "qc_fail_if", "end_prompt"):
                plan.pop(k, None)
            plan.update({"shot_id": sid, "version": 0, "attempt": 0, "missing_refs": [],
                         "adopted_reuse": {"template": tpl, "reason": why.strip()}})
            plans[sid] = plan
            order.append(sid)
            adopted_log.append({"shot_id": sid, "template": tpl, "reason": why.strip()})

    clips = {c["id"]: c for c in manifest.get("clips") or []}
    outcomes, takes, launched, output_paths = {}, {}, {}, {}
    for sid in order:
        c = clips.get(sid)
        if c is None:
            continue  # not in the accepted edit (dropped / rejected): left without an outcome
        src = c.get("source") or ""
        m = _CLIP_FILE.search(src)
        if not m:
            raise SavedStateError(f"{sid}: edit source {src!r} is not a harness clip file")
        clip_abs = (run_dir / src) if not os.path.isabs(src) else Path(src)
        if not clip_abs.exists():
            raise SavedStateError(f"{sid}: clip {src} is missing on disk")
        version = int(m.group(1)) % 1000
        stills = _files(clip_abs.parent, _STILL_FILE)
        still = stills.get(version)
        if still is None:
            raise SavedStateError(f"{sid}: no still_v{version} beside {src}")
        outcome = {"shot_id": sid, "status": "completed", "clip_path": src, "clip_url": None,
                   "still_url": str(still.resolve()), "still_path": str(still.resolve()),
                   "restored_from": "saved_state"}
        outcomes[sid] = outcome
        output_paths[sid] = {"clip": src}
        tk = {}
        for num, f in _files(clip_abs.parent, _CLIP_FILE).items():
            if num % 1000 == version:
                n = num // 1000
                rel = str(Path(src).parent / f.name)
                tk[n] = dict(outcome) if rel == src else {
                    "shot_id": sid, "status": "unknown", "clip_path": rel, "clip_url": None,
                    "still_url": outcome["still_url"], "restored_from": "saved_state"}
        takes[sid] = tk
        plan = dict(plans[sid])
        plan.update({"attempt": version, "version": version, "dry_run": not rows.get("live_mode", True)})
        plan.pop("pre_approved_still", None)
        if c.get("shot_meta"):
            plan["shot_meta"] = dict(c["shot_meta"])
        launched[sid] = plan
    if not outcomes:
        raise SavedStateError("the accepted edit has no workflow shot")

    # counters from the audit (every update route writes one row) and the ledger job ids
    clip_retries: dict[str, int] = {}
    retry_log, recompose_log, grant_log, accept = [], [], [], {}
    rc_versions: dict[str, int] = {}
    grants: dict[str, int] = {}
    lifts: dict[str, list[int]] = {}
    last_rc: dict[str, int] = {}
    for r in audit:
        d, a = r["details"], r["action"]
        if a == "retry_clip" and (m := _RETRY.match(d)):
            sid, n = m.group(1), int(m.group(2))
            clip_retries[sid] = max(clip_retries.get(sid, 0), n)
            retry_log.append({"round": "G3_CLIPS", "shot_id": sid, "n": n, "job_id": m.group(3),
                              "reason": m.group(4).strip(), "restored_from": "audit_log"})
        elif a == "recompose_shot" and (m := _RECOMPOSE.match(d)):
            sid, n, v = m.group(1), int(m.group(2)), int(m.group(3))
            last_rc[sid] = n
            rc_versions[sid] = max(rc_versions.get(sid, 0), v)
            recompose_log.append({"shot_id": sid, "n": n, "version": v,
                                  "workflow_id": f"{episode_id}-shot-{sid}-recompose{n}",
                                  "reason": m.group("reason").strip(), "decision": None, "restored_from": "audit_log"})
        elif a == "recompose_decision" and (m := _DECISION.match(d)):
            sid = m.group(1)
            for row in reversed(recompose_log):
                if row["shot_id"] == sid and row["decision"] is None:
                    row["decision"], row["decision_reason"] = m.group(2), m.group(3).strip()
                    break
            if m.group(2) == "approved" and grants.get(sid) and last_rc.get(sid, 0) > 2:
                lifts.setdefault(sid, []).append(last_rc[sid])
        elif a in ("grant_recompose", "grant_recompose_override") and (m := _GRANT.match(d)):
            grants[m.group(1)] = grants.get(m.group(1), 0) + 1
            grant_log.append({"shot_id": m.group(1), "reason": m.group(2).strip(), "restored_from": "audit_log"})
        elif a == "accept_clip" and (m := _ACCEPT.match(d)):
            accept[m.group(1)] = {"reason": m.group(3).strip(), "restored_from": "audit_log",
                                  **({"take": int(m.group(2))} if m.group(2) else {})}
    rc_counts: dict[str, int] = {}
    for row in recompose_log:
        rc_counts[row["shot_id"]] = max(rc_counts.get(row["shot_id"], 0), row["n"])
    for j in rows.get("jobs") or []:
        jid = j.get("job_id") or ""
        if (m := _CLIP_JOB.match(jid)) and m.group("ep") == episode_id and m.group("n"):
            sid = m.group("sid")
            clip_retries[sid] = max(clip_retries.get(sid, 0), int(m.group("n")))
        elif (m := _STILL_RC_JOB.match(jid)) and m.group("ep") == episode_id:
            sid = m.group("sid")
            rc_counts[sid] = max(rc_counts.get(sid, 0), int(m.group("n")))
    still_children = {}
    for sid in launched:
        clip_dir = Path(outcomes[sid]["still_path"]).parent
        vers = sorted(_files(clip_dir, _STILL_FILE))
        rc_versions[sid] = max([rc_versions.get(sid, 0)] + vers)
        still_children[sid] = [{"version": v, "workflow_id": f"{episode_id}-shot-{sid}-saved-v{v}",
                                "kind": "saved_state"} for v in vers]
    acceptances = {sid: v for sid, v in accept.items() if sid in outcomes}

    restored_qc = {"verdict": "RESTORED", "blocking": False, "escalate": False,
                   "reason": "restored from the saved accepted edit (GAP-79); re-checked on the next rebuild"}
    state = {
        "episode_id": episode_id, "stage": "mute_edit", "idea": "", "current_gate": "G_FINAL_CUT",
        "shot_attempts": {sid: launched[sid]["version"] for sid in launched},
        "output_paths": output_paths, "pending_retries": {},
        "assembly_path": str((ed / "edit" / "mute_assembly.mp4").resolve()),
        "assembly_manifest": manifest, "assembly_qc": dict(restored_qc),
        "delivery_paths": dict(delivery.get("paths") or {}), "delivery_manifest": delivery,
        "delivery_qc": dict(restored_qc) if delivery else {},
    }
    attrs: dict[str, Any] = {
        "passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"],
        "approved_g1_shot_plan": True, "approved_g2_scene_stills": True, "approved_g_final_cut": True,
        "parsed_beatmap": parsed_beatmap, "_beatmap_path": rows.get("beatmap_path"),
        "_dry_run": not rows.get("live_mode", True),
        "launched_shot_plans": launched, "assembly_shots": [launched[s] for s in order if s in launched],
        "shot_outcomes": outcomes, "clip_takes": {s: {str(n): t for n, t in tk.items()} for s, tk in takes.items()},
        "clip_retries": clip_retries, "clip_retry_log": retry_log, "pending_clip_retries": {},
        "clip_review_acceptances": acceptances,
        "recompose_counts": rc_counts, "recompose_versions": rc_versions, "recompose_log": recompose_log,
        "recompose_grants": grants, "recompose_grant_log": grant_log, "g3_clip_lifts": lifts,
        "pending_recompose": {}, "adopted_reuse_log": adopted_log,
        "still_version_children": still_children,
        "assembly_version": max(1, int(delivery.get("edit_version") or 0)),
        "delivery_version": int(delivery.get("version") or 0), "delivery_phase": "done" if delivery else None,
    }
    return {"version": SNAPSHOT_VERSION, "episode_id": episode_id, "state": state, "attrs": attrs,
            "shot_workflow_ids": {}, "still_version_ids": [], "reconstructed": True,
            "recompute_signatures": True}


def saved_snapshot(episode_id: str, episode_dir: str | Path, db_path: str | Path,
                   parsed_beatmap_fn, ignore_persisted: bool = False) -> tuple[dict, str]:
    """(snapshot, source): the persisted snapshot when present, else a reconstruction.
    parsed_beatmap_fn(beatmap_path) -> parsed beatmap dict (only called for a reconstruction).
    ignore_persisted: the file came from the run being superseded (see the API), so rebuild."""
    persisted = None if ignore_persisted else read_persisted(episode_dir)
    rows = load_db_rows(db_path, episode_id)
    if persisted is not None:
        saved_at = json.loads(snapshot_path(episode_dir).read_text(encoding="utf-8")).get("saved_at") or ""
        newer = [r for r in rows["audit"] if r["action"] in SHOT_CHANGING_ACTIONS + ("approve_g_final_cut",)
                 and str(r["timestamp"]) > saved_at]
        if newer:
            raise SavedStateError(f"the persisted snapshot ({saved_at} UTC) is older than recorded changes "
                                  f"({newer[-1]['action']} at {newer[-1]['timestamp']} UTC)")
        return persisted, "persisted"
    if not rows.get("beatmap_path"):
        raise SavedStateError("no beatmap path recorded for the episode")
    return reconstruct_snapshot(episode_id, episode_dir, rows, parsed_beatmap_fn(rows["beatmap_path"])), "reconstructed"
