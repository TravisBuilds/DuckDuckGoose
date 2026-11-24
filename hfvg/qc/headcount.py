"""Headcount QC (judge b, docs/QC-JUDGES.md): who is in frame, figure by figure, against the shot plan.

``shot_roster`` turns a shot's plan into the figures it declares:

- one figure per ``shot_meta.cast`` entry, a character of the cast library of any kind (a person, a
  creature, a robot, a puppet, a prop that acts), expected once;
- ``shot_meta.count`` is the number of main characters in frame: those without a cast entry form
  one group, counted together;
- figures that older shots.json keys declare outside ``count`` (hfvg.qc.animal_count.legacy_figures).

A ``CastCounter`` answers for one still or clip, in one shape whoever answers (the vision judge, a
reviewer's recorded count, an older counter through its adapter): how many of each figure are in
frame and whether each looks like the declared character, how many figures nobody declared stand
out as subjects (``unexpected``) and how many are only in the background (``background``).

``check_headcount`` compares the answer with the roster, the worst row deciding:

- FAIL: a figure counted another number of times than declared, an unexpected figure;
- NEEDS_REVIEW (a person decides): a figure that may not be the declared character, background
  figures, a figure the counter did not count, a confidence below the threshold, a counter that
  cannot answer (the judge budget used up says so), no counter, no media, a count below the cast list;
- PASS: everything matches.

The G2 headcount (hfvg.qc.g2_headcount, a reviewer's count first) and assembly QC
(hfvg.activities.qc.check_assembly_qc) run this check.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hfvg.qc.judges import JudgeBudgetExhausted, confidence_of, count_of

PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"
_RANK = {PASS: 0, NEEDS_REVIEW: 1, FAIL: 2}

DEFAULT_CONFIDENCE_THRESHOLD = 0.7
# The main characters without a cast entry. Cast slugs never start with "_".
GROUP_ID = "_cast"


@dataclass(frozen=True)
class Figure:
    """One declared figure: a cast entry, or a group of main characters counted together."""

    id: str
    name: str
    expected: int = 1
    description: str = ""
    group: bool = False
    locked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "expected": self.expected, "group": self.group,
                "locked": self.locked}


@dataclass(frozen=True)
class Roster:
    """The figures a shot declares. ``note``: a declaration a person must confirm (never a pass)."""

    figures: tuple[Figure, ...]
    note: str | None = None

    def figure(self, figure_id: Any) -> Figure | None:
        return next((f for f in self.figures if f.id == figure_id), None)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"figures": [f.as_dict() for f in self.figures], "unexpected": 0}
        if self.note:
            out["note"] = self.note
        return out


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def cast_figures(shot_meta: dict[str, Any] | None) -> list[Figure]:
    """One figure per declared cast entry (``shot_meta.cast``), in plan order, each slug once."""
    cast = (shot_meta or {}).get("cast")
    out: list[Figure] = []
    for entry in cast if isinstance(cast, list) else []:
        slug = entry.get("character") if isinstance(entry, dict) else None
        if not isinstance(slug, str) or not slug.strip() or any(f.id == slug for f in out):
            continue
        out.append(Figure(slug, _one_line(entry.get("name")) or slug, description=_one_line(entry.get("clause")),
                          locked=entry.get("locked") is True))
    return out


def shot_roster(shot_meta: dict[str, Any] | None) -> Roster | None:
    """The figures the shot declares; None when it declares neither a count nor a cast."""
    from hfvg.qc.animal_count import legacy_figures

    meta = shot_meta if isinstance(shot_meta, dict) else {}
    named = cast_figures(meta)
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    if count is None and not named:
        return None
    extra, covered = legacy_figures(meta, len(named), count)
    main = len(named) - covered
    figures = list(named)
    note = None
    if count is not None and count > main:
        figures.append(Figure(GROUP_ID, "other cast" if named else "cast", expected=count - main, group=True))
    elif count is not None and count < main:
        note = (f"the shot plan says {count} main character(s) in frame but its cast lists {main}: "
                "a person confirms who belongs in the shot")
    return Roster(tuple(figures) + tuple(extra), note)


class CastCounter(ABC):
    """Counts the declared figures of one still or clip."""

    @abstractmethod
    async def count_cast(self, media_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        """Answer for the figures of ``shot_context["roster"]`` (raise when there is no answer).

        ``shot_context`` also carries ``shot_meta``, ``episode_id``, ``shot_id`` and, for a clip,
        ``window`` (its trim window). The answer::

            {"figures": {figure id: {"count": int, "matches": bool}},
             "together": [{"ids": [figure id, ...], "count": int, "label": str}],  # optional
             "unexpected": int, "background": int, "confidence": float, "method": str}

        ``matches`` is False when a figure counted under a character's id may not be that character;
        ``together`` counts several figures as one number (a counter that cannot tell them apart,
        with an optional ``matches`` of its own).
        """


class FakeCastCounter(CastCounter):
    """Deterministic counter for tests: every declared figure counted as declared, unless ``counts``
    ({figure id or "unexpected" / "background": n}, or {(file name, key): n} for one file) says
    otherwise. ``mismatched``: the ids counted that may not be the declared character."""

    def __init__(self, counts: dict | None = None, confidence: float = 1.0, mismatched: tuple = (),
                 method: str = "fake_deterministic"):
        self.counts = dict(counts or {})
        self.confidence = confidence
        self.mismatched = set(mismatched)
        self.method = method
        self.calls: list[str] = []
        self.contexts: list[dict[str, Any]] = []

    async def count_cast(self, media_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        name = Path(media_path).name
        self.calls.append(name)
        self.contexts.append(shot_context)

        def get(key: str, default: int) -> int:
            return int(self.counts.get((name, key), self.counts.get(key, default)))

        return {"figures": {f.id: {"count": get(f.id, f.expected), "matches": f.id not in self.mismatched}
                            for f in shot_context["roster"].figures},
                "unexpected": get("unexpected", 0), "background": get("background", 0),
                "confidence": self.confidence, "method": self.method}


_COUNTER: CastCounter | None = None


def set_cast_counter(counter: CastCounter | None) -> None:
    global _COUNTER
    _COUNTER = counter


def get_cast_counter() -> CastCounter | None:
    return _COUNTER


def checked_answer(raw: Any, roster: Roster) -> dict[str, Any]:
    """A counter's answer in one shape (ValueError when it is malformed: never read as zero)."""
    if not isinstance(raw, dict):
        raise ValueError("the counter gave no answer")
    raw_figures = raw.get("figures") or {}
    if not isinstance(raw_figures, dict):
        raise ValueError("figures is not an object")
    figures = {}
    for fid, row in raw_figures.items():
        if roster.figure(fid) is None:
            continue
        if not isinstance(row, dict):
            raise ValueError(f"no count for {fid}")
        matches = row.get("matches")
        figures[fid] = {"count": count_of(row.get("count")), "matches": matches if isinstance(matches, bool) else None}
    together = []
    for group in raw.get("together") or []:
        ids = [i for i in (group.get("ids") or []) if roster.figure(i) is not None] if isinstance(group, dict) else []
        if ids:
            matches = group.get("matches")
            together.append({"ids": ids, "count": count_of(group.get("count")),
                             "label": _one_line(group.get("label")) or "cast",
                             "matches": matches if isinstance(matches, bool) else None})
    conf = raw.get("confidence")
    return {"figures": figures, "together": together, "unexpected": count_of(raw.get("unexpected")),
            "background": count_of(raw.get("background")), "confidence": 0.0 if conf is None else confidence_of(conf),
            "method": str(raw.get("method") or "")}


