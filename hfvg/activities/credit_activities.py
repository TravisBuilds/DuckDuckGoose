"""Activities for credit reserve/commit/release operations."""

import os
import aiosqlite
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
    actual_credits: int | None = None,
) -> None:
    """
    Commit credits after successful job completion.
    
    Converts reservation to spent credits.
    
    Args:
        episode_id: Episode identifier
        job_id: Job identifier
        actual_credits: Actual credits consumed (None = use reserved amount)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # If actual_credits not provided, use the reserved amount
    if actual_credits is None:
        line_id = f"episode_{episode_id}"
        async with aiosqlite.connect(ledger.db_path, uri=True) as db:
            async with db.execute(
                """SELECT amount_credits FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
                (line_id, job_id)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    actual_credits = row[0]
                else:
                    # No reservation found - likely DRY_RUN mode, skip silently
                    activity.logger.info(f"No credit reservation found for {job_id}, skipping commit (likely DRY_RUN)")
                    return
    
    # Check if reservation exists before committing
    line_id = f"episode_{episode_id}"
    async with aiosqlite.connect(ledger.db_path, uri=True) as db:
        async with db.execute(
            """SELECT amount_credits FROM credit_transactions 
               WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
            (line_id, job_id)
        ) as cur:
            if not await cur.fetchone():
                # Check if this is a DRY_RUN job (has -dry- in job_id)
                is_dry_run = "-dry-" in job_id
                if is_dry_run:
                    # DRY_RUN mode: skip silently
                    activity.logger.info(f"No credit reservation found for {job_id}, skipping commit (DRY_RUN)")
                    return
                else:
                    # Live mode: missing reservation is an error - raise non-retryable
                    from temporalio.exceptions import ApplicationError
                    raise ApplicationError(
                        f"No credit reservation found for live job {job_id}",
                        non_retryable=True
                    )
    
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
    
    # Check if reservation exists before releasing
    line_id = f"episode_{episode_id}"
    async with aiosqlite.connect(ledger.db_path, uri=True) as db:
        async with db.execute(
            """SELECT amount_credits FROM credit_transactions 
               WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
            (line_id, job_id)
        ) as cur:
            if not await cur.fetchone():
                # Check if this is a DRY_RUN job (has -dry- in job_id)
                is_dry_run = "-dry-" in job_id
                if is_dry_run:
                    # DRY_RUN mode: skip silently
                    activity.logger.info(f"No credit reservation found for {job_id}, skipping release (DRY_RUN)")
                    return
                else:
                    # Live mode: missing reservation is an error - raise non-retryable
                    from temporalio.exceptions import ApplicationError
                    raise ApplicationError(
                        f"No credit reservation found for live job {job_id}",
                        non_retryable=True
                    )
    
    await ledger.release_credits(episode_id, job_id, notes)
    
    activity.logger.info(f"Credits released for {job_id}: {notes}")
