"""Headcount QC (judge b, docs/QC-JUDGES.md): who is in frame, figure by figure, against the shot plan.

``shot_roster`` turns a shot's plan into the figures it declares:

- one figure per ``shot_meta.cast`` entry, a character of the cast library of any kind (a person, a
  creature, a robot, a puppet, a prop that acts), expected once;
- ``shot_meta.count`` is the number of main characters in frame: those without a cast entry form
  one group, counted together;
- one figure per shot flag the show declares a figure for, outside ``count`` (show profile
  ``flag_figures``, through ``register_flag_figures``): a figure that is not one of the shot's main
  characters, counted alone.

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
from hfvg.show_profile import ShowProfileError, flag_value

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


# (shot flag, figure) pairs: the figure a shot adds to its roster when it sets the flag
FlagFigures = tuple[tuple[str, Figure], ...]


def register_flag_figures(profile: Any) -> FlagFigures:
    """The figures a show's shot flags declare outside ``count`` (show profile ``flag_figures``), as
    the (flag, figure) pairs ``shot_roster`` reads. A show without flag figures has none."""
    return tuple((f.flag, Figure(f.figure_id, f.name, description=f.description)) for f in profile.flag_figures)


def show_flag_figures(episode_id: str | None = None) -> FlagFigures:
    """The flag figures of the episode's show profile (hfvg.show_profile.resolve_show_profile; a
    show.json that does not load raises ShowProfileError); without an episode, the legacy adapter's."""
    if not episode_id:
        from hfvg.compat import legacy_show
        return register_flag_figures(legacy_show.series_profile())
    from hfvg.show_profile import resolve_show_profile
    return register_flag_figures(resolve_show_profile(episode_id))


def flag_on(shot_meta: dict[str, Any] | None, flag: str) -> bool:
    return bool(flag_value(shot_meta, flag)[1])


def _flagged(meta: dict[str, Any], named: int, count: int | None,
             flag_figures: FlagFigures) -> tuple[tuple[Figure, ...], int]:
    """(figures the shot's flags add to the roster, how many cast entries they account for): a flag
    declares its figure outside ``count``; a cast list longer than ``count`` already lists it."""
    extra: list[Figure] = []
    covered = 0
    for flag, figure in flag_figures:
        if not flag_on(meta, flag):
            continue
        if count is not None and named - covered > count:
            covered += 1
        else:
            extra.append(figure)
    return tuple(extra), covered


def flag_figures_in_cast(roster: Roster, shot_meta: dict[str, Any] | None,
                         flag_figures: FlagFigures) -> list[Figure]:
    """The flag figures of the shot that are one of its cast entries, not figures of their own (a cast
    list longer than ``count`` lists them)."""
    meta = shot_meta or {}
    count = meta.get("count")
    if not (isinstance(count, int) and len(cast_figures(meta)) > count):
        return []
    return [figure for flag, figure in flag_figures if roster.figure(figure.id) is None and flag_on(meta, flag)]


def totals_answer(roster: Roster, shot_meta: dict[str, Any] | None, flag_figures: FlagFigures, *, cast: int,
                  flagged: int, unexpected: int, matches: bool | None, confidence: float,
                  method: str) -> dict[str, Any]:
    """Three totals as an answer per figure: the main characters (the flag figures excepted), the flag
    figures, every other figure. Reviewer rows recorded before per-figure counts (hfvg.qc.g2_headcount)
    and counters that ask those three questions answer this way."""
    flag_ids = {figure.id for _flag, figure in flag_figures}
    shown = [figure for _flag, figure in flag_figures if roster.figure(figure.id) is not None]
    cast_ids = [f.id for f in roster.figures if f.id not in flag_ids]
    out: dict[str, Any] = {"figures": {}, "together": [], "unexpected": unexpected, "background": 0,
                           "confidence": confidence, "method": method}
    in_cast = [] if shown else flag_figures_in_cast(roster, shot_meta, flag_figures)
    if len(shown) == 1:
        out["figures"][shown[0].id] = {"count": flagged, "matches": matches}
    elif shown:
        out["together"].append({"ids": [f.id for f in shown], "count": flagged, "label": "flag figures",
                                "matches": matches})
    elif in_cast:
        cast += min(flagged, len(in_cast))
        out["unexpected"] += max(flagged - len(in_cast), 0)
    else:
        out["unexpected"] += flagged
    if cast_ids:
        out["together"].append({"ids": cast_ids, "count": cast, "label": "cast", "matches": matches})
    else:
        out["unexpected"] += cast
    return out


def shot_roster(shot_meta: dict[str, Any] | None, flag_figures: FlagFigures | None = None) -> Roster | None:
    """The figures the shot declares; None when it declares neither a count nor a cast.
    ``flag_figures``: the show's (``register_flag_figures``; None: ``show_flag_figures()``)."""
    meta = shot_meta if isinstance(shot_meta, dict) else {}
    named = cast_figures(meta)
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        count = None
    if count is None and not named:
        return None
    if flag_figures is None:
        flag_figures = show_flag_figures()
    extra, covered = _flagged(meta, len(named), count, flag_figures)
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

        ``shot_context`` also carries ``shot_meta``, ``episode_id``, ``shot_id``, ``flag_figures`` (the
        show's, the roster was built with) and, for a clip, ``window`` (its trim window). The answer::

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
                          shot_context: dict[str, Any] | None = None, *, media: str = "media",
                          flag_figures: FlagFigures | None = None) -> dict[str, Any]:
    """Count the shot's declared figures in one still or clip. Never raises: a counter that cannot
    answer, or a show profile that does not load, is NEEDS_REVIEW for this shot. ``flag_figures``:
    the show's (None: those of ``shot_context["episode_id"]``'s show, ``show_flag_figures``)."""
    if flag_figures is None:
        try:
            flag_figures = show_flag_figures((shot_context or {}).get("episode_id"))
        except ShowProfileError as e:
            return {"status": NEEDS_REVIEW, "checks": [], "expected": None,
                    "reason": f"the show profile does not load: {str(e)[:200]}"}
    roster = shot_roster(shot_meta, flag_figures)
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
    context = {**(shot_context or {}), "shot_meta": shot_meta, "roster": roster, "flag_figures": flag_figures}
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
