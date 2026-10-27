"""Explicit per-shot model routing (fail closed).

Every shot carries explicit routing fields: ``model`` in {kling, seedance} and ``tier`` in
{draft, final}. The planner NEVER sends a shot to Seedance when it is tagged ``wet`` or its
duration is under 4 s, and refuses (RoutingError) on anything missing or unknown.

Seedance resolution by tier: draft -> 480p, final -> 1080p. Seedance "final" is a FRESH 1080p
submit: finalizing from a draft job id is unverified on REST and deliberately not implemented.
"""

import os
from dataclasses import dataclass

MODELS = ("kling", "seedance")
TIERS = ("draft", "final")
SEEDANCE_MIN_DURATION_S = 4
SEEDANCE_TIER_RESOLUTION = {"draft": "480p", "final": "1080p"}
SEEDANCE_BLOCKED_TAGS = frozenset({"wet"})


SEEDANCE_OVERRIDE_ENV = "HFVG_SEEDANCE_OVERRIDE_SHOTS"


def seedance_override_ids() -> frozenset[str]:
    """Owner-approved per-shot Seedance exceptions (env HFVG_SEEDANCE_OVERRIDE_SHOTS, comma list).

    A listed shot id may route to Seedance even when it is multi-animal or tagged ``wet``
    (owner-approved head-to-head test). Unset/empty = the rules apply unchanged.
    The duration and whole-seconds checks are never overridden.
    """
    raw = os.getenv(SEEDANCE_OVERRIDE_ENV, "")
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


class RoutingError(ValueError):
    """The shot cannot be routed as requested (fail closed)."""


@dataclass(frozen=True)
class ShotRoute:
    model: str
    tier: str
    resolution: str | None  # Seedance only; Kling is native 1080p


def plan_route(shot: dict) -> ShotRoute:
    """Validate and plan the route for one shot dict.

    Required keys: ``model``, ``tier``, ``duration`` (seconds), ``count`` (number of animals).
    Optional: ``tags`` (list[str]), ``shot_id`` (str; only used for the env-gated Seedance
    override, see :func:`seedance_override_ids`).
    
    Multi-animal rule: shots with count > 1 MUST use Kling with first-frame only (never Seedance reference mode).
    """
    model = shot.get("model")
    tier = shot.get("tier")
    count = shot.get("count", 1)
    
    if model not in MODELS:
        raise RoutingError(f"Shot routing requires model in {MODELS}, got {model!r}")
    if tier not in TIERS:
        raise RoutingError(f"Shot routing requires tier in {TIERS}, got {tier!r}")
    duration = shot.get("duration")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        raise RoutingError(f"Shot routing requires a positive duration, got {duration!r}")
    tags = {str(t).lower() for t in (shot.get("tags") or [])}

    shot_id = shot.get("shot_id")
    override = model == "seedance" and isinstance(shot_id, str) and shot_id in seedance_override_ids()

    # Multi-animal enforcement: force Kling for count > 1 (unless the shot is owner-overridden)
    if count > 1 and model == "seedance" and not override:
        raise RoutingError(
            f"Multi-animal shots (count={count}) must use Kling first-frame, not Seedance reference mode"
        )

    if model == "seedance":
        blocked = sorted(tags & SEEDANCE_BLOCKED_TAGS)
        if blocked and not override:
            raise RoutingError(f"Shots tagged {blocked} must not be routed to Seedance (use Kling)")
        if duration < SEEDANCE_MIN_DURATION_S:
            raise RoutingError(
                f"Duration {duration}s is under {SEEDANCE_MIN_DURATION_S}s: not routable to Seedance (use Kling)"
            )
        return ShotRoute("seedance", tier, SEEDANCE_TIER_RESOLUTION[tier])
    return ShotRoute("kling", tier, None)
