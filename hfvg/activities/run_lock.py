"""Activities for account-level run lock management."""

import os
from temporalio import activity

from hfvg.credit_ledger import CreditLedger


@activity.defn
async def acquire_run_lock(
    episode_id: str,
    workflow_id: str,
    account_identifier: str = "default",
) -> dict:
    """
    Acquire account-level run lock at workflow start.
    
    Args:
        episode_id: Episode identifier
        workflow_id: Temporal workflow ID
        account_identifier: Account/org identifier for the lock
    
    Returns:
        dict with acquired (bool), existing_episode (str|None)
    
    Raises:
        ValueError: If another episode is already running on this account
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    result = await ledger.acquire_run_lock(episode_id, workflow_id, account_identifier)
    
    if not result["acquired"]:
        activity.logger.error(
            f"Run lock acquisition failed for {episode_id}: "
            f"account {account_identifier} already running {result['existing_episode']}"
        )
        raise ValueError(
            f"Account {account_identifier} already has episode {result['existing_episode']} running. "
            "Wait for it to complete or release the lock."
        )
    
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
