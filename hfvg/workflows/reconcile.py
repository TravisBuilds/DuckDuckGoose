"""ReconcilePendingJobsWorkflow: stall watchdog + reconciler, run on a Temporal Schedule.

One run:
1. Watchdog: ``detect_stalled_jobs`` alerts on ``reserved`` jobs with no ledger progress for
   STALL_THRESHOLD_MINUTES and routes them to step 2 (status -> pending_reconcile).
2. For each ``pending_reconcile`` job: re-poll by harness job id (``poll_job_status``).
   - completed -> ``commit_job_budget`` + ``commit_credits_activity`` (both idempotent per job id:
     a job the shot workflow already committed is never charged twice), then ``land_late_asset``
     (download, normalize, attach).
   - failed / blocked / canceled -> ``release_job_budget`` + ``release_credits_activity``
     (idempotent; a committed job is never released).
   - still running -> left pending for the next scheduled run.
3. Late assets whose landing failed earlier are re-landed (no re-poll, no second commit).

Schedule: :func:`ensure_reconcile_schedule` creates/updates the Temporal Schedule
``RECONCILE_SCHEDULE_ID`` (every RECONCILE_INTERVAL_MINUTES, overlap SKIP); the worker calls it on
start (RECONCILE_SCHEDULE_ENABLED) and ``hfvg reconcile --schedule`` does too.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from hfvg.activities.credit_activities import commit_credits_activity, release_credits_activity
    from hfvg.activities.reconcile import (
        MAX_LAND_ATTEMPTS,
        detect_stalled_jobs,
        get_pending_reconcile_jobs,
        land_late_asset,
        record_reconcile_outcome,
    )
    from hfvg.activities.studio_generation import (
        commit_job_budget,
        poll_job_status,
        release_job_budget,
    )

RECONCILE_WORKFLOW_ID = "hfvg-reconcile-pending-jobs"
RECONCILE_SCHEDULE_ID = "hfvg-reconcile-schedule"

_TERMINAL_FAILURES = ("failed", "blocked", "canceled")


@workflow.defn
class ReconcilePendingJobsWorkflow:
    """Watchdog + reconciler pass over the per-job USD ledger. Safe to run repeatedly."""

    def __init__(self) -> None:
        self.summary: dict = {}

    @workflow.query
    def get_summary(self) -> dict:
        return self.summary

    @workflow.run
    async def run(self, max_jobs: int = 100, stall_threshold_minutes: int | None = None) -> dict:
        quick = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))
        summary = {"total": 0, "committed": 0, "released": 0, "landed": 0, "land_failed": 0,
                   "still_pending": 0, "stalled": [], "errors": []}
        self.summary = summary

        # 1. Stall watchdog: alert + route stalled jobs to this reconciler pass
        stalled = await workflow.execute_activity(
            detect_stalled_jobs, args=[stall_threshold_minutes],
            start_to_close_timeout=timedelta(minutes=1), retry_policy=quick)
        summary["stalled"] = [s["job_id"] for s in stalled]

        # 2/3. Work list
        jobs = await workflow.execute_activity(
            get_pending_reconcile_jobs, args=[max_jobs],
            start_to_close_timeout=timedelta(minutes=1), retry_policy=quick)
        summary["total"] = len(jobs)

        for job in jobs:
            try:
                if job["phase"] == "land":
                    await self._land(job, job["output_url"], summary, quick)
                    continue
                status = await workflow.execute_activity(
                    poll_job_status, args=[job["job_id"], job["job_type"], job["episode_id"]],
                    start_to_close_timeout=timedelta(seconds=60), retry_policy=quick)
                state = status["status"]
                if state == "completed":
                    await workflow.execute_activity(
                        commit_job_budget,
                        args=[job["episode_id"], job["shot_id"], job["job_id"], job["job_type"],
                              job["line_name"], job["usd_micros"], None],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                    await workflow.execute_activity(
                        commit_credits_activity, args=[job["episode_id"], job["job_id"]],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                    summary["committed"] += 1
                    await workflow.execute_activity(
                        record_reconcile_outcome, args=[job, "committed", status.get("output_url") or ""],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                    await self._land(job, status["output_url"], summary, quick)
                elif state in _TERMINAL_FAILURES:
                    reason = f"reconciled {state}: {status.get('error') or ''}".strip()
                    await workflow.execute_activity(
                        release_job_budget,
                        args=[job["episode_id"], job["shot_id"], job["job_id"], job["job_type"],
                              job["line_name"], job["usd_micros"], reason],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                    await workflow.execute_activity(
                        release_credits_activity, args=[job["episode_id"], job["job_id"], reason],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                    summary["released"] += 1
                    await workflow.execute_activity(
                        record_reconcile_outcome, args=[job, "released", reason],
                        start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
                else:
                    summary["still_pending"] += 1
            except ActivityError as e:
                summary["errors"].append({"job_id": job["job_id"], "error": str(e.cause or e)})
        self.summary = summary
        return summary

    async def _land(self, job: dict, url: str | None, summary: dict, quick: RetryPolicy) -> None:
        if not url:
            summary["land_failed"] += 1
            summary["errors"].append({"job_id": job["job_id"], "error": "completed without an output URL"})
            return
        landed = await workflow.execute_activity(
            land_late_asset, args=[job, url],
            start_to_close_timeout=timedelta(minutes=10), retry_policy=quick)
        if landed.get("landed"):
            summary["landed"] += 1
            await workflow.execute_activity(
                record_reconcile_outcome, args=[job, "landed", landed.get("local_path") or ""],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)
        else:
            summary["land_failed"] += 1
            abandoned = int(landed.get("attempts") or 0) >= MAX_LAND_ATTEMPTS
            await workflow.execute_activity(
                record_reconcile_outcome,
                args=[job, "land_abandoned" if abandoned else "land_failed", landed.get("error") or ""],
                start_to_close_timeout=timedelta(seconds=30), retry_policy=quick)


async def ensure_reconcile_schedule(client, task_queue: str, interval_minutes: int | None = None,
                                    max_jobs: int = 100):
    """Create (or update) the Temporal Schedule that runs the reconciler every
    ``interval_minutes`` (default RECONCILE_INTERVAL_MINUTES env, 15). Overlap: SKIP (one pass
    at a time). Returns the schedule handle."""
    import os

    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleUpdate,
    )
    from temporalio.client import ScheduleOverlapPolicy

    minutes = interval_minutes if interval_minutes is not None else int(
        os.getenv("RECONCILE_INTERVAL_MINUTES", "15"))
    if minutes <= 0:
        raise ValueError(f"RECONCILE_INTERVAL_MINUTES must be positive, got {minutes}")
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            ReconcilePendingJobsWorkflow.run, args=[max_jobs, None],
            id=RECONCILE_WORKFLOW_ID, task_queue=task_queue),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=minutes))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        return await client.create_schedule(RECONCILE_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(RECONCILE_SCHEDULE_ID)

        async def _update(_input):
            return ScheduleUpdate(schedule=schedule)

        await handle.update(_update)
        return handle
