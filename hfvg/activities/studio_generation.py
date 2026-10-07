"""
Studio generation activities with full enforcement.

Enforcement checklist (fail closed):
1. Episode must be in live mode (stored in episodes table)
2. G1.08 credit plan must be approved (gate status in episodes table)
3. Budget reserve must succeed before submitting (BudgetLedger)
4. Idempotency-Key on every provider call
5. Cost estimate before submitting
6. Activity-level enforcement (not just API-level)

Default mode: DRY_RUN (fake providers)
Real providers only when all checks pass.
"""

import os
import asyncio
import uuid
import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from temporalio import activity

from hfvg.budget import BudgetLedger
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider


@activity.defn
async def check_live_mode_and_g108(episode_id: str) -> tuple[bool, bool]:
    """
    Check if episode is in live mode and G1.08 is approved.
    
    Reads DATABASE_PATH from environment (studio DB).
    
    Args:
        episode_id: Episode identifier
    
    Returns:
        (live_mode, g108_approved)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, False
            return bool(row[0]), bool(row[1])


def generate_idempotency_key(episode_id: str = None, shot_id: str = None, version: int = None, prompt: str = None, **kwargs) -> str:
    """
    Generate deterministic idempotency key from request parameters.
    
    For backward compatibility, accepts legacy positional args.
    For new calls, pass all parameters as kwargs for full coverage:
    - episode_id, shot_id, version, prompt (base)
    - refs, quality, resolution (for stills)
    - start_image, duration (for clips)
    
    Returns: 32-character hex hash
    """
    import json
    
    # Build key data from all parameters
    if episode_id is not None:
        # Legacy positional args provided
        key_data = {
            'episode_id': episode_id,
            'shot_id': shot_id,
            'version': version,
            'prompt': prompt,
        }
        # Also include any kwargs
        key_data.update(kwargs)
    else:
        # New style: all kwargs
        key_data = kwargs
    
    # Sort keys for deterministic ordering
    key_str = json.dumps(key_data, sort_keys=True)
    hash_bytes = hashlib.sha256(key_str.encode()).digest()
    return hash_bytes.hex()[:32]


@activity.defn
async def submit_still_job_enforced(
    episode_id: str,
    shot_id: str,
    prompt: str,
    version: int = 1,
    refs: list[str] | None = None,
    resolution: str = "1k",
    quality: str = "medium",
) -> dict[str, Any]:
    """
    Submit still generation job with full enforcement.
    
    Enforcement:
    - Episode in live mode (fail if not)
    - G1.08 approved (fail if not)
    - Budget reserve succeeds (fail if at stop)
    - Idempotency-Key (deterministic)
    - Cost estimate before submit
    
    Returns:
        dict with job_id, line_name, reserved_amount for polling
    
    Raises:
        ValueError: If enforcement checks fail
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        job_id = f"still-dry-{uuid.uuid4().hex[:12]}"
        activity.logger.info(
            f"[DRY-RUN] Submitted still {job_id} for {episode_id}/{shot_id} (DRY_RUN=true)"
        )
        return {
            "job_id": job_id,
            "line_name": "L2_drafts",
            "reserved_amount": 2.5,
        }
    
    # Check DB for live mode and G1.08 (DRY_RUN=false, so check requirements)
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    if not live_mode:
        raise ValueError(
            f"Episode {episode_id} not in live mode. "
            "Switch to live mode before generating."
        )
    
    if not g108_approved:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve credit plan before generating."
        )
    
    # Live mode: all checks passed (DRY_RUN=false, live_mode=true, g108_approved=true)
    activity.logger.info(
        f"[LIVE MODE] Submitting paid still generation for {episode_id}/{shot_id}"
    )
    
    # Generate idempotency key including all parameters that affect generation
    idempotency_key = generate_idempotency_key(
        episode_id=episode_id,
        shot_id=shot_id,
        version=version,
        prompt=prompt,
        refs=refs,
        quality=quality,
        resolution=resolution,
    )
    
    # Initialize provider
    provider = HiggsfieldStillProvider()
    
    try:
        # Estimate cost
        num_refs = len(refs) if refs else 0
        estimated_cost = await provider.estimate_cost(
            prompt=prompt,
            resolution=resolution,
            quality=quality,
            num_refs=num_refs,
        )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id}: {estimated_cost} credits"
        )
        
        # Determine budget line based on quality
        if quality == "high" and resolution == "2k":
            line_name = "L3_final_stills"
        elif quality == "high":
            line_name = "L1_refs"
        else:
            line_name = "L2_drafts"
        
        # Reserve budget
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount=estimated_cost,
            reason=f"Still {shot_id} v{version}"
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {estimated_cost} credits."
            )
        
        activity.logger.info(
            f"Reserved {estimated_cost} credits from {line_name} for {shot_id}"
        )
        
        # Submit to provider - wrapped to release reservation on ANY error
        try:
            job_id = await provider.submit_image(
                prompt=prompt,
                resolution=resolution,
                quality=quality,
                image_urls=refs,
                idempotency_key=idempotency_key,
            )
            
            activity.logger.info(
                f"[LIVE] Submitted still {job_id} for {episode_id}/{shot_id} "
                f"(cost: {estimated_cost}, line: {line_name})"
            )
            
            # Return dict with job info for polling
            return {
                "job_id": job_id,
                "line_name": line_name,
                "reserved_amount": estimated_cost,
            }
        
        except Exception as submit_error:
            # Release reservation on submission failure
            activity.logger.error(
                f"Submit failed, releasing {estimated_cost} from {line_name}: {submit_error}"
            )
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount=estimated_cost,
                reason=f"Submit failed: {str(submit_error)[:100]}"
            )
            raise
        
    finally:
        await provider.close()


