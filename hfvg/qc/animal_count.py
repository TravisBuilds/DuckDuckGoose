"""The headcount interfaces of the first series, kept for its data and code (compatibility).

The pipeline counts the figures a shot declares (hfvg.qc.headcount). What the first series stored,
and the code written for it, still work through these adapters:

- ``legacy_figures``: its host, declared by shots.json's ``duck_present`` (``shot_meta.duck``)
  outside ``count``, is one more figure of the roster (id ``_duck_host``, counted alone);
- ``legacy_row_answer``: a reviewer headcount recorded as cast / duck / other (the rows of
  ``g2_headcounts.json`` written before per-figure counts) in the general answer shape;
- ``AnimalCounter``: the species interface (``count_animals(media, expected_type, shot_context)``).
  Its ``count_cast`` asks the questions the older checks asked and answers in the general shape, so
  ``FakeAnimalCounter`` and any counter written for it work with every general check;
- ``expected_cast``, ``DuckPresenceCheck``, ``check_shot_animals``, ``create_duck_check`` and the
  registry names ``set_animal_counter`` / ``get_animal_counter`` (the general registry).

As before, a check never passes when nothing looked at the pixels: no counter, a counter error or
counts that do not add up are NEEDS_REVIEW.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import Any

from hfvg.qc.headcount import (
    DEFAULT_CONFIDENCE_THRESHOLD,
    FAIL,
    NEEDS_REVIEW,
    PASS,
    CastCounter,
    Figure,
    Roster,
    cast_figures,
    check_headcount,
    get_cast_counter,
    set_cast_counter,
)
from hfvg.qc.judges import count_of, is_video

__all__ = ["DEFAULT_CONFIDENCE_THRESHOLD", "FAIL", "NEEDS_REVIEW", "PASS", "LEGACY_HOST", "AnimalCounter",
           "FakeAnimalCounter", "DuckPresenceCheck", "check_shot_animals", "create_duck_check", "expected_cast",
           "get_animal_counter", "legacy_figures", "legacy_row_answer", "set_animal_counter"]

LEGACY_HOST = Figure("_duck_host", "duck", description="the series' duck host, not one of the shot's main characters")


def legacy_figures(shot_meta: dict[str, Any], named: int, count: int | None) -> tuple[tuple[Figure, ...], int]:
    """(figures to add to the roster, how many cast entries they account for). ``duck_present``
    declares the host outside ``count``; a cast list longer than ``count`` already lists it."""
    if not shot_meta.get("duck"):
        return (), 0
    if count is not None and named > count:
        return (), 1
    return (LEGACY_HOST,), 0


def _host_in_cast(roster: Roster, shot_meta: dict[str, Any] | None) -> bool:
    """True when the host of a ``duck_present`` shot is one of its cast entries (not its own figure)."""
    meta = shot_meta or {}
    count = meta.get("count")
    return (roster.figure(LEGACY_HOST.id) is None and bool(meta.get("duck"))
            and isinstance(count, int) and len(cast_figures(meta)) > count)


def _general(roster: Roster, shot_meta: dict[str, Any] | None, *, cast: int, ducks: int, unexpected: int,
             matches: bool | None, confidence: float, method: str) -> dict[str, Any]:
    """Counts of the old questions (cast without the host, ducks, every other figure) per figure."""
    host = roster.figure(LEGACY_HOST.id) is not None
    cast_ids = [f.id for f in roster.figures if f.id != LEGACY_HOST.id]
    out: dict[str, Any] = {"figures": {}, "together": [], "unexpected": unexpected, "background": 0,
                           "confidence": confidence, "method": method}
    if host:
        out["figures"][LEGACY_HOST.id] = {"count": ducks, "matches": matches}
    elif _host_in_cast(roster, shot_meta):
        cast += min(ducks, 1)
        out["unexpected"] += max(ducks - 1, 0)
    else:
        out["unexpected"] += ducks
    if cast_ids:
        out["together"].append({"ids": cast_ids, "count": cast, "label": "cast", "matches": matches})
    else:
        out["unexpected"] += cast
    return out


def legacy_row_answer(row: dict[str, Any], roster: Roster, shot_meta: dict[str, Any] | None) -> dict[str, Any]:
    """A reviewer row recorded as {"cast", "duck", "other"} in the general answer shape."""
    return _general(roster, shot_meta, cast=count_of(row.get("cast")), ducks=count_of(row.get("duck")),
                    unexpected=count_of(row.get("other", 0)), matches=True, confidence=1.0, method="")


class AnimalCounter(CastCounter):
    """A counter written for the species questions: ``cast`` (the shot's main characters, the host
    excepted), ``duck`` and ``animal`` (every figure in frame)."""

    @abstractmethod
    async def count_animals(self, media_path: str | Path, expected_type: str = "duck",
                            shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return {"count": int, "confidence": float 0-1, "type_match": bool, "method": str}."""

    async def count_cast(self, media_path: str | Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        """The G2 questions for a still (cast, duck, animal), the assembly questions for a clip (every
        figure but the host, then the host); a missing type match is a figure a person checks."""
        roster: Roster = shot_context["roster"]
        meta = shot_context.get("shot_meta")
        host = roster.figure(LEGACY_HOST.id) is not None or _host_in_cast(roster, meta)
        if is_video(media_path):
            asked = {"animal": await self.count_animals(media_path, "animal",
                                                        shot_context={**shot_context, "exclude_duck_host": host})}
            if host:
                asked["duck"] = await self.count_animals(media_path, "duck", shot_context=shot_context)
            cast, ducks, unexpected = count_of(asked["animal"].get("count")), 0, 0
            if host:
                ducks = count_of(asked["duck"].get("count"))
        else:
            asked = {t: await self.count_animals(media_path, t, shot_context=shot_context)
                     for t in ("cast", "duck", "animal")}
            cast, ducks, every = (count_of(asked[t].get("count")) for t in ("cast", "duck", "animal"))
            unexpected = every - cast - ducks
            if unexpected < 0:
                raise ValueError(f"the counts do not add up: {every} in frame but {cast} cast and {ducks} ducks")
        confidence = min(float(a.get("confidence", 0.0)) for a in asked.values())
        matches = all(a.get("type_match", False) is True for a in asked.values())
        methods = sorted({str(a.get("method") or "") for a in asked.values()} - {""})
        return _general(roster, meta, cast=cast, ducks=ducks, unexpected=unexpected, matches=matches,
                        confidence=confidence, method=", ".join(methods))


class FakeAnimalCounter(AnimalCounter):
    """Deterministic counter for tests: returns the configured counts, nothing else.

    counts: {expected_type: int} or {(file name, expected_type): int} (file-specific wins)."""

    def __init__(self, counts: dict | None = None, confidence: float = 1.0, type_match: bool = True):
        self.counts = dict(counts or {})
        self.confidence = confidence
        self.type_match = type_match
        self.calls: list[tuple[str, str]] = []
        self.contexts: list[dict[str, Any] | None] = []

    async def count_animals(self, media_path: str | Path, expected_type: str = "duck",
                            shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        name = Path(media_path).name
        self.calls.append((name, expected_type))
        self.contexts.append(shot_context)
        count = self.counts.get((name, expected_type), self.counts.get(expected_type, 0))
        return {
            "found": count > 0,
            "count": int(count),
            "confidence": self.confidence,
            "type_match": self.type_match,
            "method": "fake_deterministic",
        }


def set_animal_counter(counter: CastCounter | None) -> None:
    set_cast_counter(counter)


def get_animal_counter() -> CastCounter | None:
    return get_cast_counter()


def expected_cast(shot_meta: dict | None) -> dict[str, int] | None:
    """{"cast", "duck", "animal"} expected in frame, or None when the shot declares no count."""
    meta = shot_meta or {}
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    duck = 1 if meta.get("duck") else 0
    return {"cast": count, "duck": duck, "animal": count + duck}


def _no_checker(what: str) -> dict[str, Any]:
    return {
        "status": NEEDS_REVIEW,
        "passed": False,
        "escalate": True,
        "reason": f"no animal checker configured: a human must verify {what}",
        "details": {},
    }


class DuckPresenceCheck:
    """Duck presence and count for one still or clip. counter=None -> escalate (never pass)."""

    def __init__(self, counter: AnimalCounter | None, confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD):
        self.counter = counter
        self.confidence_threshold = confidence_threshold

    async def _check(self, media_path: str | Path, expected_type: str, expected_count: int, label: str,
                     shot_context: dict[str, Any] | None = None) -> dict:
        if self.counter is None:
            return _no_checker(f"{label} (expected {expected_count})")
        if not Path(media_path).exists():
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"media not found: {media_path}", "details": {}}
        try:
            result = await self.counter.count_animals(media_path, expected_type, shot_context=shot_context)
            confidence = float(result.get("confidence", 0.0))
            int(result.get("count", -1))
        except Exception as e:  # noqa: BLE001 - a counter that cannot answer is a person's call
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"{label}: counter error: {str(e)[:200]}", "details": {}}
        if confidence < self.confidence_threshold:
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"{label}: low confidence {confidence:.2f} < {self.confidence_threshold}",
                    "details": result}
        if not result.get("type_match", False):
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"{label}: counted, but may not be the declared character", "details": result}
        if int(result.get("count", -1)) != expected_count:
            return {"status": FAIL, "passed": False, "escalate": False,
                    "reason": f"{label}: expected {expected_count}, got {result.get('count')}",
                    "details": result}
        return {"status": PASS, "passed": True, "escalate": False,
                "reason": f"{label}: {expected_count} confirmed", "details": result}

    async def check_still(self, image_path: str | Path, expected_count: int = 1,
                          shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._check(image_path, "duck", expected_count, "duck presence", shot_context)

    async def check_clip(self, video_path: str | Path, expected_count: int = 1,
                         shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._check(video_path, "duck", expected_count, "duck presence", shot_context)


async def check_shot_animals(counter: CastCounter | None, media_path: str | Path,
                             shot_meta: dict[str, Any] | None,
                             confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
                             shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Animal count (shot_meta.count) and duck presence (shot_meta.duck) for one shot's clip, the
    two checks of the first series. A general counter gets the general headcount instead.

    Returns {"status": PASS|FAIL|NEEDS_REVIEW, "checks": [...], "reason": str}. Worst status wins
    (FAIL > NEEDS_REVIEW > PASS). No counter, or a counter error -> NEEDS_REVIEW, never PASS."""
    if counter is not None and not isinstance(counter, AnimalCounter):
        return await check_headcount(counter, media_path, shot_meta, confidence_threshold, shot_context)
    meta = shot_meta or {}
    checker = DuckPresenceCheck(counter, confidence_threshold)
    checks: list[dict[str, Any]] = []
    context = {"shot_meta": meta, **(shot_context or {})}
    count = meta.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        count_context = {**context, "exclude_duck_host": bool(meta.get("duck"))}
        res = await checker._check(media_path, "animal", count, "animal count", count_context)
        checks.append({"check": "animal_count", **res})
    else:
        checks.append({"check": "animal_count", "status": NEEDS_REVIEW, "passed": False, "escalate": True,
                       "reason": "shot has no animal count to verify against", "details": {}})
    if meta.get("duck"):
        res = await checker._check(media_path, "duck", 1, "duck presence", context)
        checks.append({"check": "duck_presence", **res})
    statuses = {c["status"] for c in checks}
    status = FAIL if FAIL in statuses else (NEEDS_REVIEW if NEEDS_REVIEW in statuses else PASS)
    reason = "; ".join(c["reason"] for c in checks if c["status"] != PASS) or "all animal checks passed"
    return {"status": status, "checks": checks, "reason": reason}


def create_duck_check(counter: AnimalCounter | None = None,
                      confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> DuckPresenceCheck:
    """Duck check using ``counter`` or the registered counter. With neither it escalates."""
    if counter is None:
        registered = get_cast_counter()
        counter = registered if isinstance(registered, AnimalCounter) else None
    return DuckPresenceCheck(counter, confidence_threshold)