def _row(figure: str | None, label: str, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"figure": figure, "label": label, "status": status, "reason": reason,
            **{k: v for k, v in extra.items() if v is not None}}


def _count_row(figure: str | None, label: str, expected: int, counted: int, matches: bool | None,
               common: dict[str, Any]) -> dict[str, Any]:
    numbers = {"expected": expected, "counted": counted, **common}
    if counted != expected:
        return _row(figure, label, FAIL, f"{label}: expected {expected}, counted {counted}", **numbers)
    if matches is False:
        return _row(figure, label, NEEDS_REVIEW, f"{label}: counted, but may not be the declared character",
                    matches=False, **numbers)
    return _row(figure, label, PASS, f"{label}: {expected} as declared", **numbers)


def compare_headcount(roster: Roster, answer: dict[str, Any], confidence_threshold: float) -> list[dict[str, Any]]:
    """The rows of one checked answer (``checked_answer``) against the roster."""
    common = {"method": answer["method"], "confidence": answer["confidence"]}
    conf = answer["confidence"]
    if conf < confidence_threshold:
        return [_row(None, "headcount", NEEDS_REVIEW, f"low confidence {conf:.2f} < {confidence_threshold}", **common)]
    rows = []
    grouped: set[str] = set()
    for group in answer["together"]:
        grouped.update(group["ids"])
        expected = sum(roster.figure(i).expected for i in group["ids"])
        rows.append(_count_row(None, group["label"], expected, group["count"], group["matches"], common))
    for fig in roster.figures:
        if fig.id in grouped:
            continue
        got = answer["figures"].get(fig.id)
        if got is None:
            rows.append(_row(fig.id, fig.name, NEEDS_REVIEW, f"{fig.name}: not counted", expected=fig.expected,
                             **common))
        else:
            rows.append(_count_row(fig.id, fig.name, fig.expected, got["count"], None if fig.group else got["matches"],
                                   common))
    unexpected = answer["unexpected"]
    rows.append(_row(None, "unexpected figures", FAIL if unexpected else PASS,
                     f"unexpected figures: expected 0, counted {unexpected}" if unexpected else "no unexpected figures",
                     expected=0, counted=unexpected, **common))
    if answer["background"]:
        rows.append(_row(None, "background figures", NEEDS_REVIEW,
                         f"background figures nobody declared: {answer['background']} (a person decides)",
                         counted=answer["background"], **common))
    if roster.note:
        rows.append(_row(None, "cast list", NEEDS_REVIEW, roster.note, **common))
    return rows


