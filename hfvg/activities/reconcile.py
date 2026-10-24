"""Reconciler activities: find and process pending_reconcile jobs."""

import os
from typing import Any

import aiosqlite
from temporalio import activity

from hfvg.budget import BudgetLedger


@activity.defn
async def get_pending_reconcile_jobs(max_jobs: int = 100) -> list[dict[str, Any]]:
    """
    Find all jobs with status='pending_reconcile'.
    
    Args:
        max_jobs: Maximum number of jobs to return
    
    Returns:
        list of job dicts with: job_id, episode_id, shot_id, job_type, 
        line_name, reserved_amount, created_at
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    async with aiosqlite.connect(db_path) as db:
        # Query the per_job_ledger for pending_reconcile jobs
        async with db.execute(
            """
            SELECT job_id, episode_id, shot_id, job_type, line_name, 
                   reserved_usd_micros, created_at
            FROM per_job_ledger
            WHERE status = 'pending_reconcile'
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (max_jobs,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    jobs = []
    for row in rows:
        jobs.append({
            "job_id": row[0],
            "episode_id": row[1],
            "shot_id": row[2],
            "job_type": row[3],
            "line_name": row[4],
            "reserved_amount": row[5],
            "created_at": row[6],
        })
    
    activity.logger.info(
        f"Found {len(jobs)} pending_reconcile jobs (max_jobs={max_jobs})"
    )
    
    return jobs


@activity.defn
async def detect_stalled_jobs(
    threshold_minutes: int = 45
) -> list[dict[str, Any]]:
    """
    Detect jobs with no progress beyond threshold (stall watchdog).
    
    Args:
        threshold_minutes: Minutes without progress to consider stalled
    
    Returns:
        list of stalled job dicts with: job_id, episode_id, shot_id, 
        job_type, minutes_stalled, created_at
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    async with aiosqlite.connect(db_path) as db:
        # Find jobs that are reserved but not terminal, older than threshold
        async with db.execute(
            """
            SELECT job_id, episode_id, shot_id, job_type, created_at,
                   CAST((julianday('now') - julianday(created_at)) * 1440 AS INTEGER) 
                       as minutes_stalled
            FROM per_job_ledger
            WHERE status = 'reserved'
              AND CAST((julianday('now') - julianday(created_at)) * 1440 AS INTEGER) > ?
            ORDER BY created_at ASC
            """,
            (threshold_minutes,)
        ) as cursor:
            rows = await cursor.fetchall()
    
    stalled = []
    for row in rows:
        stalled.append({
            "job_id": row[0],
            "episode_id": row[1],
            "shot_id": row[2],
            "job_type": row[3],
            "created_at": row[4],
            "minutes_stalled": row[5],
        })
    
    if stalled:
        activity.logger.warning(
            f"Detected {len(stalled)} stalled jobs (threshold={threshold_minutes}min)"
        )
        
        # Record in audit log
        async with aiosqlite.connect(db_path) as db:
            for job in stalled:
                await db.execute(
                    """INSERT INTO audit_log (episode_id, action, details, user)
                       VALUES (?, ?, ?, ?)""",
                    (job["episode_id"], "stall_detected",
                     f"{job['job_type']} {job['shot_id']} job {job['job_id']} "
                     f"stalled for {job['minutes_stalled']} minutes",
                     "system")
                )
            await db.commit()
    else:
        activity.logger.info(
            f"No stalled jobs detected (threshold={threshold_minutes}min)"
        )
    
    return stalled


@activity.defn
async def mark_job_stalled(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
    minutes_stalled: int,
) -> None:
    """
    Mark a job as stalled (separate from pending_reconcile).
    
    This sets a flag that can be surfaced in API/UI while the job
    remains in 'reserved' status. The reconciler will pick it up
    if it becomes pending_reconcile.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
        minutes_stalled: How many minutes the job has been stalled
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
    # For now, we just record in audit log
    # Could extend per_job_ledger schema to add a stalled flag
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "job_stalled",
             f"{job_type} {shot_id} job {job_id} marked stalled "
             f"after {minutes_stalled} minutes",
             "system")
        )
        await db.commit()
    
    activity.logger.warning(
        f"Job {job_id} marked stalled after {minutes_stalled} minutes"
    )
