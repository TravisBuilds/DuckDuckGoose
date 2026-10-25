"""G1 credit plan activity: estimate credits and USD for all shots."""

import os
from pathlib import Path

from temporalio import activity

from hfvg.estimates import dry_shot_estimate, seedance_resolution
from hfvg.pre_approved import pre_approved_ids
from hfvg.pricing import micros_to_usd_str
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider, SeedanceVideoProvider
from hfvg.shot_data import picture_seconds


async def _anchors(episode_id: str, shots: list[dict]) -> dict[str, str]:
    """Anchor stills already configured for these shots (so preflight counts them like submit does).
    Never creates a database: no DB or no anchor table means no anchors."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    if not Path(db_path).exists():
        return {}
    from hfvg.studio_db import get_anchor_still
    out = {}
    for shot in shots:
        sid = shot.get("shot_id") or shot.get("id")
        try:
            anchor = await get_anchor_still(db_path, episode_id, sid)
        except Exception:
            return {}
        if anchor:
            out[sid] = anchor["local_path"]
    return out


async def _pre_approved_ids(episode_id: str, shots: list[dict]) -> set[str]:
    """Shots whose start still is pre-approved (shots.json or anchor rows; hfvg.pre_approved)."""
    anchors: list[dict] = []
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    if Path(db_path).exists():
        from hfvg.studio_db import list_anchor_stills
        try:
            anchors = await list_anchor_stills(episode_id)
        except Exception:
            anchors = []
    return pre_approved_ids(shots, anchors)


def _dry_plan(shots: list[dict], pre_approved: set[str], extra: dict) -> dict:
    """G1 plan for a dry run from the per-call estimates the dry submits reserve."""
    rows = []
    total_still = total_clip = 0
    any_unpriced = False
    for shot in shots:
        sid = shot["shot_id"]
        est = dry_shot_estimate(shot, pre_approved=sid in pre_approved)
        clip_micros = est["clip_usd_micros"]
        any_unpriced = any_unpriced or clip_micros is None
        total_still += est["still_usd_micros"]
        total_clip += clip_micros or 0
        rows.append({
            "shot_id": sid,
            "pre_approved": est["pre_approved"],
            "stills": est["stills"],
            "still_credits": round(est["still_credits"], 2),
            "still_usd": micros_to_usd_str(est["still_usd_micros"]),
            "still_usd_micros": est["still_usd_micros"],
            "still_estimate_available": True,
            "clip_credits": round(est["clip_credits"], 2),
            "clip_usd": micros_to_usd_str(clip_micros or 0),
            "clip_usd_micros": clip_micros,
            "clip_estimate_available": clip_micros is not None,
            "total_credits": round(est["still_credits"] + est["clip_credits"], 2),
            "total_usd": micros_to_usd_str(est["still_usd_micros"] + (clip_micros or 0)),
        })
    return {
        "shots": rows,
        "total_still_credits": round(sum(r["still_credits"] for r in rows), 2),
        "total_clip_credits": round(sum(r["clip_credits"] for r in rows), 2),
        "total_credits": round(sum(r["total_credits"] for r in rows), 2),
        "total_still_usd": micros_to_usd_str(total_still),
        "total_clip_usd": micros_to_usd_str(total_clip),
        "total_usd": micros_to_usd_str(total_still + total_clip),
        "total_usd_micros": total_still + total_clip,
        "estimates_available": not any_unpriced,
        "estimate_source": "dry per-call estimates (same values the dry submits reserve)",
        **extra,
    }


def _moderation_preview(shots: list[dict], pre_approved: set[str]) -> dict:
    """B5: what the moderation lint will decide at each paid submit (evaluated as dry here, so a
    hard rule shows "would_refuse" at G1 instead of surprising the run later)."""
    from hfvg.moderation_lint import ModerationLintError, evaluate
    out: dict[str, dict] = {}
    for shot in shots:
        sid = shot["shot_id"]
        params = shot.get("params") or {}
        row: dict = {}
        try:
            if sid not in pre_approved:
                row["still"] = evaluate(sid, shot.get("prompt", ""), shot.get("shot_meta"), kind="still",
                                        refs=shot.get("refs"), live=False)
            row["clip"] = evaluate(sid, shot.get("clip_prompt") or shot.get("prompt", ""), shot.get("shot_meta"),
                                   kind="clip", model=params.get("model", "kling"), tags=shot.get("tags"),
                                   live=False)
        except ModerationLintError as e:  # invalid HFVG_PLAYBOOK_HARD_RULES
            row = {"error": str(e)}
        out[sid] = row
    return out


async def preflight_g1(episode_id: str, shots: list[dict]) -> dict:
    """Preflight every shot (still start/end + clip) against the model caps registry."""
    from hfvg.preflight import preflight_plans
    report = preflight_plans(shots, anchors=await _anchors(episode_id, shots))
    for text in report["errors"]:
        activity.logger.warning(f"[G1 preflight] {text}")
    return report


@activity.defn
async def compute_g1_plan_activity(episode_id: str, shots: list[dict], dry_run: bool) -> dict:
    """
    Compute per-shot and total credits and USD for the G1 plan.
    
    Uses provider /estimate endpoints to get costs for each shot's still and clip.
    
    Args:
        episode_id: Episode ID
        shots: List of shot plans from beatmap
        dry_run: If True, the dry per-call estimates (hfvg.estimates) without any network call
        
    Returns:
        dict with:
            - shots: list of {shot_id, still_credits, still_usd, clip_credits, clip_usd, total_credits, total_usd, still_estimate_available, clip_estimate_available}
            - total_still_credits: sum of all still credits
            - total_clip_credits: sum of all clip credits
            - total_credits: sum of all credits
            - total_still_usd: sum of all still USD
            - total_clip_usd: sum of all clip USD
            - total_usd: sum of all USD
            - estimates_available: False if any estimate failed
    """
    shot_estimates = []

    # Preflight first, in dry and live runs: G1 approval is refused while any shot breaks a model limit
    report = await preflight_g1(episode_id, shots)
    preflight_fields = {
        "preflight": report["shots"],
        "preflight_errors": report["errors"],
        "dry_run": dry_run,
    }
    # Estimates still run (free /estimate calls) so the plan shows costs next to the errors.

    pre_approved = await _pre_approved_ids(episode_id, shots)
    preflight_fields["pre_approved_stills"] = sorted(pre_approved)
    preflight_fields["picture_seconds"] = picture_seconds(shots)
    preflight_fields["moderation_lint"] = _moderation_preview(shots, pre_approved)

    # Dry run: the same per-call estimates the dry submits reserve (hfvg.estimates), no network.
    if dry_run:
        return _dry_plan(shots, pre_approved, preflight_fields)

    # Live mode: make real provider calls
    still_provider = HiggsfieldStillProvider()
    
    try:
        for shot in shots:
            shot_id = shot["shot_id"]
            prompt = shot.get("prompt", "")
            params = shot.get("params", {})
            duration = params.get("duration", 5.0)
            model = params.get("model", "kling")
            resolution = params.get("resolution", "1k")
            quality = params.get("quality", "medium")
            aspect_ratio = params.get("aspect_ratio", "9:16")
            refs = shot.get("refs", [])
            end_frame = params.get("end_frame", False)
            
            # Estimate still cost
            still_estimate_available = True
            still_credits = 0.0
            still_micros = 0
            try:
                # Estimate start frame still (none for a pre-approved start still: no submit)
                if shot_id not in pre_approved:
                    usd_micros, credits = await still_provider.estimate_usd_and_credits(
                        prompt=prompt,
                        resolution=resolution,
                        quality=quality,
                        aspect_ratio=aspect_ratio,
                    )
                    still_credits = credits
                    still_micros = int(usd_micros)
                
                # If end_frame is True, estimate end frame still too
                if end_frame:
                    # Use end_prompt if available, otherwise same as start frame
                    end_prompt = shot.get("end_prompt") or prompt
                    end_usd_micros, end_credits = await still_provider.estimate_usd_and_credits(
                        prompt=end_prompt,
                        resolution=resolution,
                        quality=quality,
                        aspect_ratio=aspect_ratio,
                    )
                    still_credits += end_credits
                    still_micros += int(end_usd_micros)
                    
            except Exception as e:
                activity.logger.warning(f"Still estimate failed for {shot_id}: {e}")
                still_estimate_available = False
            
            # Estimate clip cost based on model
            if model == "kling":
                clip_provider = KlingVideoProvider()
            elif model == "seedance":
                clip_provider = SeedanceVideoProvider()
            else:
                clip_provider = KlingVideoProvider()  # Default
            
            clip_estimate_available = True
            clip_credits = 0.0
            clip_micros = 0
            try:
                if model == "seedance":
                    # Seedance doesn't use image URLs for estimates
                    # the resolution the submit routes to (tier), never the still resolution
                    usd_micros, credits = await clip_provider.estimate_usd_and_credits(
                        duration=int(duration),
                        resolution=seedance_resolution(params.get("tier")),
                    )
                else:
                    # Kling and others require valid image URLs
                    # At G1 time, no stills exist yet; use stable placeholder
                    # (pricing doesn't depend on image content)
                    from hfvg.config import config as cfg
                    placeholder_url = cfg.get_placeholder_image_url()
                    
                    # For end-frame shots, pass placeholder as last_image_url too
                    last_image_url = placeholder_url if end_frame else None
                    
                    usd_micros, credits = await clip_provider.estimate_usd_and_credits(
                        image_url=placeholder_url,
                        prompt=prompt,
                        duration=int(duration),
                        end_image_url=last_image_url,
                    )
                clip_credits = credits
                clip_micros = int(usd_micros)
            except Exception as e:
                activity.logger.warning(f"Clip estimate failed for {shot_id}: {e}")
                clip_estimate_available = False
            finally:
                await clip_provider.close()
            
            shot_estimates.append({
                "shot_id": shot_id,
                "pre_approved": shot_id in pre_approved,
                "still_credits": round(still_credits, 2),
                # the same provider /estimate values the live submit reserves (integer usd micros)
                "still_usd": micros_to_usd_str(still_micros),
                "still_usd_micros": still_micros,
                "still_estimate_available": still_estimate_available,
                "clip_credits": round(clip_credits, 2),
                "clip_usd": micros_to_usd_str(clip_micros),
                "clip_usd_micros": clip_micros,
                "clip_estimate_available": clip_estimate_available,
                "total_credits": round(still_credits + clip_credits, 2),
                "total_usd": micros_to_usd_str(still_micros + clip_micros),
            })
    
    finally:
        await still_provider.close()
    
    # Check if any estimates are unavailable
    any_unavailable = any(
        not s["still_estimate_available"] or not s["clip_estimate_available"]
        for s in shot_estimates
    )
    
    # Calculate totals
    total_still_credits = sum(s["still_credits"] for s in shot_estimates)
    total_clip_credits = sum(s["clip_credits"] for s in shot_estimates)
    total_credits = total_still_credits + total_clip_credits
    total_still = sum(s["still_usd_micros"] for s in shot_estimates)
    total_clip = sum(s["clip_usd_micros"] for s in shot_estimates)
    
    return {
        "shots": shot_estimates,
        "total_still_credits": round(total_still_credits, 2),
        "total_clip_credits": round(total_clip_credits, 2),
        "total_credits": round(total_credits, 2),
        "total_still_usd": micros_to_usd_str(total_still),
        "total_clip_usd": micros_to_usd_str(total_clip),
        "total_usd": micros_to_usd_str(total_still + total_clip),
        "total_usd_micros": total_still + total_clip,
        "estimate_source": "provider /estimate per call (same values the live submits reserve)",
        "estimates_available": not any_unavailable,  # False if any estimate failed
        **preflight_fields,  # preflight, preflight_errors, dry_run (workflow validation)
    }
