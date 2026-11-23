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
the shape of the counter's answer. Figure-count rows recorded before per-figure counts hold three
totals: ``cast`` (the main characters, the show's flag figures excepted), the flag figures' count
(``flag_counts``; stored under the key those rows always had, ``legacy_show.ROW_FLAG_KEY``, and read
under either) and ``other``; they are read as one answer per figure (hfvg.qc.headcount.totals_answer).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from hfvg import paths
from hfvg.compat.legacy_show import ROW_FLAG_KEY, ROW_TOTAL_KEY
from hfvg.qc.headcount import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FAIL,
    NEEDS_REVIEW,
    PASS,
    CastCounter,
    FlagFigures,
    Roster,
    check_headcount,
    get_cast_counter,
    shot_roster,
    show_flag_figures,
    totals_answer,
)
from hfvg.qc.judges import count_of
from hfvg.show_profile import ShowProfileError

__all__ = ["FLAG_COUNTS", "REPORT_CONCURRENCY", "ReviewerHeadcountCounter", "check_still_headcount",
           "g2_headcount_report", "headcounts_path", "load_headcounts", "record_headcount", "reviewer_answer"]

FLAG_COUNTS = "flag_counts"  # the flag figures' count in a figure-count row (also read as ROW_FLAG_KEY)

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


def flag_count_key(counts: dict[str, Any]) -> str:
    """The key a figure-count row holds the flag figures' count under (FLAG_COUNTS or ROW_FLAG_KEY).
    ValueError when it holds both with different values."""
    if ROW_FLAG_KEY not in counts:
        return FLAG_COUNTS
    if FLAG_COUNTS in counts and counts[FLAG_COUNTS] != counts[ROW_FLAG_KEY]:
        raise ValueError(f"{FLAG_COUNTS} and {ROW_FLAG_KEY} are the same count; they differ")
    return ROW_FLAG_KEY


def _row(counts: dict[str, Any]) -> dict[str, Any]:
    """The stored counts of one still: per figure (``figures``), or the three totals of a figure-count
    row, stored under the keys such rows always had."""
    if counts.get("figures") is None:
        cast = _count(counts, "cast")
        flagged, other = _count(counts, flag_count_key(counts)), _count(counts, "other", 0)
        return {"cast": cast, ROW_FLAG_KEY: flagged, "other": other, ROW_TOTAL_KEY: cast + flagged + other}
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


def reviewer_answer(row: dict[str, Any], roster: Roster, shot_meta: dict[str, Any] | None,
                    flag_figures: FlagFigures | None = None) -> dict[str, Any]:
    """A reviewer row in the counter's answer shape. A figure the shot does not declare (the plan
    changed after the count) is an unexpected figure; a declared one missing from the row is not
    counted (NEEDS_REVIEW). ``flag_figures``: the show's, for a figure-count row (None:
    ``show_flag_figures()``)."""
    method = f"reviewer_headcount ({row.get('by') or 'reviewer'})"
    if not isinstance(row.get("figures"), dict):
        figures = show_flag_figures() if flag_figures is None else flag_figures
        flagged = row.get(ROW_FLAG_KEY) if ROW_FLAG_KEY in row else row.get(FLAG_COUNTS)
        return {**totals_answer(roster, shot_meta, figures, cast=count_of(row.get("cast")), flagged=count_of(flagged),
                                unexpected=count_of(row.get("other", 0)), matches=True, confidence=1.0, method=""),
                "method": method}
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
            return reviewer_answer(row, shot_context["roster"], shot_context.get("shot_meta"),
                                   shot_context.get("flag_figures"))
        if self.fallback is not None:
            return await self.fallback.count_cast(media_path, shot_context)
        raise ValueError("no reviewer headcount recorded for this still and no counter")

    def has_source(self, media_path) -> bool:
        return self.lookup(media_path) is not None or self.fallback is not None


async def check_still_headcount(counter: CastCounter | None, image_path: str | Path | None,
                                shot_meta: dict | None,
                                confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                                shot_context: dict[str, Any] | None = None,
                                flag_figures: FlagFigures | None = None) -> dict[str, Any]:
    """The headcount of one still against the shot's declared figures (hfvg.qc.headcount). Worst
    status wins; a counter error is NEEDS_REVIEW. ``shot_context`` goes to the counter;
    ``flag_figures``: the show's (None: those of ``shot_context["episode_id"]``'s show)."""
    if flag_figures is None:
        try:
            flag_figures = show_flag_figures((shot_context or {}).get("episode_id"))
        except ShowProfileError:
            flag_figures = None  # check_headcount says why the shot cannot be checked
    if flag_figures is not None and shot_roster(shot_meta, flag_figures) is not None and (
            counter is None or (isinstance(counter, ReviewerHeadcountCounter) and image_path
                                and not counter.has_source(image_path))):
        res = await check_headcount(None, image_path, shot_meta, confidence_threshold, flag_figures=flag_figures)
        return {**res, "reason": "no headcount counter configured and no reviewer headcount recorded for this still"}
    return await check_headcount(counter, image_path, shot_meta, confidence_threshold, shot_context, media="still",
                                 flag_figures=flag_figures)


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
    try:
        flag_figures = show_flag_figures(episode_id)
    except ShowProfileError:
        flag_figures = None  # every shot says why it cannot be checked
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
                shot_context={"episode_id": episode_id, "shot_id": shot_id, "refs": plan.get("refs")},
                flag_figures=flag_figures)
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
