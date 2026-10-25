"""Higgsfield balance check activity."""

import os
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite
from temporalio import activity
from temporalio.exceptions import ApplicationError


@activity.defn
async def check_higgsfield_balance_activity(episode_id: str) -> dict:
    """
    Check if manual Higgsfield balance entry exists and is not too old.
    
    Required before live runs since there's no balance endpoint.
    
    Returns:
        dict with balance_usd, recorded_at, age_hours
        
    Raises:
        ApplicationError if no balance or balance too old
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    max_age_hours = int(os.getenv("HFVG_BALANCE_MAX_AGE_HOURS", "24"))
    
    # Ensure manual_balances table exists
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS manual_balances (
                provider TEXT NOT NULL,
                balance_usd REAL NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
                recorded_by TEXT,
                notes TEXT,
                PRIMARY KEY (provider, recorded_at)
            )
        """)
        await db.commit()
        
        # Get latest Higgsfield balance
        async with db.execute(
            """SELECT balance_usd, recorded_at, recorded_by, notes
               FROM manual_balances
               WHERE provider = 'higgsfield'
               ORDER BY recorded_at DESC
               LIMIT 1"""
        ) as cursor:
            row = await cursor.fetchone()
    
    if not row:
        raise ApplicationError(
            "No manual Higgsfield balance recorded. Run CLI command to record balance before live runs: "
            "hfvg balance record higgsfield <amount_usd>",
            type="NoBalanceRecorded",
            non_retryable=True
        )
    
    balance_usd, recorded_at_str, recorded_by, notes = row
    recorded_at = datetime.fromisoformat(recorded_at_str)
    age = datetime.now() - recorded_at
    age_hours = age.total_seconds() / 3600
    
    if age_hours > max_age_hours:
        raise ApplicationError(
            f"Higgsfield balance is {age_hours:.1f} hours old (recorded {recorded_at_str}), "
            f"exceeds maximum age {max_age_hours} hours. "
            f"Record a fresh balance before live runs: hfvg balance record higgsfield <amount_usd>",
            type="BalanceTooOld",
            non_retryable=True
        )
    
    activity.logger.info(
        f"Higgsfield balance check passed: ${balance_usd:.2f} USD recorded {age_hours:.1f}h ago"
    )
    
    return {
        "balance_usd": balance_usd,
        "recorded_at": recorded_at_str,
        "age_hours": age_hours,
        "recorded_by": recorded_by,
        "notes": notes,
    }
