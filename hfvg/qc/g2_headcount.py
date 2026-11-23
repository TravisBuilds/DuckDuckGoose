"""G2 headcount QC (GAP-27): who is in each kept still, figure by figure, against the shot plan.

ep05v2 V12 v2 showed FOUR main characters for a 3-character shot and still reached the G2 package:
nothing compared the figures in a still with the cast. This check does (hfvg.qc.headcount), for the
still version each shot will actually animate (the latest re-roll or the version kept with
select_still_version): every declared figure counted as declared, no unexpected figure.

A FAIL BLOCKS G2 approval (API, 409) unless the reviewer overrides that shot with an audited reason.
Counts come from, in order: a reviewer headcount recorded for that exact still
(POST /api/episodes/{ep}/shots/{shot}/headcount, stored in <EPISODES_DIR>/<ep>/g2_headcounts.json),
then the registered counter (the vision counter when the QC judges are on, docs/QC-JUDGES.md: one
cached call per still). With neither the shot is NEEDS_REVIEW - never a silent PASS; a counter
error is NEEDS_REVIEW for that shot.

A reviewer row is ``{"figures": {figure id: n}, "unexpected": n, "background": n, "by", "note"}``,
the shape of the counter's answer; rows recorded before per-figure counts (cast / duck / other) are
read through hfvg.qc.animal_count.legacy_row_answer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from hfvg import paths
from hfvg.qc.animal_count import expected_cast, legacy_row_answer
from hfvg.qc.headcount import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FAIL,
    NEEDS_REVIEW,
    PASS,
    CastCounter,
    Roster,
    check_headcount,
    get_cast_counter,
    shot_roster,
)

__all__ = ["REPORT_CONCURRENCY", "ReviewerHeadcountCounter", "check_still_headcount", "expected_cast",
           "g2_headcount_report", "headcounts_path", "load_headcounts", "record_headcount"]

REPORT_CONCURRENCY = 4  # shots counted at once (each may be one judge call)
_STILL_RE = re.compile(r"^still_v(\d+)(_end)?\.(?:jpe?g|png|webp)$", re.IGNORECASE)
_FIGURE_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
MAX_FIGURES = 32


def headcounts_path(episode_id: str, episodes_dir: str | os.PathLike | None = None) -> Path:
    root = Path(episodes_dir) if episodes_dir is not None else paths.episodes_dir()
    return root / episode_id / "g2_headcounts.json"


def load_headcounts(episode_id: str, episodes_dir=None) -> dict:
    p = headcounts_path(episode_id, episodes_dir)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _count(counts: dict[str, Any], key: str, default: int | None = None) -> int:
    v = counts.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError(f"{key} must be a non-negative integer, got {v!r}")
    return v


def _row(counts: dict[str, Any]) -> dict[str, Any]:
    """The stored counts of one still: per figure (``figures``), or cast / duck / other as before."""
    if counts.get("figures") is None:
        cast, duck, other = _count(counts, "cast"), _count(counts, "duck"), _count(counts, "other", 0)
        return {"cast": cast, "duck": duck, "other": other, "animal": cast + duck + other}
    figures = counts["figures"]
    if not isinstance(figures, dict) or len(figures) > MAX_FIGURES:
        raise ValueError(f"figures must be an object of at most {MAX_FIGURES} figure ids and counts")
    for fid in figures:
        if not isinstance(fid, str) or not _FIGURE_ID_RE.match(fid):
            raise ValueError(f"figure id {fid!r} is not a cast id")
    return {"figures": {fid: _count(figures, fid) for fid in sorted(figures)},
            "unexpected": _count(counts, "unexpected", 0), "background": _count(counts, "background", 0)}


def record_headcount(episode_id: str, shot_id: str, version: int, counts: dict[str, Any], by: str,
                     note: str = "", episodes_dir=None) -> dict:
    """Store a reviewer's count of one still version (atomic write). Returns the stored row."""
    row = {**_row(counts), "by": by, "note": note}
    data = load_headcounts(episode_id, episodes_dir)
    data.setdefault(shot_id, {})[str(int(version))] = row
    p = headcounts_path(episode_id, episodes_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)
    return row


def reviewer_answer(row: dict[str, Any], roster: Roster, shot_meta: dict[str, Any] | None) -> dict[str, Any]:
    """A reviewer row in the counter's answer shape. A figure the shot does not declare (the plan
    changed after the count) is an unexpected figure; a declared one missing from the row is not
    counted (NEEDS_REVIEW)."""
    method = f"reviewer_headcount ({row.get('by') or 'reviewer'})"
    if not isinstance(row.get("figures"), dict):
        return {**legacy_row_answer(row, roster, shot_meta), "method": method}
    figures, unexpected = {}, _count(row, "unexpected", 0)
    for fid, n in row["figures"].items():
        if roster.figure(fid) is None:
            unexpected += _count(row["figures"], fid)
        else:
            figures[fid] = {"count": n, "matches": True}
    return {"figures": figures, "unexpected": unexpected, "background": _count(row, "background", 0),
            "confidence": 1.0, "method": method}


