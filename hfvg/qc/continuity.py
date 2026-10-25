"""Continuity checks on the mute picture edit: which are HARD (blocking) vs ADVISORY per episode.

Check names (``CONTINUITY_CHECKS``):

- ``headcount``           animal count (shot_meta.count) + duck presence (shot_meta.duck) through the
                          AnimalCounter interface (hfvg.qc.animal_count)
- ``relative_size``       the characters' relative size order stays consistent across shots
- ``left_to_right_order`` the characters' left-to-right order stays consistent across shots

An episode selects its hard checks with the top-level ``continuity_hard_checks`` list in shots.json
(e.g. ``["relative_size", "headcount"]``); every other check is advisory. Without the key the
default is ``DEFAULT_HARD_CHECKS`` (headcount only: the behaviour before the key existed).

A hard check that FAILs makes the assembly verdict FAIL; a hard check nothing could verify (no checker
configured, low confidence) is NEEDS_REVIEW (fail closed, never a silent pass). An advisory check
never blocks: its FAIL / unverified result is reported for the reviewer (verdict WATCH at worst).

The size / order checks are model-dependent and live behind ``ContinuityChecker``. No real checker
ships in this package, so in production they are unverified: NEEDS_REVIEW when hard, reported as
advisory otherwise. ``FakeContinuityChecker`` is a deterministic test double.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"

HEADCOUNT = "headcount"
RELATIVE_SIZE = "relative_size"
LEFT_TO_RIGHT_ORDER = "left_to_right_order"
CONTINUITY_CHECKS = (HEADCOUNT, RELATIVE_SIZE, LEFT_TO_RIGHT_ORDER)
DEFAULT_HARD_CHECKS = (HEADCOUNT,)
# Checks answered by a ContinuityChecker (headcount uses the AnimalCounter instead)
CHECKER_CHECKS = (RELATIVE_SIZE, LEFT_TO_RIGHT_ORDER)
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


class ContinuityError(ValueError):
    """Invalid continuity configuration."""


def validate_hard_checks(value: Any) -> list[str]:
    """Validate a ``continuity_hard_checks`` list (known names, no duplicates)."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ContinuityError(f"continuity_hard_checks must be a list of check names, got {value!r}")
    unknown = sorted(set(value) - set(CONTINUITY_CHECKS))
    if unknown:
        raise ContinuityError(
            f"continuity_hard_checks: unknown check(s) {unknown}; known: {list(CONTINUITY_CHECKS)}"
        )
    if len(set(value)) != len(value):
        raise ContinuityError(f"continuity_hard_checks: duplicate entries in {value}")
    return list(value)


def continuity_policy(hard_checks: list[str] | None) -> dict[str, list[str]]:
    """{"hard": [...], "advisory": [...]} in CONTINUITY_CHECKS order. None -> the default."""
    hard = set(validate_hard_checks(list(hard_checks)) if hard_checks is not None else DEFAULT_HARD_CHECKS)
    return {
        "hard": [c for c in CONTINUITY_CHECKS if c in hard],
        "advisory": [c for c in CONTINUITY_CHECKS if c not in hard],
        "source": "episode" if hard_checks is not None else "default",
    }


class ContinuityChecker(ABC):
    """Answers one continuity check for one shot's clip."""

    @abstractmethod
    async def check(self, media_path: str | Path, check: str, shot_context: dict[str, Any]) -> dict[str, Any]:
        """Return {"ok": bool, "confidence": float 0-1, "detail": str}."""


class FakeContinuityChecker(ContinuityChecker):
    """Deterministic checker for tests: {(shot_id, check): ok} or {check: ok}; unknown -> ok."""

    def __init__(self, results: dict | None = None, confidence: float = 1.0):
        self.results = dict(results or {})
        self.confidence = confidence
        self.calls: list[tuple[str, str]] = []

    async def check(self, media_path, check, shot_context):
        sid = str(shot_context.get("shot_id"))
        self.calls.append((sid, check))
        ok = self.results.get((sid, check), self.results.get(check, True))
        return {"ok": bool(ok), "confidence": self.confidence, "detail": "fake_deterministic"}


_CHECKER: ContinuityChecker | None = None


def set_continuity_checker(checker: ContinuityChecker | None) -> None:
    global _CHECKER
    _CHECKER = checker


def get_continuity_checker() -> ContinuityChecker | None:
    return _CHECKER


async def run_checker_check(checker: ContinuityChecker | None, media_path: str | Path, check: str,
                            shot_context: dict[str, Any],
                            confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD) -> dict[str, Any]:
    """One size/order check -> {"check", "status", "reason"}; never PASS without a checker."""
    if checker is None:
        return {"check": check, "status": NEEDS_REVIEW,
                "reason": f"no continuity checker configured: a human must verify {check}"}
    if not Path(media_path).exists():
        return {"check": check, "status": NEEDS_REVIEW, "reason": f"media not found: {media_path}"}
    try:
        res = await checker.check(media_path, check, shot_context)
        conf = float(res.get("confidence", 0.0))
    except Exception as e:  # a checker error is never a pass
        return {"check": check, "status": NEEDS_REVIEW, "reason": f"{check}: checker error: {str(e)[:200]}"}
    if conf < confidence_threshold:
        return {"check": check, "status": NEEDS_REVIEW,
                "reason": f"{check}: low confidence {conf:.2f} < {confidence_threshold}"}
    if not res.get("ok"):
        return {"check": check, "status": FAIL, "reason": f"{check}: {res.get('detail') or 'inconsistent'}"}
    return {"check": check, "status": PASS, "reason": f"{check}: consistent"}


def classify(results: list[dict[str, Any]], policy: dict[str, list[str]]) -> dict[str, Any]:
    """Split per-shot check results into hard vs advisory.

    results: [{"shot_id", "check", "status", "reason"}]. Returns the rows tagged ``hard`` plus
    hard_fail (a hard check failed), hard_review (a hard check unverified: fail closed) and
    advisory (an advisory check failed / unverified). The assembly QC activity turns these into
    FAIL > NEEDS_REVIEW > WATCH together with the motion and headcount results."""
    hard = set(policy["hard"])
    rows = [{**r, "hard": r["check"] in hard} for r in results]
    hard_fail = [r for r in rows if r["hard"] and r["status"] == FAIL]
    hard_review = [r for r in rows if r["hard"] and r["status"] == NEEDS_REVIEW]
    advisory = [r for r in rows if not r["hard"] and r["status"] != PASS]
    return {"results": rows, "hard_fail": hard_fail, "hard_review": hard_review, "advisory": advisory}
