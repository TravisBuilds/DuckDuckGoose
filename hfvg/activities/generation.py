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


@activity.defn
async def submit_still_job(episode_id: str, req: GenerationRequest) -> str:
    """
    Submit a still generation job with idempotency.

    Returns provider job_id. Never submits the same request twice.
    """
    ledger = Ledger()
    await ledger.init_db()

    key = ledger.idempotency_key(req)

    existing = await ledger.get_job(key)
    if existing:
        activity.logger.info(f"Job already exists for key {key[:8]}: {existing['provider_job_id']}")
        return existing["provider_job_id"]

    await ledger.check_balance(req.estimated_cost)

    if config.DRY_RUN:
        job_id = f"still-{uuid.uuid4().hex[:12]}"
        activity.logger.info(f"[DRY-RUN] Submitted still job {job_id} for {req.shot_id}")
    else:
        # Real Higgsfield API call
        provider = HiggsfieldProvider(dry_run=False)
        
        # Determine resolution and quality from request
        # Default to draft tier (1k medium) unless specified
        resolution = req.params.get("resolution", "1k")
        quality = req.params.get("quality", "medium")
        model = req.params.get("model", "gpt_image_2")
        
        job_id = await provider.submit_image(
            prompt=req.prompt,
            model=model,
            resolution=resolution,
            quality=quality,
            references=req.references or [],
            idempotency_key=key,
        )
        activity.logger.info(f"[API] Submitted still job {job_id} for {req.shot_id}")

    await ledger.insert_job(key, job_id, episode_id, req.shot_id, req, "running")
    await ledger.deduct_credits(episode_id, req.estimated_cost, "estimate", job_id)

    return job_id


@activity.defn
async def submit_clip_job(episode_id: str, req: GenerationRequest) -> str:
    """
    Submit a clip generation job with idempotency.

    Returns provider job_id. Never submits the same request twice.
    """
    ledger = Ledger()
    await ledger.init_db()

    key = ledger.idempotency_key(req)

    existing = await ledger.get_job(key)
    if existing:
        activity.logger.info(f"Job already exists for key {key[:8]}: {existing['provider_job_id']}")
        return existing["provider_job_id"]

    await ledger.check_balance(req.estimated_cost)

    if config.DRY_RUN:
        job_id = f"clip-{uuid.uuid4().hex[:12]}"
        activity.logger.info(f"[DRY-RUN] Submitted clip job {job_id} for {req.shot_id}")
    else:
        # Real Higgsfield API call with model routing per PIPELINE-LESSONS
        provider = HiggsfieldProvider(dry_run=False)
        
        # Model routing: wet/mud/pool/soak/bare → Kling, else → Seedance
        tags = req.params.get("tags", [])
        wet_tags = {"wet", "mud", "pool", "soak", "bare", "underwater"}
        is_wet = any(tag.lower() in wet_tags for tag in tags)
        
        if is_wet:
            # Wet content: Kling 3.0 pro (never blocks)
            model = "kling_3.0"
            resolution = "1080p"  # Kling native
            draft = False
        else:
            # Dry content: Seedance 2.5
            model = "seedance_2.5"
            # Draft tier: 480p with draft:true
            # Final: 720p (NOT 1080p unless hero shot)
            is_draft = req.params.get("tier") == "draft"
            resolution = "480p" if is_draft else "720p"
            draft = is_draft
        
        job_id = await provider.submit_video(
            start_image=req.start_image or "",
            prompt=req.prompt,
            model=model,
            duration=req.params.get("duration", 5.0),
            resolution=resolution,
            draft=draft,
            references=req.references or [],
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
            await ledger.update_job_status(job_id, "completed")
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
            await ledger.update_job_status(job_id, "blocked")
            raise ContentBlockError(
                f"Content moderation blocked job {job_id}: {status.error}",
                provider="higgsfield"
            )
        
        elif status.status == ProviderJobStatus.FAILED:
            # Failed generation - retryable
            await ledger.update_job_status(job_id, "failed")
            raise RetryableError(f"Generation failed for job {job_id}: {status.error}")
        
        elif status.status in (ProviderJobStatus.PENDING, ProviderJobStatus.PROCESSING):
            # Still processing - continue polling
            await asyncio.sleep(poll_interval)
        
        else:
            # Unknown status - retry
            await asyncio.sleep(poll_interval)
    
    # Max polls reached - timeout
    raise RetryableError(f"Job {job_id} timed out after {max_polls} polls")
