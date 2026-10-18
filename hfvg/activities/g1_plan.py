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
        dry_run: If True, return placeholder estimates without real API calls
        
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
    
    # In dry run, return placeholder estimates without real API calls
    if dry_run:
        for shot in shots:
            shot_estimates.append({
                "shot_id": shot["shot_id"],
                "still_credits": 1.0,
                "still_usd": "0.05",
                "still_estimate_available": True,
                "clip_credits": 5.0,
                "clip_usd": "0.25",
                "clip_estimate_available": True,
                "total_credits": 6.0,
                "total_usd": "0.30",
            })
        
        return {
            "shots": shot_estimates,
            "total_still_credits": float(len(shots)),
            "total_clip_credits": float(len(shots) * 5),
            "total_credits": float(len(shots) * 6),
            "total_still_usd": f"{len(shots) * 0.05:.2f}",
            "total_clip_usd": f"{len(shots) * 0.25:.2f}",
            "total_usd": f"{len(shots) * 0.30:.2f}",
            "estimates_available": True,
        }
    
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
            still_usd = 0.0
            try:
                # Estimate start frame still
                usd_micros, credits = await still_provider.estimate_usd_and_credits(
                    prompt=prompt,
                    resolution=resolution,
                    quality=quality,
                    aspect_ratio=aspect_ratio,
                )
                still_credits = credits
                still_usd = usd_micros / 1_000_000
                
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
                    still_usd += (end_usd_micros / 1_000_000)
                    
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
            clip_usd = 0.0
            try:
                if model == "seedance":
                    # Seedance doesn't use image URLs for estimates
                    usd_micros, credits = await clip_provider.estimate_usd_and_credits(
                        duration=int(duration),
                        resolution=resolution,
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
                clip_usd = usd_micros / 1_000_000
            except Exception as e:
                activity.logger.warning(f"Clip estimate failed for {shot_id}: {e}")
                clip_estimate_available = False
            finally:
                await clip_provider.close()
            
            shot_estimates.append({
                "shot_id": shot_id,
                "still_credits": round(still_credits, 2),
                "still_usd": f"{still_usd:.2f}",
                "still_estimate_available": still_estimate_available,
                "clip_credits": round(clip_credits, 2),
                "clip_usd": f"{clip_usd:.2f}",
                "clip_estimate_available": clip_estimate_available,
                "total_credits": round(still_credits + clip_credits, 2),
                "total_usd": f"{still_usd + clip_usd:.2f}",
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
        "estimates_available": not any_unavailable,  # False if any estimate failed
        "dry_run": dry_run,  # Pass through for workflow validation
    }
