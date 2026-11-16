"""BalanceCheckWorkflow: the scheduled balance read, alerts and provider usage sync.

A Temporal Schedule (:func:`ensure_balance_check_schedule`, every BALANCE_CHECK_INTERVAL_MINUTES,
default 360, overlap SKIP) starts one run per interval; the run is a single activity
(``scheduled_balance_check``). The worker ensures the schedule on start unless
BALANCE_CHECK_SCHEDULE_ENABLED=false. Separate from the episode workflows: nothing here is replayed
by an episode run.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg.activities.balance_check import scheduled_balance_check

BALANCE_CHECK_WORKFLOW_ID = "hfvg-balance-check"
BALANCE_CHECK_SCHEDULE_ID = "hfvg-balance-check-schedule"


@workflow.defn
class BalanceCheckWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            scheduled_balance_check,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=RetryPolicy(maximum_attempts=2, initial_interval=timedelta(seconds=30)),
        )


async def ensure_balance_check_schedule(client, task_queue: str, interval_minutes: int | None = None):
    """Create (or update) the balance check schedule. Returns the schedule handle."""
    from temporalio.client import (
        Schedule,
        ScheduleActionStartWorkflow,
        ScheduleAlreadyRunningError,
        ScheduleIntervalSpec,
        ScheduleOverlapPolicy,
        SchedulePolicy,
        ScheduleSpec,
        ScheduleUpdate,
    )

    from hfvg.balance_monitor import balance_check_interval_minutes

    minutes = interval_minutes if interval_minutes is not None else balance_check_interval_minutes()
    if minutes <= 0:
        raise ValueError(f"BALANCE_CHECK_INTERVAL_MINUTES must be positive, got {minutes}")
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            BalanceCheckWorkflow.run, id=BALANCE_CHECK_WORKFLOW_ID, task_queue=task_queue),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(minutes=minutes))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        return await client.create_schedule(BALANCE_CHECK_SCHEDULE_ID, schedule)
    except ScheduleAlreadyRunningError:
        handle = client.get_schedule_handle(BALANCE_CHECK_SCHEDULE_ID)

        async def _update(_input):
            return ScheduleUpdate(schedule=schedule)

        await handle.update(_update)
        return handle
