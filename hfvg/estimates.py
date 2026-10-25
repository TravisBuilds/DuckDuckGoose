"""Per-call cost estimates shared by the G1 plan, the dry-run reservations and the offline API table.

One function per paid call kind, so the number shown at G1 is the number the submit reserves:

- ``still_call_usd_micros()``: one still submit in a dry run (``DRY_STILL_USD_MICROS``, the same
  constant submit_still_job_enforced reserves in dry mode).
- ``clip_call_usd_micros(model, tier, seconds)``: one clip submit in a dry run (the per-model rate
  table, ``hfvg.pricing.clip_estimate_usd_micros``, the same call submit_clip_job_enforced reserves
  in dry mode). Live runs use the providers' free /estimate calls for both (compute_g1_plan_activity).

``shot_call_plan`` lists the paid calls one shot makes on its first attempt: the start still
(skipped when the start still is pre-approved), the end still (end-frame shots) and the clip.
"""

from __future__ import annotations

from typing import Any

from hfvg.credit_pricing import estimate_clip_credits, estimate_still_credits
from hfvg.pricing import DRY_STILL_USD_MICROS, clip_estimate_usd_micros


def seedance_resolution(tier: str | None) -> str:
    """The resolution a Seedance submit uses for this tier (same as the submit's plan_route)."""
    from hfvg.routing import SEEDANCE_TIER_RESOLUTION
    return SEEDANCE_TIER_RESOLUTION["draft" if tier == "draft" else "final"]


def still_call_usd_micros() -> int:
    """USD micros one dry-run still submit reserves."""
    return DRY_STILL_USD_MICROS


def clip_call_usd_micros(model: str, tier: str | None, seconds: float) -> int:
    """USD micros one dry-run clip submit reserves (raises ValueError for an unknown model)."""
    return clip_estimate_usd_micros(model, tier, seconds)


def shot_call_plan(shot: dict[str, Any], pre_approved: bool = False) -> dict[str, Any]:
    """Paid calls for one shot's first attempt: {"start_still": bool, "end_still": bool, "clip": True,
    "model", "duration", "tier"}. Accepts a workflow shot (params) or a raw shots.json entry."""
    params = shot.get("params") or {}
    end_frame = bool(params.get("end_frame", shot.get("end_frame", False)))
    return {
        "start_still": not pre_approved,
        "end_still": end_frame,
        "clip": True,
        "model": str(params.get("model") or shot.get("model") or "kling"),
        "duration": float(params.get("duration") or shot.get("duration") or 5.0),
        "tier": params.get("tier") or shot.get("tier"),
    }


def dry_shot_estimate(shot: dict[str, Any], pre_approved: bool = False,
                      still_model: str | None = None) -> dict[str, Any]:
    """Dry-run estimate for one shot = the sum of what its dry submits reserve.

    Returns usd micros for stills / clip (clip None when the model has no rate) and credits."""
    plan = shot_call_plan(shot, pre_approved)
    n_stills = int(plan["start_still"]) + int(plan["end_still"])
    still_micros = n_stills * still_call_usd_micros()
    try:
        clip_micros: int | None = clip_call_usd_micros(plan["model"], plan["tier"], plan["duration"])
        clip_error = None
    except (ValueError, TypeError) as e:
        clip_micros, clip_error = None, str(e)
    from hfvg.config import still_model_path
    still_credits = n_stills * estimate_still_credits(still_model or still_model_path())
    clip_credits = estimate_clip_credits(
        {"kling": "kling_3.0", "seedance": "seedance_2.5"}.get(plan["model"], plan["model"]), plan["duration"],
        seedance_resolution(plan["tier"]) if plan["model"] == "seedance" else None,
    )
    return {
        "stills": n_stills,
        "pre_approved": pre_approved,
        "still_usd_micros": still_micros,
        "clip_usd_micros": clip_micros,
        "clip_error": clip_error,
        "still_credits": still_credits,
        "clip_credits": clip_credits,
    }
