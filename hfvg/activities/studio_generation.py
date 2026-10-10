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

from hfvg.budget import BudgetLedger, REVISION_LINE
from hfvg.pricing import (
    DRY_CLIP_USD_PER_SECOND_MICROS,
    DRY_STILL_USD_MICROS,
    micros_to_usd_str,
)
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
    aspect_ratio: str = "9:16",
    tier: str | None = None,
) -> dict[str, Any]:
    """
    Submit still generation job with full enforcement.
    
    Enforcement:
    - Episode in live mode (fail if not)
    - G1.08 approved (fail if not)
    - Budget reserve succeeds (fail if at stop)
    - Idempotency-Key (deterministic)
    - Cost estimate (USD) before submit; GC.01 manual balance required
    
    Args:
        tier: draft/final/retry/revision for the per-job ledger. Defaults: draft on L2,
            final on L1/L3. tier="revision" draws on the revision reserve (revision tag).
    
    Returns:
        dict with job_id, line_name, reserved_amount (int usd_micros) for polling
    
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
            "reserved_amount": DRY_STILL_USD_MICROS,
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
        aspect_ratio=aspect_ratio,
    )
    
    # Initialize provider
    provider = HiggsfieldStillProvider()
    
    try:
        # Estimate cost in integer micro-dollars (provider estimate's USD value)
        estimated_cost = await provider.estimate_usd_micros(
            prompt=prompt,
            resolution=resolution,
            quality=quality,
            aspect_ratio=aspect_ratio,
        )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id}: {micros_to_usd_str(estimated_cost)} USD"
        )
        
        # Determine budget line based on quality
        if quality == "high" and resolution == "2k":
            line_name = "L3_final_stills"
            default_tier = "final"
        elif quality == "high":
            line_name = "L1_refs"
            default_tier = "final"
        else:
            line_name = "L2_drafts"
            default_tier = "draft"
        tier = tier or default_tier
        revision = tier == "revision"
        if revision:
            line_name = REVISION_LINE
        
        # Reserve budget
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount_usd_micros=estimated_cost,
            reason=f"Still {shot_id} v{version}",
            revision=revision,
            require_balance=True,
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {micros_to_usd_str(estimated_cost)} USD."
            )
        
        activity.logger.info(
            f"Reserved {micros_to_usd_str(estimated_cost)} USD from {line_name} for {shot_id}"
        )
        
        # Submit to provider - wrapped to release reservation on ANY error
        try:
            job_id = await provider.submit_image(
                prompt=prompt,
                resolution=resolution,
                quality=quality,
                image_urls=refs,
                idempotency_key=idempotency_key,
                aspect_ratio=aspect_ratio,
            )
            
            activity.logger.info(
                f"[LIVE] Submitted still {job_id} for {episode_id}/{shot_id} "
                f"(cost: {micros_to_usd_str(estimated_cost)} USD, line: {line_name})"
            )
            try:
                await ledger.record_job(
                    episode_id, job_id, shot_id, provider.model_path, tier, line_name,
                    estimated_cost,
                )
            except aiosqlite.Error as ledger_err:
                # The provider already accepted (and will charge) this job: NEVER release the hold
                # because the bookkeeping row failed. The reservation stays held.
                activity.logger.error(
                    f"Job {job_id} submitted but per-job ledger row failed: {ledger_err}. "
                    f"Reservation HELD."
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
                f"Submit failed, releasing {micros_to_usd_str(estimated_cost)} USD from {line_name}: {submit_error}"
            )
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=estimated_cost,
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
    tier: str | None = None,
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
            "reserved_amount": int(round(duration * DRY_CLIP_USD_PER_SECOND_MICROS)),
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
        # Estimate cost in micro-dollars (Kling 3.0 Pro: estimate's USD, else rate table)
        estimated_cost = await provider.estimate_usd_micros(
            image_url=start_image_url,
            prompt=prompt,
            duration=int(duration)
        )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id} clip: "
            f"{micros_to_usd_str(estimated_cost)} USD ({duration}s)"
        )
        
        # Reserve budget from L4_video (revision tier draws on the revision reserve)
        tier = tier or "final"
        revision = tier == "revision"
        line_name = REVISION_LINE if revision else "L4_video"
        
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount_usd_micros=estimated_cost,
            reason=f"Clip {shot_id} v{version} ({duration}s)",
            revision=revision,
            require_balance=True,
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {micros_to_usd_str(estimated_cost)} USD."
            )
        
        activity.logger.info(
            f"Reserved {micros_to_usd_str(estimated_cost)} USD from {line_name} for {shot_id}"
        )
        
        # Validate start_image URL
        if not start_image_url or not start_image_url.startswith(("http://", "https://", "/")):
            # Release reservation on validation failure
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=estimated_cost,
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
                f"(cost: {micros_to_usd_str(estimated_cost)} USD, {duration}s)"
            )
            try:
                await ledger.record_job(
                    episode_id, job_id, shot_id, provider.model_path, tier, line_name,
                    estimated_cost,
                )
            except aiosqlite.Error as ledger_err:
                # The provider already accepted (and will charge) this job: NEVER release the hold
                # because the bookkeeping row failed. The reservation stays held.
                activity.logger.error(
                    f"Job {job_id} submitted but per-job ledger row failed: {ledger_err}. "
                    f"Reservation HELD."
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
                f"Clip submit failed, releasing {micros_to_usd_str(estimated_cost)} USD from {line_name}: {submit_error}"
            )
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=estimated_cost,
                reason=f"Clip submit failed: {str(submit_error)[:100]}"
            )
            raise
        
    finally:
        await provider.close()


@activity.defn
async def poll_job_status(
    job_id: str,
    job_type: str,
    episode_id: str,
) -> dict[str, Any]:
    """
    Poll job status once (short activity, ≤60s).
    
    This activity is called repeatedly from the workflow with workflow.sleep between calls.
    It does NOT release reservations on timeout - the workflow handles reconciliation.
    
    Args:
        job_id: Provider job ID
        job_type: "still" or "clip"
        episode_id: Episode ID
    
    Returns:
        dict with status (str), output_url (str|None), error (str|None), cost (float|None)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(0.1)
        return {
            "status": "completed",
            "output_url": f"/fake/{job_type}.{'jpg' if job_type == 'still' else 'mp4'}",
            "error": None,
            "cost": None,
        }
    
    # Check DB for live mode
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
    
    try:
        job_status = await provider.get_job_status(job_id)
        
        # Return status without taking any budget actions
        # The workflow will handle commit/release based on terminal status
        return {
            "status": job_status.status.value,  # "completed", "failed", "blocked", "canceled", "in_progress", "queued"
            "output_url": job_status.output_url if job_status.status.value == "completed" else None,
            "error": job_status.error if job_status.status.value in ("failed", "blocked") else None,
            # Provider status carries credits (not USD); the ledger commits the held estimate
            "cost": None,
        }
    finally:
        await provider.close()