class ReviewerHeadcountCounter(CastCounter):
    """Counts a human reviewer recorded for outputs/<shot>/still_v<N>.<ext>; else defers to `fallback`."""

    def __init__(self, headcounts: dict, fallback: CastCounter | None = None):
        self.headcounts = headcounts or {}
        self.fallback = fallback

    def lookup(self, media_path: str | Path) -> dict | None:
        p = Path(media_path)
        m = _STILL_RE.match(p.name)
        if not m or m.group(2):
            return None
        row = (self.headcounts.get(p.parent.name) or {}).get(str(int(m.group(1))))
        return row if isinstance(row, dict) else None

    async def count_cast(self, media_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        row = self.lookup(media_path)
        if row is not None:
            return reviewer_answer(row, shot_context["roster"], shot_context.get("shot_meta"))
        if self.fallback is not None:
            return await self.fallback.count_cast(media_path, shot_context)
        raise ValueError("no reviewer headcount recorded for this still and no counter")

    def has_source(self, media_path) -> bool:
        return self.lookup(media_path) is not None or self.fallback is not None


async def check_still_headcount(counter: CastCounter | None, image_path: str | Path | None,
                                shot_meta: dict | None,
                                confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                                shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """The headcount of one still against the shot's declared figures (hfvg.qc.headcount). Worst
    status wins; a counter error is NEEDS_REVIEW. ``shot_context`` goes to the counter."""
    if shot_roster(shot_meta) is not None and (
            counter is None or (isinstance(counter, ReviewerHeadcountCounter) and image_path
                                and not counter.has_source(image_path))):
        res = await check_headcount(None, image_path, shot_meta, confidence_threshold)
        return {**res, "reason": "no headcount counter configured and no reviewer headcount recorded for this still"}
    return await check_headcount(counter, image_path, shot_meta, confidence_threshold, shot_context, media="still")


def _still_for(out_dir: Path, version: int | None) -> Path | None:
    if version is None:
        return None
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        p = out_dir / f"still_v{version}{ext}"
        if p.is_file():
            return p
    return None


async def g2_headcount_report(episode_id: str, versions: dict, plans: dict[str, dict | None],
                              decisions: dict | None = None, counter: CastCounter | None = None,
                              data_dir=None, episodes_dir=None,
                              overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """Headcount of every non-rejected shot's kept still version.

    versions: EpisodeWorkflowV2.still_versions query; plans: shot -> still_version_plan of the kept
    version; decisions: shot_decisions (rejected shots are skipped); overrides: shot -> reason.
    Returns {"status", "blocking": [shots that FAIL and are not overridden], "shots": {...},
    "judge_budget_used_up": [shots sent to a person because the judge budget ran out]}."""
    decisions = decisions or {}
    overrides = overrides or {}
    data_root = Path(data_dir) if data_dir is not None else paths.data_dir()
    reviewer = ReviewerHeadcountCounter(load_headcounts(episode_id, episodes_dir),
                                        counter if counter is not None else get_cast_counter())
    gate = asyncio.Semaphore(REPORT_CONCURRENCY)

    async def shot_report(shot_id: str) -> dict[str, Any]:
        if decisions.get(shot_id) == "rejected":
            return {"status": "SKIPPED", "reason": "rejected at G2 (never animated)"}
        v = (versions[shot_id] or {}).get("current_version")
        plan = plans.get(shot_id) or {}
        path = _still_for(data_root / "episodes" / episode_id / "outputs" / shot_id, v)
        async with gate:
            res = await check_still_headcount(
                reviewer, path, plan.get("shot_meta"),
                shot_context={"episode_id": episode_id, "shot_id": shot_id, "refs": plan.get("refs")})
        res.update({"version": v, "still": str(path) if path else None})
        if res["status"] == FAIL and shot_id in overrides:
            res["override"] = overrides[shot_id]
        return res

    ordered = sorted(versions or {})
    shots: dict[str, Any] = dict(zip(ordered, await asyncio.gather(*(shot_report(s) for s in ordered))))
    blocking = sorted(s for s, r in shots.items() if r.get("status") == FAIL and s not in overrides)
    statuses = {r.get("status") for r in shots.values()}
    status = FAIL if blocking else (NEEDS_REVIEW if NEEDS_REVIEW in statuses or FAIL in statuses else PASS)
    used_up = sorted(s for s, r in shots.items() if r.get("judge_budget_used_up"))
    return {"status": status, "blocking": blocking, "shots": shots, "judge_budget_used_up": used_up}
