"""Reconciler workflow: reconcile pending_reconcile jobs.

A scheduled Temporal workflow that:
1. Finds all jobs with status='pending_reconcile'
2. Re-polls provider job status by job_id
3. Lands late assets (download, normalize, attach to shot)
4. Commits or releases the budget (idempotent, never double-charges)
5. Updates job status to prevent double-processing

Runs on a cron schedule (default: every 15 minutes).
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg.activities.studio_generation import (
        commit_job_budget,
        poll_job_status,
        release_job_budget,
    )
    from hfvg.activities.save_assets import save_asset_locally
    from hfvg.activities.credit_activities import (
        commit_credits_activity,
        release_credits_activity,
    )


@workflow.defn(name="ReconcilePendingJobsWorkflow")
class ReconcilePendingJobsWorkflow:
    """Reconcile all pending_reconcile jobs."""
    
    @workflow.run
    async def run(self, max_jobs: int = 100) -> dict[str, Any]:
        """
        Reconcile pending jobs.
        
        Args:
            max_jobs: Maximum number of jobs to process in one run
        
        Returns:
            dict with reconciliation statistics
        """
        workflow.logger.info(f"Starting reconciliation run (max_jobs={max_jobs})")
        
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=["InsufficientCreditsError"],
        )
        
        # Get pending jobs
        from hfvg.activities.reconcile import get_pending_reconcile_jobs
        
        pending_jobs = await workflow.execute_activity(
            get_pending_reconcile_jobs,
            args=[max_jobs],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=retry_policy,
        )
        
        workflow.logger.info(
            f"Found {len(pending_jobs)} pending_reconcile jobs to process"
        )
        
        stats = {
            "total": len(pending_jobs),
            "completed": 0,
            "failed": 0,
            "still_pending": 0,
            "errors": [],
        }
        
        for job in pending_jobs:
            job_id = job["job_id"]
            episode_id = job["episode_id"]
            shot_id = job["shot_id"]
            job_type = job["job_type"]
            line_name = job["line_name"]
            reserved_amount = job["reserved_amount"]
            
            workflow.logger.info(
                f"Reconciling job {job_id} ({job_type} for {episode_id}/{shot_id})"
            )
            
            try:
                # Re-poll provider status
                status_result = await workflow.execute_activity(
                    poll_job_status,
                    args=[job_id, job_type, episode_id],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
                
                status = status_result["status"]
                
                if status == "completed":
                    # Job completed - commit budget and save asset
                    workflow.logger.info(f"Job {job_id} completed late, committing")
                    
                    # Commit budget
                    await workflow.execute_activity(
                        commit_job_budget,
                        args=[
                            episode_id, shot_id, job_id, job_type,
                            line_name, reserved_amount,
                            status_result.get("cost"),
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    # Commit credits
                    await workflow.execute_activity(
                        commit_credits_activity,
                        args=[episode_id, job_id],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    # Save asset locally
                    if "output_url" in status_result:
                        try:
                            await workflow.execute_activity(
                                save_asset_locally,
                                args=[
                                    episode_id, shot_id,
                                    status_result["output_url"],
                                    job_type, 1
                                ],
                                start_to_close_timeout=timedelta(minutes=10),
                                retry_policy=retry_policy,
                            )
                            workflow.logger.info(
                                f"Late asset saved for {job_id}"
                            )
                        except Exception as e:
                            workflow.logger.warning(
                                f"Failed to save late asset for {job_id}: {e}"
                            )
                    
                    stats["completed"] += 1
                    
                elif status in ("failed", "blocked", "canceled"):
                    # Job failed - release budget
                    workflow.logger.info(
                        f"Job {job_id} {status}, releasing budget"
                    )
                    
                    error_details = status_result.get("error", "no details")
                    reason = f"reconcile: {status}: {error_details}"
                    
                    await workflow.execute_activity(
                        release_job_budget,
                        args=[
                            episode_id, shot_id, job_id, job_type,
                            line_name, reserved_amount, reason,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    # Release credits
                    await workflow.execute_activity(
                        release_credits_activity,
                        args=[episode_id, job_id, reason],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    stats["failed"] += 1
                    
                else:
                    # Still pending - leave it for next run
                    workflow.logger.info(
                        f"Job {job_id} still {status}, leaving for next run"
                    )
                    stats["still_pending"] += 1
                    
            except Exception as e:
                workflow.logger.error(
                    f"Error reconciling job {job_id}: {e}"
                )
                stats["errors"].append({
                    "job_id": job_id,
                    "error": str(e),
                })
        
        workflow.logger.info(
            f"Reconciliation run complete: {stats['completed']} completed, "
            f"{stats['failed']} failed, {stats['still_pending']} still pending, "
            f"{len(stats['errors'])} errors"
        )
        
        return stats
