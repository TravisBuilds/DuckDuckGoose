"""Per-shot clip QC constraints (``qc_fail_if`` in shots.json), checked on every generated clip.

A shot may declare free-text failure conditions, e.g.::

    "qc_fail_if": ["prop ends in water", "prop vanishes", "host touches a guest"]

Every clip of that shot (first attempt, moderation resubmit, clip-only retry) is checked against
the list by a ``ClipConstraintChecker`` (vision / judge model, behind this interface). The result is
recorded per clip (``clip_qc["constraints"]``) with one row per declared constraint.

Fail closed: no checker configured, a missing clip file, a checker error or a low-confidence answer
is NEEDS_REVIEW (the clip is escalated to a human), never a silent pass. A violated constraint is a
FAIL. No real checker ships in this package. DRY_RUN placeholder clips are not footage: their rows
are recorded as DRY_RUN ("not checked"), which is neither a pass nor a block.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

PASS = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"
DRY_RUN = "DRY_RUN"
NONE_DECLARED = "NONE_DECLARED"
DEFAULT_CONFIDENCE_THRESHOLD = 0.7


class ConstraintError(ValueError):
    """Invalid qc_fail_if declaration."""


def validate_constraints(value: Any, where: str = "qc_fail_if") -> list[str]:
    """A list of non-empty strings (stripped, duplicates refused)."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConstraintError(f"{where} must be a list of strings, got {value!r}")
    out = [v.strip() for v in value]
    if any(not v for v in out):
        raise ConstraintError(f"{where}: empty constraint")
    if len(set(out)) != len(out):
        raise ConstraintError(f"{where}: duplicate constraint in {out}")
    return out


class ClipConstraintChecker(ABC):
    """Judges whether a clip shows a declared failure condition."""

    @abstractmethod
    async def check(self, media_path: str | Path, constraint: str, shot_context: dict[str, Any]) -> dict[str, Any]:
        """Return {"violated": bool, "confidence": float 0-1, "detail": str}."""


class FakeClipConstraintChecker(ClipConstraintChecker):
    """Deterministic checker for tests: the constraints in ``violated`` are reported violated."""

    def __init__(self, violated: set[str] | None = None, confidence: float = 1.0):
        self.violated = set(violated or ())
        self.confidence = confidence
        self.calls: list[tuple[str, str]] = []

    async def check(self, media_path, constraint, shot_context):
        self.calls.append((Path(media_path).name, constraint))
        return {"violated": constraint in self.violated, "confidence": self.confidence,
                "detail": "fake_deterministic"}


_CHECKER: ClipConstraintChecker | None = None


def set_constraint_checker(checker: ClipConstraintChecker | None) -> None:
    global _CHECKER
    _CHECKER = checker


def get_constraint_checker() -> ClipConstraintChecker | None:
    return _CHECKER


def _summary(status: str, rows: list[dict[str, Any]], reason: str) -> dict[str, Any]:
    return {"status": status, "results": rows, "reason": reason,
            "declared": [r["constraint"] for r in rows]}


async def evaluate_clip_constraints(
    checker: ClipConstraintChecker | None,
    media_path: str | Path | None,
    constraints: list[str] | None,
    shot_context: dict[str, Any] | None = None,
    *,
    dry_run: bool = False,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """Check one clip against the shot's declared constraints.

    Returns {"status": PASS|FAIL|NEEDS_REVIEW|DRY_RUN|NONE_DECLARED, "results": [per constraint],
    "reason", "declared"}. Worst wins: FAIL > NEEDS_REVIEW > PASS."""
    constraints = list(constraints or [])
    ctx = dict(shot_context or {})
    if not constraints:
        return _summary(NONE_DECLARED, [], "no qc_fail_if constraints declared")
    if dry_run:
        rows = [{"constraint": c, "status": DRY_RUN, "reason": "dry run: placeholder clip, not checked"}
                for c in constraints]
        return _summary(DRY_RUN, rows, "dry run: constraints recorded, not checked (live checks every clip)")
    rows: list[dict[str, Any]] = []
    for c in constraints:
        if checker is None:
            rows.append({"constraint": c, "status": NEEDS_REVIEW,
                         "reason": "no clip constraint checker configured: a human must verify"})
            continue
        if not media_path or not Path(media_path).exists():
            rows.append({"constraint": c, "status": NEEDS_REVIEW, "reason": f"clip file not found: {media_path}"})
            continue
        try:
            res = await checker.check(media_path, c, ctx)
        except Exception as e:  # a checker error is never a pass
            rows.append({"constraint": c, "status": NEEDS_REVIEW, "reason": f"checker error: {str(e)[:200]}"})
            continue
        conf = float(res.get("confidence", 0.0))
        if conf < confidence_threshold:
            rows.append({"constraint": c, "status": NEEDS_REVIEW,
                         "reason": f"low confidence {conf:.2f} < {confidence_threshold}"})
        elif res.get("violated"):
            rows.append({"constraint": c, "status": FAIL, "reason": res.get("detail") or "violated"})
        else:
            rows.append({"constraint": c, "status": PASS, "reason": res.get("detail") or "not observed"})
    statuses = {r["status"] for r in rows}
    if FAIL in statuses:
        status = FAIL
    elif NEEDS_REVIEW in statuses:
        status = NEEDS_REVIEW
    else:
        status = PASS
    reason = "; ".join(f"{r['constraint']}: {r['status']} ({r['reason']})" for r in rows if r["status"] != PASS) \
        or "no declared failure condition observed"
    return _summary(status, rows, reason)
