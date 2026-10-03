"""Real Temporal integration tests using start_local()."""

import asyncio
import os

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Set test mode with no delays
os.environ["DRY_RUN_STILL_DELAY"] = "0.01"
os.environ["DRY_RUN_CLIP_DELAY"] = "0.01"
os.environ["DRY_RUN_QC_DELAY"] = "0.01"
os.environ["DRY_RUN_MEDIA_DELAY"] = "0.01"
os.environ["DRY_RUN_AUDIO_DELAY"] = "0.01"
os.environ["DRY_RUN_FAILURE_RATE"] = "0.0"

with workflow.unsafe.imports_passed_through():
    from hfvg import activities
    from hfvg.errors import InsufficientCreditsError
    from hfvg.ledger import Ledger
    from hfvg.models import Asset, GenerationRequest, JobStatus
    from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow


# Test-specific activities and workflows

_stall_attempts = 0


@activity.defn
async def stalling_activity() -> str:
    """Activity that stalls on first attempt."""
    global _stall_attempts
    _stall_attempts += 1
    
    if _stall_attempts == 1:
        # Don't heartbeat, will timeout
        await asyncio.sleep(10)
        return "should-not-reach"
    else:
        activity.heartbeat({"status": "working"})
        await asyncio.sleep(0.01)
        return "completed"


@workflow.defn
class StallTestWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            stalling_activity,
            start_to_close_timeout=workflow.timedelta(seconds=10),
            heartbeat_timeout=workflow.timedelta(seconds=1),
            retry_policy=workflow.common.RetryPolicy(
                maximum_attempts=2,
                initial_interval=workflow.timedelta(milliseconds=100),
            ),
        )


# For idempotency test
_idempotency_test_db = None
_idempotency_calls = {"submit": 0, "await": 0}
_idempotency_job_ids = []


@activity.defn
async def tracked_submit(episode_id: str, req: GenerationRequest) -> str:
    global _idempotency_calls, _idempotency_job_ids, _idempotency_test_db
    _idempotency_calls["submit"] += 1
    
    ledger_instance = Ledger(_idempotency_test_db)
    key = ledger_instance.idempotency_key(req)

    existing = await ledger_instance.get_job(key)
    if existing:
        job_id = existing["provider_job_id"]
    else:
        await ledger_instance.check_balance(req.estimated_cost)
        job_id = f"job-{req.shot_id}"
        await ledger_instance.insert_job(key, job_id, episode_id, req.shot_id, req)
        await ledger_instance.deduct_credits(episode_id, req.estimated_cost, "test", job_id)

    _idempotency_job_ids.append(job_id)
    return job_id


@activity.defn
async def tracked_await(job_id: str, job_type: str = "still") -> Asset:
    global _idempotency_calls
    _idempotency_calls["await"] += 1
    activity.heartbeat({"job_id": job_id})

    if _idempotency_calls["await"] == 1:
        raise Exception("Simulated failure")

    return Asset(
        asset_id=f"asset-{job_id}",
        job_id=job_id,
        url=f"https://example.com/{job_id}.jpg",
        status=JobStatus.COMPLETED,
        cost=6.5,
    )


@workflow.defn
class IdempotencyTestWorkflow:
    @workflow.run
    async def run(self) -> dict:
        req = GenerationRequest(
            shot_id="shot-test",
            version=1,
            prompt="Test",
            refs=[],
            params={},
            estimated_cost=6.5,
        )

        job_id = await workflow.execute_activity(
            tracked_submit,
            args=["ep", req],
            start_to_close_timeout=workflow.timedelta(seconds=5),
        )

        asset = await workflow.execute_activity(
            tracked_await,
            args=[job_id, "still"],
            start_to_close_timeout=workflow.timedelta(seconds=5),
            heartbeat_timeout=workflow.timedelta(seconds=2),
            retry_policy=workflow.common.RetryPolicy(maximum_attempts=3),
        )

        return {"job_id": job_id, "asset_url": asset.url}


# For credits test
_credits_test_db = None


@activity.defn
async def expensive_check(cost: float) -> str:
    global _credits_test_db
    ledger_instance = Ledger(_credits_test_db)
    await ledger_instance.check_balance(cost)
    await ledger_instance.deduct_credits("test-ep", cost, "test")
    return "completed"