def _unanswered(e: BaseException) -> str:
    if isinstance(e, JudgeBudgetExhausted):
        return str(e)[:300]
    return f"counter error: {str(e)[:200]}"


def worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _RANK.get(s, 1), default=PASS)


async def check_headcount(counter: CastCounter | None, media_path: str | Path | None, shot_meta: dict[str, Any] | None,
                          confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                          shot_context: dict[str, Any] | None = None, *, media: str = "media") -> dict[str, Any]:
    """Count the shot's declared figures in one still or clip. Never raises: a counter that cannot
    answer is NEEDS_REVIEW for this shot."""
    roster = shot_roster(shot_meta)
    if roster is None:
        return {"status": NEEDS_REVIEW, "checks": [], "expected": None,
                "reason": "shot declares no cast to verify against"}
    expected = roster.as_dict()
    if counter is None:
        return {"status": NEEDS_REVIEW, "checks": [], "expected": expected,
                "reason": "no headcount counter configured: a person counts who is in frame"}
    if not media_path or not Path(media_path).is_file():
        return {"status": NEEDS_REVIEW, "checks": [], "expected": expected,
                "reason": f"{media} not found on disk: {media_path}"}
    context = {**(shot_context or {}), "shot_meta": shot_meta, "roster": roster}
    budget_used_up = False
    try:
        rows = compare_headcount(roster, checked_answer(await counter.count_cast(media_path, context), roster),
                                 confidence_threshold)
    except Exception as e:  # noqa: BLE001 - a counter that cannot answer is a person's call
        rows = [_row(None, "headcount", NEEDS_REVIEW, _unanswered(e))]
        budget_used_up = isinstance(e, JudgeBudgetExhausted)
    status = worst([r["status"] for r in rows])
    reasons = [r["reason"] for r in rows if r["status"] != PASS]
    out = {"status": status, "checks": rows, "expected": expected,
           "reason": "; ".join(reasons) or "headcount matches the cast"}
    if budget_used_up:
        out["judge_budget_used_up"] = True
    return out