@activity.defn
async def submit_clip_job_enforced(
    episode_id: str,
    shot_id: str,
    start_image_url: str,
    prompt: str,
    duration: float = 5.0,
    version: int = 1,
) -> dict[str, Any]:
    """
    Submit Kling 3.0 Pro video generation job with full enforcement.
    
    Enforcement:
    - Episode in live mode (fail if not)
    - G1.08 approved (fail if not)
    - Budget reserve succeeds (fail if at stop)
    - Idempotency-Key (deterministic)
    - Cost estimate before submit
    
    Returns:
        dict with job_id, line_name, reserved_amount for polling
    
    Raises:
        ValueError: If enforcement checks fail
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Check DB for live mode and G1.08
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    # Decision logic: DRY_RUN env can force dry, but never force live
    # Only go live if: DRY_RUN=false AND DB live_mode=true AND DB g108_approved=true
    use_live = not dry_run_env and live_mode and g108_approved
    
    if not use_live:
        # Dry run mode: use fake provider
        job_id = f"clip-dry-{uuid.uuid4().hex[:12]}"
        reason = []
        if dry_run_env:
            reason.append("DRY_RUN=true")
        if not live_mode:
            reason.append("DB live_mode=false")
        if not g108_approved:
            reason.append("DB g108_approved=false")
        
        activity.logger.info(
            f"[DRY-RUN] Submitted clip {job_id} for {episode_id}/{shot_id} "
            f"({', '.join(reason)})"
        )
        # Return dict with job info for polling
        return {
            "job_id": job_id,
            "line_name": "L4_video",
            "reserved_amount": duration * 1.5,
        }
    
    # Live mode: all checks passed (DRY_RUN=false, live_mode=true, g108_approved=true)
    activity.logger.info(
        f"[LIVE MODE] Submitting paid clip generation for {episode_id}/{shot_id}"
    )
    
    # Double-check: these should never happen due to use_live logic, but fail-safe
    if not live_mode:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve credit plan before generating."
        )
    
    # Generate idempotency key including all parameters that affect generation
    idempotency_key = generate_idempotency_key(
        episode_id=episode_id,
        shot_id=shot_id,
        version=version,
        prompt=f"clip:{prompt}",
        start_image=start_image_url,
        duration=duration,
    )
    
    # Initialize provider
    provider = KlingVideoProvider()
    
    try:
        # Estimate cost (Kling 3.0 Pro: via API with full request params)
        estimated_cost = await provider.estimate_cost(
            image_url=start_image_url,
            prompt=prompt,
            duration=int(duration)
        )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id} clip: {estimated_cost} credits ({duration}s)"
        )
        
        # Reserve budget from L4_video
        line_name = "L4_video"
        
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount=estimated_cost,
            reason=f"Clip {shot_id} v{version} ({duration}s)"
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {estimated_cost} credits."
            )
        
        activity.logger.info(
            f"Reserved {estimated_cost} credits from {line_name} for {shot_id}"
        )
        
        # Validate start_image URL
        if not start_image_url or not start_image_url.startswith(("http://", "https://", "/")):
            # Release reservation on validation failure
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount=estimated_cost,
                reason=f"Invalid start_image: {start_image_url}"
            )
            raise ValueError(f"Invalid start_image URL: {start_image_url}")
        
        # Submit to provider - wrapped to release reservation on ANY error
        try:
            job_id = await provider.submit_video(
                image_url=start_image_url,
                prompt=prompt,
                duration=int(duration),  # Must be integer
                idempotency_key=idempotency_key,
            )
            
            activity.logger.info(
                f"[LIVE] Submitted Kling clip {job_id} for {episode_id}/{shot_id} "
                f"(cost: {estimated_cost}, {duration}s)"
            )
            
            # Return dict with job info for polling
            return {
                "job_id": job_id,
                "line_name": line_name,
                "reserved_amount": estimated_cost,
            }
        
        except Exception as submit_error:
            # Release reservation on submission failure
            activity.logger.error(
                f"Clip submit failed, releasing {estimated_cost} from {line_name}: {submit_error}"
            )
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount=estimated_cost,
                reason=f"Clip submit failed: {str(submit_error)[:100]}"
            )
            raise
        
    finally:
        await provider.close()


@activity.defn
async def await_job_enforced(
    job_id: str,
    job_type: str,
    episode_id: str,
    shot_id: str,
    line_name: str,
    reserved_amount: float,
) -> dict[str, Any]:
    """
    Poll generation job and commit/release budget based on result.
    
    Args:
        job_id: Provider job ID
        job_type: "still" or "clip"
        episode_id: Episode ID
        shot_id: Shot ID
        line_name: Budget line that reserved
        reserved_amount: Amount reserved
    
    Returns:
        dict with status, url, cost
    
    Raises:
        ValueError: If job fails
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(0.2)
        result = {
            "status": "completed",
            "url": f"/fake/{job_type}.{'jpg' if job_type == 'still' else 'mp4'}",
            "cost": reserved_amount,
        }
        activity.logger.info(
            f"[DRY-RUN] Job {job_id} completed: {result['url']} (DRY_RUN=true)"
        )
        return result
    
    # Check DB for live mode and G1.08 (DRY_RUN=false means we need these)
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    if not live_mode:
        raise ValueError(
            f"Episode {episode_id} not in live mode. "
            "Switch to live mode before polling."
        )
    
    if not g108_approved:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve before polling."
        )
    
    # Live mode: poll provider
    if job_type == "still":
        provider = HiggsfieldStillProvider()
    else:
        provider = KlingVideoProvider()
    
    released = False  # Track if we've already released to prevent double release
    
    try:
        # Poll with heartbeats
        max_polls = 100
        poll_interval = 5.0
        retries_5xx = 0
        max_retries_5xx = 3
        
        for i in range(max_polls):
            activity.heartbeat({"job_id": job_id, "poll": i})
            
            try:
                job_status = await provider.get_job_status(job_id)
                retries_5xx = 0  # Reset counter on success
            except httpx.HTTPStatusError as http_err:
                # Retry on 5xx errors instead of releasing (job may still be running)
                if http_err.response.status_code >= 500 and retries_5xx < max_retries_5xx:
                    retries_5xx += 1
                    activity.logger.warning(
                        f"5xx error polling {job_id} (attempt {retries_5xx}/{max_retries_5xx}): {http_err}. Retrying..."
                    )
                    await asyncio.sleep(poll_interval)  # Wait before retry
                    continue
                # Non-5xx or max retries reached: release and raise
                activity.logger.error(
                    f"Max retries reached or non-5xx error for {job_id}: {http_err}"
                )
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount=reserved_amount,
                    reason=f"{job_type} {shot_id} poll error: {http_err}"
                )
                released = True
                raise
            
            if job_status.status.value == "completed":
                # Success - commit budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                actual_cost = job_status.cost or reserved_amount
                
                await ledger.commit(
                    episode_id=episode_id,
                    line_name=line_name,
                    reserved_amount=reserved_amount,
                    actual_cost=actual_cost,
                    usd_micros=None,
                    job_id=job_id,
                    reason=f"{job_type} {shot_id} completed"
                )
                
                activity.logger.info(
                    f"Committed {actual_cost} credits to {line_name} for {shot_id}"
                )
                
                return {
                    "status": "completed",
                    "url": job_status.output_url,
                    "cost": actual_cost,
                }
            
            elif job_status.status.value in ("failed", "blocked"):
                # Failed - release budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount=reserved_amount,
                    reason=f"{job_type} {shot_id} {job_status.status.value}"
                )
                released = True  # Mark as released
                
                activity.logger.error(
                    f"Job {job_id} {job_status.status.value}: {job_status.error}"
                )
                
                raise ValueError(
                    f"Generation {job_status.status.value}: {job_status.error}"
                )
            
            # Still processing
            await asyncio.sleep(poll_interval)
        
        # Timeout - release budget
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        await ledger.release(
            episode_id=episode_id,
            line_name=line_name,
            amount=reserved_amount,
            reason=f"{job_type} {shot_id} timeout"
        )
        released = True  # Mark as released
        
        raise ValueError(f"Job {job_id} timed out after {max_polls} polls")
    
    except asyncio.CancelledError:
        # Activity cancelled: release reservation if not already released
        if not released:
            activity.logger.warning(f"Activity cancelled for job {job_id}, releasing reservation")
            ledger = BudgetLedger(db_path)
            await ledger.init_db()
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount=reserved_amount,
                reason=f"{job_type} {job_id} cancelled"
            )
        if 'provider' in locals():
            await provider.close()
        raise
    
    except Exception as poll_error:
        # Poll exception: release reservation if not already released
        if not released:
            activity.logger.error(f"Poll failed for job {job_id}: {poll_error}")
            ledger = BudgetLedger(db_path)
            await ledger.init_db()
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount=reserved_amount,
                reason=f"{job_type} {job_id} poll error: {str(poll_error)[:100]}"
            )
        if 'provider' in locals():
            await provider.close()
        raise
        
    finally:
        if 'provider' in locals():
            await provider.close()
