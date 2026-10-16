"""Generation activities: stills and clips with submit/await pattern."""

import asyncio
import random
import uuid

from temporalio import activity

from hfvg.config import config
from hfvg.errors import ContentBlockError, RetryableError
from hfvg.ledger import Ledger
from hfvg.models import Asset, GenerationRequest, JobStatus
from hfvg.providers import HiggsfieldProvider, ProviderJobStatus


async def check_live_mode_and_g108(db_path: str, episode_id: str) -> tuple[bool, bool]:
    """
    Check if episode is in live mode and G1.08 is approved.
    
    Returns:
        (live_mode, g108_approved)
    """
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, False
            return bool(row[0]), bool(row[1])


@activity.defn
async def submit_still_job(episode_id: str, req: GenerationRequest) -> str:
    """
    Submit a still generation job with idempotency.
    
    DEPRECATED: Use submit_still_job_enforced instead.
    This legacy activity is gated identically to the enforced version.

    Returns provider job_id. Never submits the same request twice.
    """
    activity.logger.warning(
        "DEPRECATED: submit_still_job is a legacy activity. "
        "Use submit_still_job_enforced instead."
    )
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Check DB for live mode and G1.08
    live_mode, g108_approved = await check_live_mode_and_g108(db_path, episode_id)
    
    # Decision logic: DRY_RUN env can force dry, but never force live
    use_live = not dry_run_env and live_mode and g108_approved
    
    ledger = Ledger()
    await ledger.init_db()

    key = ledger.idempotency_key(req)

    existing = await ledger.get_job(key)
    if existing:
        activity.logger.info(f"Job already exists for key {key[:8]}: {existing['provider_job_id']}")
        return existing["provider_job_id"]

    await ledger.check_balance(req.estimated_cost)

    if not use_live:
        job_id = f"still-{uuid.uuid4().hex[:12]}"
        activity.logger.info(f"[DRY-RUN] Submitted still job {job_id} for {req.shot_id}")
    else:
        # Real Higgsfield API call - GATED by DB live_mode and G1.08
        from hfvg.providers import HiggsfieldStillProvider
        provider = HiggsfieldStillProvider()
        
        # Determine resolution and quality from request
        # Default to draft tier (1k medium) unless specified
        resolution = req.params.get("resolution", "1k")
        quality = req.params.get("quality", "medium")
        
        try:
            job_id = await provider.submit_still(
                prompt=req.prompt,
                resolution=resolution,
                quality=quality,
                refs=req.refs or [],
                idempotency_key=key,
            )
            activity.logger.info(f"[LIVE] Submitted still job {job_id} for {req.shot_id}")
        finally:
            await provider.close()

    await ledger.insert_job(key, job_id, episode_id, req.shot_id, req, "running")
    await ledger.deduct_credits(episode_id, req.estimated_cost, "estimate", job_id)

    return job_id


@activity.defn
async def submit_clip_job(episode_id: str, req: GenerationRequest) -> str:
    """
    Submit a clip generation job with idempotency.
    
    DEPRECATED: Use submit_clip_job_enforced instead.
    This legacy activity is gated identically to the enforced version.

    Returns provider job_id. Never submits the same request twice.
    """
    activity.logger.warning(
        "DEPRECATED: submit_clip_job is a legacy activity. "
        "Use submit_clip_job_enforced instead."
    )
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Check DB for live mode and G1.08
    live_mode, g108_approved = await check_live_mode_and_g108(db_path, episode_id)
    
    # Decision logic: DRY_RUN env can force dry, but never force live
    use_live = not dry_run_env and live_mode and g108_approved
    
    ledger = Ledger()
    await ledger.init_db()

    key = ledger.idempotency_key(req)

    existing = await ledger.get_job(key)
    if existing:
        activity.logger.info(f"Job already exists for key {key[:8]}: {existing['provider_job_id']}")
        return existing["provider_job_id"]

    await ledger.check_balance(req.estimated_cost)

    if not use_live:
        job_id = f"clip-{uuid.uuid4().hex[:12]}"
        activity.logger.info(f"[DRY-RUN] Submitted clip job {job_id} for {req.shot_id}")
    else:
        # Real Higgsfield API call with model routing per PIPELINE-LESSONS
        provider = HiggsfieldProvider(dry_run=False)
        
        # Model routing: respect BEATMAP model column (K/S), then infer from tags
        # Default to Kling if no explicit routing (Ep04 all-Kling)
        model_hint = req.params.get("model")  # From BEATMAP K/S column
        tags = req.params.get("tags", [])
        
        # Wet/pool/underwater tags → Kling (remove 'bare' - undefined)
        wet_tags = {"wet", "mud", "pool", "soak", "underwater", "water"}
        is_wet = any(tag.lower() in wet_tags for tag in tags)
        
        # Route based on BEATMAP model column first, then tags, then default Kling
        if model_hint == "K" or model_hint == "kling_3.0":
            model = "kling_3.0"
            resolution = "1080p"
            draft = False
        elif model_hint == "S" or model_hint == "seedance_2.5":
            model = "seedance_2.5"
            is_draft = req.params.get("tier") == "draft"
            resolution = "480p" if is_draft else "720p"
            draft = is_draft
        elif is_wet:
            # Wet content: Kling 3.0 pro (never blocks)
            model = "kling_3.0"
            resolution = "1080p"
            draft = False
        else:
            # Default: Kling first, Seedance only where it's clearly better
            model = "kling_3.0"
            resolution = "1080p"
            draft = False
        
        # Get start_image from params (uploaded stills)
        start_image = req.params.get("start_image") or req.params.get("still_url") or ""
        
        job_id = await provider.submit_video(
            start_image=start_image,
            prompt=req.prompt,
            model=model,
            duration=req.params.get("duration", 5.0),
            resolution=resolution,
            draft=draft,
            references=req.refs or [],
            idempotency_key=key,
        )
        activity.logger.info(
            f"[API] Submitted {model} clip job {job_id} for {req.shot_id} "
            f"(wet={is_wet}, draft={draft}, res={resolution})"
        )

    await ledger.insert_job(key, job_id, episode_id, req.shot_id, req, "running")
    await ledger.deduct_credits(episode_id, req.estimated_cost, "estimate", job_id)

    return job_id


