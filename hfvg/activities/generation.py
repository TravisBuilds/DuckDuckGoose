"""Generation activities: stills and clips with submit/await pattern."""

import asyncio
import random
import uuid

from temporalio import activity

from hfvg.config import config
from hfvg.errors import ContentBlockError, RetryableError
from hfvg.ledger import Ledger
from hfvg.models import Asset, GenerationRequest, JobStatus


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
        raise NotImplementedError("Real Higgsfield API calls not implemented in milestone 1")

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
        raise NotImplementedError("Real Higgsfield API calls not implemented in milestone 1")

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

    for i in range(max_polls):
        activity.heartbeat({"job_id": job_id, "poll": i, "status": "polling"})

        await asyncio.sleep(poll_interval)

        raise NotImplementedError("Real Higgsfield API calls not implemented in milestone 1")
