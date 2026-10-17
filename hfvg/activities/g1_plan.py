"""G1 credit plan activity: estimate credits and USD for all shots."""

from temporalio import activity

from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider, SeedanceVideoProvider


@activity.defn
async def compute_g1_plan_activity(episode_id: str, shots: list[dict], dry_run: bool) -> dict:
    """
    Compute per-shot and total credits and USD for the G1 plan.
    
    Uses provider /estimate endpoints to get costs for each shot's still and clip.
    
    Args:
        episode_id: Episode ID
        shots: List of shot plans from beatmap
        dry_run: If True, use dry-run mode estimates
        
    Returns:
        dict with:
            - shots: list of {shot_id, still_credits, still_usd, clip_credits, clip_usd, total_credits, total_usd}
            - total_still_credits: sum of all still credits
            - total_clip_credits: sum of all clip credits
            - total_credits: sum of all credits
            - total_still_usd: sum of all still USD
            - total_clip_usd: sum of all clip USD
            - total_usd: sum of all USD
    """
    shot_estimates = []
    
    still_provider = HiggsfieldStillProvider(dry_run=dry_run)
    
    try:
        for shot in shots:
            shot_id = shot["shot_id"]
            prompt = shot.get("prompt", "")
            params = shot.get("params", {})
            duration = params.get("duration", 5.0)
            model = params.get("model", "kling")
            
            # Estimate still cost
            try:
                still_usd_micros = await still_provider.estimate_usd_micros(
                    prompt=prompt,
                    resolution="1k",
                    quality="medium",
                    aspect_ratio=params.get("aspect_ratio", "9:16"),
                )
                # Convert usd_micros to credits (1 credit = 1 USD for Higgsfield)
                still_credits = still_usd_micros / 1_000_000
                still_usd = still_usd_micros / 1_000_000
            except Exception as e:
                activity.logger.warning(f"Still estimate failed for {shot_id}: {e}, using default")
                still_credits = 0.06  # Default estimate
                still_usd = 0.06
            
            # Estimate clip cost based on model
            if model == "kling":
                clip_provider = KlingVideoProvider(dry_run=dry_run)
            elif model == "seedance":
                clip_provider = SeedanceVideoProvider(dry_run=dry_run)
            else:
                clip_provider = KlingVideoProvider(dry_run=dry_run)  # Default
            
            try:
                clip_usd_micros = await clip_provider.estimate_usd_micros(
                    image_url="https://example.com/placeholder.jpg",  # Placeholder, not used for estimate
                    prompt=prompt,
                    duration=duration,
                )
                clip_credits = clip_usd_micros / 1_000_000
                clip_usd = clip_usd_micros / 1_000_000
            except Exception as e:
                activity.logger.warning(f"Clip estimate failed for {shot_id}: {e}, using default")
                clip_credits = duration * 0.05  # Default: 0.05 credits per second
                clip_usd = duration * 0.05
            finally:
                await clip_provider.close()
            
            shot_estimates.append({
                "shot_id": shot_id,
                "still_credits": round(still_credits, 2),
                "still_usd": f"{still_usd:.2f}",
                "clip_credits": round(clip_credits, 2),
                "clip_usd": f"{clip_usd:.2f}",
                "total_credits": round(still_credits + clip_credits, 2),
                "total_usd": f"{still_usd + clip_usd:.2f}",
            })
    
    finally:
        await still_provider.close()
    
    # Calculate totals
    total_still_credits = sum(s["still_credits"] for s in shot_estimates)
    total_clip_credits = sum(s["clip_credits"] for s in shot_estimates)
    total_credits = total_still_credits + total_clip_credits
    total_still_usd = sum(float(s["still_usd"]) for s in shot_estimates)
    total_clip_usd = sum(float(s["clip_usd"]) for s in shot_estimates)
    total_usd = total_still_usd + total_clip_usd
    
    return {
        "shots": shot_estimates,
        "total_still_credits": round(total_still_credits, 2),
        "total_clip_credits": round(total_clip_credits, 2),
        "total_credits": round(total_credits, 2),
        "total_still_usd": f"{total_still_usd:.2f}",
        "total_clip_usd": f"{total_clip_usd:.2f}",
        "total_usd": f"{total_usd:.2f}",
    }
