"""Test idempotency: duplicate submits don't create duplicate jobs."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.ledger import Ledger
from hfvg.models import GenerationRequest


@pytest.mark.asyncio
async def test_duplicate_submit_returns_same_job_id():
    """Test that submitting the same request twice returns the same job ID."""
    ledger = Ledger(":memory:")
    await ledger.init_db()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            activities=[
                activities.submit_still_job,
            ],
        ):
            req = GenerationRequest(
                shot_id="shot-001",
                version=1,
                prompt="A cozy kitchen",
                refs=["ref1.jpg"],
                params={"type": "still"},
                estimated_cost=6.5,
            )

            job_id_1 = await env.client.execute_activity(
                activities.submit_still_job,
                args=["test-ep", req],
                task_queue="test-queue",
                id="test-activity-1",
            )

            job_id_2 = await env.client.execute_activity(
                activities.submit_still_job,
                args=["test-ep", req],
                task_queue="test-queue",
                id="test-activity-2",
            )

            assert job_id_1 == job_id_2


@pytest.mark.asyncio
async def test_different_versions_create_different_jobs():
    """Test that different versions create different jobs."""
    ledger = Ledger(":memory:")
    await ledger.init_db()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            activities=[
                activities.submit_still_job,
            ],
        ):
            req_v1 = GenerationRequest(
                shot_id="shot-001",
                version=1,
                prompt="A cozy kitchen",
                refs=["ref1.jpg"],
                params={"type": "still"},
                estimated_cost=6.5,
            )

            req_v2 = GenerationRequest(
                shot_id="shot-001",
                version=2,
                prompt="A cozy kitchen",
                refs=["ref1.jpg"],
                params={"type": "still"},
                estimated_cost=6.5,
            )

            job_id_1 = await env.client.execute_activity(
                activities.submit_still_job,
                args=["test-ep", req_v1],
                task_queue="test-queue",
                id="test-activity-v1",
            )

            job_id_2 = await env.client.execute_activity(
                activities.submit_still_job,
                args=["test-ep", req_v2],
                task_queue="test-queue",
                id="test-activity-v2",
            )

            assert job_id_1 != job_id_2
