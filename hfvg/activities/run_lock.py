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
    
    Checks if existing lock holder workflow is still running via Temporal describe.
    Reclaims lock if holder workflow is terminated/completed/failed.
    
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
    import aiosqlite
    from temporalio import activity as temporal_activity
    from temporalio.client import WorkflowExecutionStatus
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Get timeout from parameter or environment (default 3600 = 1 hour)
    if stale_timeout_seconds is None:
        stale_timeout_seconds = int(os.getenv("RUN_LOCK_STALE_TIMEOUT_SECONDS", "3600"))
    
    # Check if lock exists and if holder workflow is still running
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT episode_id, workflow_id, locked_at FROM run_locks WHERE account_identifier = ?",
            (account_identifier,)
        ) as cursor:
            existing = await cursor.fetchone()
            if existing:
                existing_episode, existing_workflow_id, locked_at_str = existing
                
                # Check if existing workflow is still running via Temporal describe
                try:
                    client = temporal_activity.info().workflow_client()
                    handle = client.get_workflow_handle(existing_workflow_id)
                    desc = await handle.describe()
                    
                    # If workflow is terminated/completed/failed, reclaim the lock
                    if desc.status in (
                        WorkflowExecutionStatus.COMPLETED,
                        WorkflowExecutionStatus.FAILED,
                        WorkflowExecutionStatus.TERMINATED,
                        WorkflowExecutionStatus.TIMED_OUT,
                        WorkflowExecutionStatus.CANCELED,
                    ):
                        activity.logger.warning(
                            f"Reclaiming stale lock from terminated workflow {existing_workflow_id} "
                            f"(status: {desc.status.name}, episode: {existing_episode})"
                        )
                        # Delete stale lock
                        await db.execute(
                            "DELETE FROM run_locks WHERE account_identifier = ?",
                            (account_identifier,)
                        )
                        await db.commit()
                    else:
                        # Workflow is still running, cannot acquire
                        activity.logger.error(
                            f"Run lock acquisition failed for {episode_id}: "
                            f"account {account_identifier} already running {existing_episode} "
                            f"(workflow {existing_workflow_id}, status: {desc.status.name})"
                        )
                        raise ValueError(
                            f"Account {account_identifier} already has episode {existing_episode} running. "
                            "Wait for it to complete or terminate it first."
                        )
                except Exception as e:
                    # If describe fails (workflow not found), assume stale and continue with time-based check
                    activity.logger.warning(
                        f"Could not describe workflow {existing_workflow_id}: {e}. "
                        "Falling back to time-based stale check."
                    )
    
    # Acquire lock (will use time-based stale check if workflow describe failed)
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