@activity.defn
async def commit_job_budget(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
    line_name: str,
    reserved_amount: int,
    actual_cost: int | None = None,
) -> None:
    """
    Commit reserved budget after successful job completion.
    
    In dry mode, this is a no-op since no reservation was made.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
        line_name: Budget line
        reserved_amount: Amount reserved
        actual_cost: Actual cost in usd_micros (defaults to reserved_amount)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # In dry mode, submit never reserved, so commit is a no-op
    if dry_run_env:
        activity.logger.info(
            f"[DRY-RUN] Skipping commit for {job_type} {shot_id} (no reservation was made)"
        )
        return
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    cost = actual_cost if actual_cost is not None else reserved_amount
    
    await ledger.commit(
        episode_id=episode_id,
        line_name=line_name,
        reserved_usd_micros=reserved_amount,
        actual_usd_micros=cost,
        job_id=job_id,
        reason=f"{job_type} {shot_id} completed"
    )
    
    activity.logger.info(
        f"Committed {micros_to_usd_str(cost)} USD to {line_name} for {shot_id}"
    )


@activity.defn
async def release_job_budget(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
    line_name: str,
    reserved_amount: int,
    reason: str,
) -> None:
    """
    Release reserved budget after confirmed job failure.
    
    Only called for confirmed terminal failures (failed, blocked, canceled).
    NOT called on timeout or poll exhaustion - those keep the reservation.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
        line_name: Budget line
        reserved_amount: Amount reserved
        reason: Release reason
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    await ledger.release(
        episode_id=episode_id,
        line_name=line_name,
        amount_usd_micros=reserved_amount,
        reason=f"{job_type} {shot_id} {reason}",
        job_id=job_id,
    )
    
    activity.logger.info(
        f"Released {micros_to_usd_str(reserved_amount)} USD from {line_name} for {shot_id}: {reason}"
    )


