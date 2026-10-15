"""Activities for credit reserve/commit/release operations."""

import os
from temporalio import activity

from hfvg.credit_ledger import CreditLedger
from hfvg.credit_pricing import estimate_still_credits, estimate_clip_credits


@activity.defn
async def reserve_credits_activity(
    episode_id: str,
    job_id: str,
    credits: int,
    notes: str = "",
) -> dict:
    """
    Reserve credits before job submission.
    
    Args:
        episode_id: Episode identifier
        job_id: Unique job identifier (used as idempotency key)
        credits: Credits to reserve
        notes: Optional description
    
    Returns:
        dict with reserved (bool), available (int), cap (int)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    result = await ledger.reserve_credits(episode_id, job_id, credits, notes)
    
    activity.logger.info(
        f"Credit reserve for {job_id}: reserved={result['reserved']}, "
        f"credits={credits}, available={result.get('available', 0)}"
    )
    
    return result


@activity.defn
async def commit_credits_activity(
    episode_id: str,
    job_id: str,
    actual_credits: int,
) -> None:
    """
    Commit credits after successful job completion.
    
    Converts reservation to spent credits.
    
    Args:
        episode_id: Episode identifier
        job_id: Job identifier
        actual_credits: Actual credits consumed (may differ from reserved)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    await ledger.commit_credits(episode_id, job_id, actual_credits)
    
    activity.logger.info(f"Credits committed for {job_id}: {actual_credits} credits")


@activity.defn
async def release_credits_activity(
    episode_id: str,
    job_id: str,
    notes: str = "",
) -> None:
    """
    Release reserved credits after job failure/cancellation.
    
    Args:
        episode_id: Episode identifier
        job_id: Job identifier
        notes: Reason for release
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    await ledger.release_credits(episode_id, job_id, notes)
    
    activity.logger.info(f"Credits released for {job_id}: {notes}")
