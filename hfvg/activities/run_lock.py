"""Activities for account-level run lock management."""

import os
from temporalio import activity

from hfvg.credit_ledger import CreditLedger


@activity.defn
async def acquire_run_lock(
    episode_id: str,
    workflow_id: str,
    account_identifier: str = "default",
    stale_timeout_seconds: int | None = None,
) -> dict:
    """
    Acquire account-level run lock at workflow start.
    
    Args:
        episode_id: Episode identifier
        workflow_id: Temporal workflow ID
        account_identifier: Account/org identifier for the lock
        stale_timeout_seconds: Timeout after which a lock is considered stale and can be reclaimed.
                               Defaults to RUN_LOCK_STALE_TIMEOUT_SECONDS env var (default: 3600)
    
    Returns:
        dict with acquired (bool), existing_episode (str|None), reclaimed (bool)
    
    Raises:
        ValueError: If another episode is already running on this account (and lock is not stale)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Get timeout from parameter or environment (default 3600 = 1 hour)
    if stale_timeout_seconds is None:
        stale_timeout_seconds = int(os.getenv("RUN_LOCK_STALE_TIMEOUT_SECONDS", "3600"))
    
    result = await ledger.acquire_run_lock(
        episode_id, workflow_id, account_identifier, stale_timeout_seconds
    )
    
    if not result["acquired"]:
        activity.logger.error(
            f"Run lock acquisition failed for {episode_id}: "
            f"account {account_identifier} already running {result['existing_episode']}"
        )
        raise ValueError(
            f"Account {account_identifier} already has episode {result['existing_episode']} running. "
            "Wait for it to complete or release the lock."
        )
    
    if result.get("reclaimed"):
        activity.logger.warning(
            f"Run lock reclaimed for {episode_id} on account {account_identifier} "
            f"(stale lock from {result['existing_episode']})"
        )
    else:
        activity.logger.info(f"Run lock acquired for {episode_id} on account {account_identifier}")
    
    return result


@activity.defn
async def release_run_lock(
    episode_id: str,
) -> None:
    """
    Release account-level run lock at workflow end.
    
    Args:
        episode_id: Episode identifier
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    await ledger.release_run_lock(episode_id)
    
    activity.logger.info(f"Run lock released for {episode_id}")
