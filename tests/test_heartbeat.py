"""Test heartbeat failures: stalled activities are failed and retried."""

import asyncio

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.models import Asset, JobStatus


@activity.defn
async def stalling_activity(should_stall: bool) -> Asset:
    """Activity that stalls (doesn't heartbeat) if should_stall is True."""
    if should_stall:
        await asyncio.sleep(120)
    else:
        activity.heartbeat({"status": "working"})
        await asyncio.sleep(0.1)
        return Asset(
            asset_id="test-asset",
            job_id="test-job",
            url="https://example.com/test.jpg",
            status=JobStatus.COMPLETED,
            cost=6.5,
        )


@pytest.mark.asyncio
async def test_stalled_activity_is_retried():
    """Test that an activity without heartbeats times out and is retried."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        attempt_count = 0

        @activity.defn
        async def counting_stalling_activity() -> Asset:
            nonlocal attempt_count
            attempt_count += 1

            if attempt_count == 1:
                await asyncio.sleep(120)
            else:
                activity.heartbeat({"status": "working"})
                await asyncio.sleep(0.1)
                return Asset(
                    asset_id="test-asset",
                    job_id="test-job",
                    url="https://example.com/test.jpg",
                    status=JobStatus.COMPLETED,
                    cost=6.5,
                )

        async with Worker(
            env.client,
            task_queue="test-queue",
            activities=[counting_stalling_activity],
        ):
            from datetime import timedelta

            from temporalio.common import RetryPolicy

            result = await env.client.execute_activity(
                counting_stalling_activity,
                task_queue="test-queue",
                id="test-stalling-activity",
                start_to_close_timeout=timedelta(minutes=5),
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                ),
            )

            assert result.asset_id == "test-asset"
            assert attempt_count == 2