@workflow.defn
class CreditPauseWorkflow:
    def __init__(self):
        self.paused = False
        self.resume_signal_received = False

    @workflow.run
    async def run(self) -> dict:
        try:
            result = await workflow.execute_activity(
                expensive_check,
                args=[100.0],
                start_to_close_timeout=workflow.timedelta(seconds=5),
                retry_policy=workflow.common.RetryPolicy(
                    maximum_attempts=1,
                    non_retryable_error_types=["InsufficientCreditsError"],
                ),
            )
            return {"status": "completed", "result": result}
        except Exception:
            self.paused = True
            await workflow.wait_condition(lambda: self.resume_signal_received)

            # Try again after credits added
            result = await workflow.execute_activity(
                expensive_check,
                args=[100.0],
                start_to_close_timeout=workflow.timedelta(seconds=5),
            )
            return {"status": "resumed", "result": result}

    @workflow.signal
    def resume(self):
        self.resume_signal_received = True

    @workflow.query
    def is_paused(self) -> bool:
        return self.paused


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_full_episode_through_all_gates(tmp_path):
    """Test (a): Full episode driven through ALL gates to completion."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeWorkflow, ShotWorkflow, PostingWorkflow],
            activities=[
                activities.submit_still_job,
                activities.submit_clip_job,
                activities.await_job,
                activities.review_still,
                activities.review_clip,
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.post_to_platform,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["test-ep-full", "Test idea", ["instagram"]],
                id="test-ep-full",
                task_queue="test-queue",
            )

            # Give workflow a moment to start
            await asyncio.sleep(0.5)

            # Approve all gates
            await handle.execute_update(EpisodeWorkflow.approve_readback)
            await asyncio.sleep(0.1)

            await handle.execute_update(EpisodeWorkflow.approve_character_locks)
            await asyncio.sleep(0.1)

            await handle.execute_update(EpisodeWorkflow.approve_storyboard)
            await asyncio.sleep(0.5)

            # Approve both scenes
            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[1])
            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[2])
            await asyncio.sleep(1)

            await handle.execute_update(EpisodeWorkflow.approve_final)
            await asyncio.sleep(0.5)

            # Approve posts
            posting_handle = env.client.get_workflow_handle("test-ep-full-posting")
            await posting_handle.execute_update(PostingWorkflow.approve_posts, args=[["instagram"]])

            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=20)

            assert result["status"] == "completed"
            assert result["episode_id"] == "test-ep-full"
            assert "final_url" in result


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_heartbeat_timeout_causes_retry(tmp_path):
    """Test (b): Activity without heartbeats times out and retries."""
    global _stall_attempts
    _stall_attempts = 0  # Reset
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[StallTestWorkflow],
            activities=[stalling_activity],
        ):
            result = await env.client.execute_workflow(
                StallTestWorkflow.run,
                id="test-stall",
                task_queue="test-queue",
            )

            assert result == "completed"
            assert _stall_attempts == 2  # First stalled, second succeeded


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_shot_retry_resumes_same_job(tmp_path):
    """Test (c): Shot workflow retry resumes same provider job ID."""
    global _idempotency_test_db, _idempotency_calls, _idempotency_job_ids
    
    db_path = str(tmp_path / "test.db")
    _idempotency_test_db = db_path
    _idempotency_calls = {"submit": 0, "await": 0}
    _idempotency_job_ids = []
    
    ledger = Ledger(db_path)
    await ledger.init_db()

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[IdempotencyTestWorkflow],
            activities=[tracked_submit, tracked_await],
        ):
            result = await env.client.execute_workflow(
                IdempotencyTestWorkflow.run,
                id="test-shot-idempotency",
                task_queue="test-queue",
            )

            # Submit called twice (original + retry) but same job ID
            assert _idempotency_calls["submit"] == 2
            assert _idempotency_calls["await"] == 2
            assert len(set(_idempotency_job_ids)) == 1  # Only ONE unique job ID
            assert result["job_id"] == _idempotency_job_ids[0]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_insufficient_credits_pauses_workflow(tmp_path):
    """Test (d): InsufficientCredits pauses workflow until resume Signal."""
    global _credits_test_db
    
    db_path = str(tmp_path / "test.db")
    _credits_test_db = db_path
    ledger = Ledger(db_path)
    await ledger.init_db()

    # Drain credits
    await ledger.deduct_credits("test", 9990.0, "setup")

    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[CreditPauseWorkflow],
            activities=[expensive_check],
        ):
            handle = await env.client.start_workflow(
                CreditPauseWorkflow.run,
                id="test-credits",
                task_queue="test-queue",
            )

            # Wait for pause
            await asyncio.sleep(1)
            paused = await handle.query(CreditPauseWorkflow.is_paused)
            assert paused

            # Add credits and resume
            await ledger.add_credits("test-ep", 200.0, "grant")
            await handle.signal(CreditPauseWorkflow.resume)

            result = await asyncio.wait_for(handle.result(), timeout=10)
            assert result["status"] == "resumed"