@activity.defn
async def await_job(job_id: str, job_type: str = "still") -> Asset:
    """
    Poll a generation job until complete, with heartbeats.

    Raises:
        RetryableError: transient failures (timeout, rate limit)
        ContentBlockError: moderation block (non-retryable)
        FatalError: other fatal errors
    """
    ledger = Ledger()
    await ledger.init_db()

    poll_interval = 5.0
    max_polls = 100

    if config.DRY_RUN:
        if job_type == "still":
            await asyncio.sleep(config.DRY_RUN_STILL_DELAY)
        else:
            await asyncio.sleep(config.DRY_RUN_CLIP_DELAY)

        if random.random() < config.DRY_RUN_CONTENT_BLOCK_RATE:
            raise ContentBlockError(
                f"Content moderation blocked job {job_id}", provider="higgsfield"
            )

        if random.random() < config.DRY_RUN_FAILURE_RATE:
            raise RetryableError(f"Transient failure for job {job_id}")

        return Asset(
            asset_id=f"asset-{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            url=f"https://example.com/assets/{job_id}.{'jpg' if job_type == 'still' else 'mp4'}",
            status=JobStatus.COMPLETED,
            cost=6.5 if job_type == "still" else 28.0,
        )

    # Real Higgsfield API polling
    provider = HiggsfieldProvider(dry_run=False)
    
    for i in range(max_polls):
        activity.heartbeat({"job_id": job_id, "poll": i, "status": "polling"})
        
        status = await provider.get_job_status(job_id)
        
        if status.status == ProviderJobStatus.COMPLETED:
            # Job complete
            await ledger.update_job_status_by_job_id(job_id, "completed")
            return Asset(
                asset_id=f"asset-{uuid.uuid4().hex[:12]}",
                job_id=job_id,
                url=status.output_url or "",
                status=JobStatus.COMPLETED,
                cost=status.cost,
            )
        
        elif status.status == ProviderJobStatus.BLOCKED:
            # NSFW moderation block - map to ContentBlockError
            # This triggers recovery ladder (rephrase → Kling → never static hold)
            await ledger.update_job_status_by_job_id(job_id, "blocked")
            raise ContentBlockError(
                f"Content moderation blocked job {job_id}: {status.error}",
                provider="higgsfield"
            )
        
        elif status.status == ProviderJobStatus.FAILED:
            # Failed generation - retryable
            await ledger.update_job_status_by_job_id(job_id, "failed")
            raise RetryableError(f"Generation failed for job {job_id}: {status.error}")
        
        elif status.status in (ProviderJobStatus.PENDING, ProviderJobStatus.PROCESSING):
            # Still processing - continue polling
            await asyncio.sleep(poll_interval)
        
        else:
            # Unknown status - retry
            await asyncio.sleep(poll_interval)
    
    # Max polls reached - timeout
    raise RetryableError(f"Job {job_id} timed out after {max_polls} polls")