@activity.defn
async def mark_job_pending_reconcile(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
) -> None:
    """
    Mark job as pending_reconcile in DB after poll timeout/exhaustion.
    
    The reservation remains held. Manual intervention required.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
    # Per-job ledger: flag the job (reservation stays held)
    await BudgetLedger(db_path).mark_job_status(job_id, "pending_reconcile")
    
    # Record in audit log
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "job_pending_reconcile",
             f"{job_type} {shot_id} job {job_id} exhausted polling window, reservation held",
             "system")
        )
        await db.commit()
    
    activity.logger.error(
        f"Job {job_id} marked pending_reconcile. Reservation held - manual intervention required."
    )


@activity.defn
async def await_job_enforced(
    job_id: str,
    job_type: str,
    episode_id: str,
    shot_id: str,
    line_name: str,
    reserved_amount: int,
) -> dict[str, Any]:
    """
    Poll generation job and commit/release budget based on result.
    
    DEPRECATED: This activity is being phased out in favor of workflow-based polling
    with poll_job_status(). It remains for backward compatibility with existing workflows.
    
    New workflows should use the workflow polling loop pattern instead.
    
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
        # Phase 1: Active polling (5s intervals, up to 500s ~100 polls)
        max_active_polls = 100
        active_poll_interval = 5.0
        retries_5xx = 0
        max_retries_5xx = 3
        
        for i in range(max_active_polls):
            activity.heartbeat({"job_id": job_id, "poll": i, "phase": "active"})
            
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
                    await asyncio.sleep(active_poll_interval)  # Wait before retry
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
                    amount_usd_micros=reserved_amount,
                    reason=f"{job_type} {shot_id} poll error: {http_err}"
                )
                released = True
                raise
            
            if job_status.status.value == "completed":
                # Success - commit budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                actual_cost = reserved_amount  # status carries credits, not USD: commit the hold
                
                await ledger.commit(
                    episode_id=episode_id,
                    line_name=line_name,
                    reserved_usd_micros=reserved_amount,
                    actual_usd_micros=actual_cost,
                    job_id=job_id,
                    reason=f"{job_type} {shot_id} completed"
                )
                
                activity.logger.info(
                    f"Committed {micros_to_usd_str(actual_cost)} USD to {line_name} for {shot_id}"
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
                    amount_usd_micros=reserved_amount,
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
            await asyncio.sleep(active_poll_interval)
        
        # Phase 2: Reconciliation polling (longer intervals, up to 30 min total)
        # DO NOT release on timeout - keep polling until we get a terminal status
        activity.logger.warning(
            f"Job {job_id} exceeded active polling window (~{max_active_polls * active_poll_interval}s). "
            f"Entering reconciliation phase with longer backoff (reservation held)."
        )
        
        max_reconciliation_polls = 180  # ~30 min at 10s intervals
        reconciliation_interval = 10.0
        
        for i in range(max_reconciliation_polls):
            activity.heartbeat({"job_id": job_id, "poll": i, "phase": "reconciliation"})
            
            try:
                job_status = await provider.get_job_status(job_id)
            except httpx.HTTPStatusError as http_err:
                # In reconciliation phase, log but keep polling
                activity.logger.warning(
                    f"Poll error in reconciliation phase for {job_id}: {http_err}. Continuing..."
                )
                await asyncio.sleep(reconciliation_interval)
                continue
            
            if job_status.status.value == "completed":
                # Success - commit budget with actual cost
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                actual_cost = reserved_amount  # status carries credits, not USD: commit the hold
                
                await ledger.commit(
                    episode_id=episode_id,
                    line_name=line_name,
                    reserved_usd_micros=reserved_amount,
                    actual_usd_micros=actual_cost,
                    job_id=job_id,
                    reason=f"{job_type} {shot_id} completed (reconciled)"
                )
                
                activity.logger.info(
                    f"[RECONCILED] Committed {micros_to_usd_str(actual_cost)} USD to {line_name} for {shot_id}"
                )
                
                return {
                    "status": "completed",
                    "url": job_status.output_url,
                    "cost": actual_cost,
                }
            
            elif job_status.status.value in ("failed", "blocked", "canceled"):
                # Confirmed terminal failure - release budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount_usd_micros=reserved_amount,
                    reason=f"{job_type} {shot_id} {job_status.status.value} (reconciled)"
                )
                released = True
                
                activity.logger.error(
                    f"[RECONCILED] Job {job_id} {job_status.status.value}: {job_status.error}"
                )
                
                raise ValueError(
                    f"Generation {job_status.status.value}: {job_status.error}"
                )
            
            # Still processing - continue reconciliation
            await asyncio.sleep(reconciliation_interval)
        
        # Final timeout after reconciliation phase - reservation STAYS HELD
        # This is a safety net; manual intervention required to release
        activity.logger.error(
            f"Job {job_id} exhausted reconciliation window (total ~{max_active_polls * active_poll_interval + max_reconciliation_polls * reconciliation_interval}s). "
            f"Reservation for {micros_to_usd_str(reserved_amount)} USD on {line_name} remains HELD. "
            f"Manual reconciliation required via provider dashboard."
        )
        
        raise ValueError(
            f"Job {job_id} exhausted polling window. Reservation held - manual reconciliation required."
        )

    
    except asyncio.CancelledError:
        # Activity cancelled: release reservation if not already released
        if not released:
            activity.logger.warning(f"Activity cancelled for job {job_id}, releasing reservation")
            ledger = BudgetLedger(db_path)
            await ledger.init_db()
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=reserved_amount,
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
                amount_usd_micros=reserved_amount,
                reason=f"{job_type} {job_id} poll error: {str(poll_error)[:100]}"
            )
        if 'provider' in locals():
            await provider.close()
        raise
        
    finally:
        if 'provider' in locals():
            await provider.close()
