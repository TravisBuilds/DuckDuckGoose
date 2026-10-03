"""Test credit guard: InsufficientCredits pauses rather than retries."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.errors import InsufficientCreditsError
from hfvg.ledger import Ledger
from hfvg.models import GenerationRequest


@pytest.mark.asyncio
async def test_insufficient_credits_raises_error():
    """Test that insufficient credits raises non-retryable error."""
    ledger = Ledger(":memory:")
    await ledger.init_db()

    await ledger.deduct_credits("test", 9990.0, "test")

    balance = await ledger.get_balance()
    assert balance < 20.0

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            activities=[activities.submit_still_job],
        ):
            req = GenerationRequest(
                shot_id="shot-001",
                version=1,
                prompt="Expensive still",
                refs=[],
                params={},
                estimated_cost=100.0,
            )

            with pytest.raises(InsufficientCreditsError) as exc_info:
                await env.client.execute_activity(
                    activities.submit_still_job,
                    args=["test-ep", req],
                    task_queue="test-queue",
                    id="test-expensive-activity",
                )

            assert exc_info.value.required == 100.0
            assert exc_info.value.available < 20.0


@pytest.mark.asyncio
async def test_sufficient_credits_allows_submission():
    """Test that sufficient credits allows job submission."""
    ledger = Ledger(":memory:")
    await ledger.init_db()

    balance = await ledger.get_balance()
    assert balance >= 10.0

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            activities=[activities.submit_still_job],
        ):
            req = GenerationRequest(
                shot_id="shot-001",
                version=1,
                prompt="Affordable still",
                refs=[],
                params={},
                estimated_cost=10.0,
            )

            job_id = await env.client.execute_activity(
                activities.submit_still_job,
                args=["test-ep", req],
                task_queue="test-queue",
                id="test-affordable-activity",
            )

            assert job_id.startswith("still-")

            new_balance = await ledger.get_balance()
            assert new_balance == balance - 10.0
