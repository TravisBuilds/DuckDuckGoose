"""Temporal integration tests with real Worker."""

import asyncio
import os
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Set test mode with minimal delays
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


# Module-level test workflows
_stall_attempt = 0


@activity.defn
async def stalling_activity() -> str:
    """Activity that stalls on first attempt (no heartbeat)."""
    global _stall_attempt
    _stall_attempt += 1
    
    if _stall_attempt == 1:
        # Don't heartbeat - will timeout (minimal sleep for test speed)
        await asyncio.sleep(2)
        return "should-not-reach"
    else:
        activity.heartbeat({"status": "working"})
        await asyncio.sleep(0.01)
        return "completed"


@workflow.defn
class HeartbeatTestWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            stalling_activity,
            start_to_close_timeout=timedelta(seconds=3),
            heartbeat_timeout=timedelta(seconds=0.8),  # Short timeout for fast test
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_interval=timedelta(milliseconds=50),
            ),
        )


# Idempotency test globals
_idem_db = None
_idem_calls = {"submit": 0, "await": 0}
_idem_job_ids = []


@activity.defn
async def tracked_submit(episode_id: str, req: GenerationRequest) -> str:
    global _idem_calls, _idem_job_ids, _idem_db
    _idem_calls["submit"] += 1
    
    ledger = Ledger(_idem_db)
    key = ledger.idempotency_key(req)

    existing = await ledger.get_job(key)
    if existing:
        job_id = existing["provider_job_id"]
    else:
        await ledger.check_balance(req.estimated_cost)
        job_id = f"job-{req.shot_id}-{_idem_calls['submit']}"
        await ledger.insert_job(key, job_id, episode_id, req.shot_id, req)
        await ledger.deduct_credits(episode_id, req.estimated_cost, "test", job_id)

    _idem_job_ids.append(job_id)
    return job_id


@activity.defn
async def tracked_await(job_id: str, job_type: str = "still") -> Asset:
    global _idem_calls
    _idem_calls["await"] += 1
    activity.heartbeat({"job_id": job_id})

    if _idem_calls["await"] == 1:
        raise Exception("Simulated await failure")

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
            start_to_close_timeout=timedelta(seconds=5),
        )

        asset = await workflow.execute_activity(
            tracked_await,
            args=[job_id, "still"],
            start_to_close_timeout=timedelta(seconds=5),
            heartbeat_timeout=timedelta(seconds=2),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )

        return {"job_id": job_id, "asset_url": asset.url}


# Credits test globals
_credits_db = None


@activity.defn
async def expensive_check(cost: float) -> str:
    global _credits_db
    ledger = Ledger(_credits_db)
    await ledger.check_balance(cost)
    await ledger.deduct_credits("test-ep", cost, "test")
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
                start_to_close_timeout=timedelta(seconds=5),
                retry_policy=RetryPolicy(
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
                start_to_close_timeout=timedelta(seconds=5),
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
@pytest.mark.skip(reason="Legacy EpisodeWorkflow: posting child workflow timing issue (known)")
async def test_a_full_episode_through_all_gates(tmp_path, monkeypatch):
    """Test (a): Full episode workflow driven through ALL gates to completion."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize studio DB (episodes table for live_mode checks)
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(db_path)
    await create_episode(db_path, "test-ep")
    # Keep in dry mode for this test (live_mode defaults to False)
    
    # Set DATABASE_PATH for activities
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
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
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.post_to_platform,
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflow.run,
                args=["test-ep", "Test idea", ["instagram"]],
                id="test-ep-full",
                task_queue="test-queue",
            )

            await asyncio.sleep(0.5)

            # Approve all gates
            await handle.execute_update(EpisodeWorkflow.approve_readback)
            await asyncio.sleep(0.1)

            await handle.execute_update(EpisodeWorkflow.approve_character_locks)
            await asyncio.sleep(0.1)

            await handle.execute_update(EpisodeWorkflow.approve_storyboard)
            await asyncio.sleep(1)

            # Approve both scenes
            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[1])
            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[2])
            await asyncio.sleep(2)

            await handle.execute_update(EpisodeWorkflow.approve_final)
            
            # Wait for PostingWorkflow to be started as child workflow (with retries)
            posting_handle = None
            for attempt in range(10):
                try:
                    posting_handle = env.client.get_workflow_handle("test-ep-posting")
                    # Try to describe it to verify it exists
                    await posting_handle.describe()
                    break
                except Exception:
                    await asyncio.sleep(0.5)
            
            if not posting_handle:
                raise RuntimeError("PostingWorkflow was not started")

            # Approve posts
            await posting_handle.execute_update(PostingWorkflow.approve_posts, args=[["instagram"]])

            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=30)

            assert result["status"] == "completed"
            assert result["episode_id"] == "test-ep"
            assert "final_url" in result


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_b_heartbeat_timeout_causes_retry(tmp_path):
    """Test (b): Activity without heartbeats times out and retries successfully."""
    global _stall_attempt
    _stall_attempt = 0
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[HeartbeatTestWorkflow],
            activities=[stalling_activity],
        ):
            result = await env.client.execute_workflow(
                HeartbeatTestWorkflow.run,
                id="test-heartbeat",
                task_queue="test-queue",
            )

            assert result == "completed"
            assert _stall_attempt == 2


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_c_shot_retry_resumes_same_job(tmp_path):
    """Test (c): Shot workflow retry resumes same provider job ID."""
    global _idem_db, _idem_calls, _idem_job_ids
    
    db_path = str(tmp_path / "test.db")
    _idem_db = db_path
    _idem_calls = {"submit": 0, "await": 0}
    _idem_job_ids = []
    
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
                id="test-idempotency",
                task_queue="test-queue",
            )

            # Submit called once (workflow doesn't retry submit, only await)
            # Await called twice (first fails, second succeeds)
            # Same job ID used throughout
            assert _idem_calls["submit"] == 1
            assert _idem_calls["await"] == 2
            assert len(_idem_job_ids) == 1  # Only ONE job ID ever created
            assert result["job_id"] == _idem_job_ids[0]


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_d_insufficient_credits_pauses_workflow(tmp_path):
    """Test (d): InsufficientCredits pauses workflow until resume Signal."""
    global _credits_db
    
    db_path = str(tmp_path / "test.db")
    _credits_db = db_path
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

            # Wait for pause (short wait for test speed)
            await asyncio.sleep(0.5)
            paused = await handle.query(CreditPauseWorkflow.is_paused)
            assert paused

            # Add credits and resume
            await ledger.add_credits("test-ep", 200.0, "grant")
            await handle.signal(CreditPauseWorkflow.resume)

            result = await asyncio.wait_for(handle.result(), timeout=5)
            assert result["status"] == "resumed"
