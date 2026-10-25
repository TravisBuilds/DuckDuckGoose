"""Animal count QC: duck presence and animal-count verification behind an interface.

Model-dependent detection lives behind ``AnimalCounter``. There is NO real counter in this package,
so production has none configured, and every check then ESCALATES to human review
(``NEEDS_REVIEW``). A check never passes silently when nothing actually looked at the pixels.

``FakeAnimalCounter`` is a deterministic implementation for tests only: it returns the counts it was
constructed with and is never used unless a caller passes or registers it explicitly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"

DEFAULT_CONFIDENCE_THRESHOLD = 0.7


class AnimalCounter(ABC):
    """Counts animals in an image or video file."""

    @abstractmethod
    async def count_animals(self, media_path: str | Path, expected_type: str = "duck") -> dict[str, Any]:
        """Return {"count": int, "confidence": float 0-1, "type_match": bool, "method": str}.

        ``expected_type`` is a species ("duck") or "animal" for every animal in frame."""


class FakeAnimalCounter(AnimalCounter):
    """Deterministic counter for tests: returns the configured counts, nothing else.

    counts: {expected_type: int} or {(file name, expected_type): int} (file-specific wins)."""

    def __init__(self, counts: dict | None = None, confidence: float = 1.0, type_match: bool = True):
        self.counts = dict(counts or {})
        self.confidence = confidence
        self.type_match = type_match
        self.calls: list[tuple[str, str]] = []

    async def count_animals(self, media_path: str | Path, expected_type: str = "duck") -> dict[str, Any]:
        name = Path(media_path).name
        self.calls.append((name, expected_type))
        count = self.counts.get((name, expected_type), self.counts.get(expected_type, 0))
        return {
            "found": count > 0,
            "count": int(count),
            "confidence": self.confidence,
            "type_match": self.type_match,
            "method": "fake_deterministic",
        }


# Process-wide counter used by the assembly QC activity. None in production (no real detector is
# shipped): checks escalate. Tests register a FakeAnimalCounter with set_animal_counter().
_COUNTER: AnimalCounter | None = None


def set_animal_counter(counter: AnimalCounter | None) -> None:
    global _COUNTER
    _COUNTER = counter


def get_animal_counter() -> AnimalCounter | None:
    return _COUNTER


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

    async def _check(self, media_path: str | Path, expected_type: str, expected_count: int, label: str) -> dict:
        if self.counter is None:
            return _no_checker(f"{label} (expected {expected_count})")
        if not Path(media_path).exists():
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"media not found: {media_path}", "details": {}}
        result = await self.counter.count_animals(media_path, expected_type)
        confidence = float(result.get("confidence", 0.0))
        if confidence < self.confidence_threshold:
            return {"status": NEEDS_REVIEW, "passed": False, "escalate": True,
                    "reason": f"{label}: low confidence {confidence:.2f} < {self.confidence_threshold}",
                    "details": result}
        if not result.get("type_match", False):
            return {"status": FAIL, "passed": False, "escalate": False,
                    "reason": f"{label}: wrong animal type (expected {expected_type})", "details": result}
        if int(result.get("count", -1)) != expected_count:
            return {"status": FAIL, "passed": False, "escalate": False,
                    "reason": f"{label}: expected {expected_count}, got {result.get('count')}",
                    "details": result}
        return {"status": PASS, "passed": True, "escalate": False,
                "reason": f"{label}: {expected_count} confirmed", "details": result}

    async def check_still(self, image_path: str | Path, expected_count: int = 1,
                          shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._check(image_path, "duck", expected_count, "duck presence")

    async def check_clip(self, video_path: str | Path, expected_count: int = 1,
                         shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._check(video_path, "duck", expected_count, "duck presence")


async def check_shot_animals(counter: AnimalCounter | None, media_path: str | Path,
                             shot_meta: dict[str, Any] | None,
                             confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict[str, Any]:
    """Animal count (shot_meta.count) and duck presence (shot_meta.duck) for one shot's clip.

    Returns {"status": PASS|FAIL|NEEDS_REVIEW, "checks": [...], "reason": str}. Worst status wins
    (FAIL > NEEDS_REVIEW > PASS). No counter -> NEEDS_REVIEW, never PASS."""
    meta = shot_meta or {}
    checker = DuckPresenceCheck(counter, confidence_threshold)
    checks: list[dict[str, Any]] = []
    count = meta.get("count")
    if isinstance(count, int) and not isinstance(count, bool):
        res = await checker._check(media_path, "animal", count, "animal count")
        checks.append({"check": "animal_count", **res})
    else:
        checks.append({"check": "animal_count", "status": NEEDS_REVIEW, "passed": False, "escalate": True,
                       "reason": "shot has no animal count to verify against", "details": {}})
    if meta.get("duck"):
        res = await checker._check(media_path, "duck", 1, "duck presence")
        checks.append({"check": "duck_presence", **res})
    statuses = {c["status"] for c in checks}
    status = FAIL if FAIL in statuses else (NEEDS_REVIEW if NEEDS_REVIEW in statuses else PASS)
    reason = "; ".join(c["reason"] for c in checks if c["status"] != PASS) or "all animal checks passed"
    return {"status": status, "checks": checks, "reason": reason}


def create_duck_check(counter: AnimalCounter | None = None,
                      confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> DuckPresenceCheck:
    """Duck check using ``counter`` or the registered counter. With neither it escalates."""
    return DuckPresenceCheck(counter if counter is not None else get_animal_counter(), confidence_threshold)
