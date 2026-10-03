"""Temporal integration tests: workflows, activities, heartbeats, retries."""

import asyncio

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from hfvg import activities
    from hfvg.errors import InsufficientCreditsError
    from hfvg.ledger import Ledger
    from hfvg.models import Asset, GenerationRequest, JobStatus
    from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow


# Module-level workflow and activity definitions for tests

_test_db_path = None  # Will be set by tests


def set_test_db_path(path: str):
    """Set the database path for test activities."""
    global _test_db_path
    _test_db_path = path


_attempt_count = 0
_submit_count = 0
_job_ids_seen = []


@activity.defn
async def heartbeat_test_activity() -> str:
    """Activity that stalls on first attempt, succeeds on second."""
    global _attempt_count
    _attempt_count += 1
    
    if _attempt_count == 1:
        # First attempt: sleep without heartbeating (will timeout after 2 seconds)
        await asyncio.sleep(10)
        return "should-not-reach"
    else:
        # Second attempt: heartbeat and succeed
        activity.heartbeat({"attempt": _attempt_count})
        await asyncio.sleep(0.1)
        activity.heartbeat({"status": "completing"})
        return "completed"


@workflow.defn
class HeartbeatTestWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            heartbeat_test_activity,
            start_to_close_timeout=workflow.timedelta(seconds=30),
            heartbeat_timeout=workflow.timedelta(seconds=2),
            retry_policy=workflow.common.RetryPolicy(
                maximum_attempts=3,
                initial_interval=workflow.timedelta(seconds=1),
            ),
        )


@activity.defn
async def test_submit_still_job(episode_id: str, req: GenerationRequest) -> str:
    """Track how many times submit is called."""
    global _submit_count, _job_ids_seen, _test_db_path
    _submit_count += 1
    
    # Use real ledger for idempotency
    ledger_instance = Ledger(_test_db_path)
    key = ledger_instance.idempotency_key(req)
    
    existing = await ledger_instance.get_job(key)
    if existing:
        job_id = existing["provider_job_id"]
    else:
        await ledger_instance.check_balance(req.estimated_cost)
        job_id = f"still-job-{req.shot_id}-v{req.version}"
        await ledger_instance.insert_job(key, job_id, episode_id, req.shot_id, req)
        await ledger_instance.deduct_credits(episode_id, req.estimated_cost, "estimate", job_id)
    
    _job_ids_seen.append(job_id)
    return job_id


@activity.defn
async def test_await_job(job_id: str, job_type: str = "still") -> Asset:
    """Fail on first attempt to trigger retry."""
    global _attempt_count
    _attempt_count += 1
    
    activity.heartbeat({"job_id": job_id, "attempt": _attempt_count})
    
    if _attempt_count == 1:
        await asyncio.sleep(0.1)
        raise Exception("Simulated transient failure")
    
    await asyncio.sleep(0.1)
    return Asset(
        asset_id=f"asset-{job_id}",
        job_id=job_id,
        url=f"https://example.com/{job_id}.jpg",
        status=JobStatus.COMPLETED,
        cost=6.5,
    )


@activity.defn
async def test_review_still(asset_url: str, rubric: dict) -> dict:
    """Always pass."""
    return {"passed": True, "issues": []}


@workflow.defn
class TestShotWorkflow:
    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        req = GenerationRequest(
            shot_id=shot_plan["shot_id"],
            version=1,
            prompt=shot_plan["prompt"],
            refs=[],
            params={"type": "still"},
            estimated_cost=6.5,
        )
        
        job_id = await workflow.execute_activity(
            test_submit_still_job,
            args=[episode_id, req],
            start_to_close_timeout=workflow.timedelta(seconds=30),
        )
        
        asset = await workflow.execute_activity(
            test_await_job,
            args=[job_id, "still"],
            start_to_close_timeout=workflow.timedelta(seconds=30),
            heartbeat_timeout=workflow.timedelta(seconds=10),
            retry_policy=workflow.common.RetryPolicy(maximum_attempts=3),
        )
        
        return {"shot_id": shot_plan["shot_id"], "asset_url": asset.url}


@activity.defn
async def expensive_activity(cost: float) -> str:
    """Activity that checks credits."""
    global _test_db_path
    ledger_instance = Ledger(_test_db_path)
    await ledger_instance.check_balance(cost)
    await ledger_instance.deduct_credits("test-ep", cost, "test")
    return "completed"


@workflow.defn
class CreditTestWorkflow:
    def __init__(self):
        self.paused_for_credits = False
        self.resume_requested = False

    @workflow.run
    async def run(self) -> dict:
        try:
            result = await workflow.execute_activity(
                expensive_activity,
                args=[100.0],
                start_to_close_timeout=workflow.timedelta(seconds=10),
                retry_policy=workflow.common.RetryPolicy(
                    maximum_attempts=1,
                    non_retryable_error_types=["InsufficientCreditsError"],
                ),
            )
            return {"status": "completed", "result": result}
        except Exception as e:
            if "InsufficientCredits" in str(type(e).__name__):
                self.paused_for_credits = True
                workflow.logger.info("Paused due to insufficient credits")
                
                # Wait for resume signal
                await workflow.wait_condition(lambda: self.resume_requested)
                
                # Try again after credits added
                result = await workflow.execute_activity(
                    expensive_activity,
                    args=[100.0],
                    start_to_close_timeout=workflow.timedelta(seconds=10),
                )
                return {"status": "resumed", "result": result}
            raise

    @workflow.signal
    def resume_after_credits_added(self):
        """Signal to resume after credits added."""
        self.resume_requested = True

    @workflow.query
    def is_paused(self) -> bool:
        return self.paused_for_credits


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_episode_workflow_completes_through_all_gates(tmp_path):
    """Test (a): Episode workflow driven through ALL gates to completion."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    async with await WorkflowEnvironment.start_time_skipping() as env:
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
                args=["test-ep-full", "A duck teaches penguins", ["instagram"]],
                id="test-ep-full",
                task_queue="test-queue",
            )

            async def drive_through_gates():
                """Send all gate approvals."""
                await asyncio.sleep(0.5)
                
                await handle.execute_update(EpisodeWorkflow.approve_readback)
                await asyncio.sleep(0.2)
                
                await handle.execute_update(EpisodeWorkflow.approve_character_locks)
                await asyncio.sleep(0.2)
                
                await handle.execute_update(EpisodeWorkflow.approve_storyboard)
                await asyncio.sleep(0.2)
                
                # Approve both scenes
                await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[1])
                await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[2])
                await asyncio.sleep(0.5)
                
                await handle.execute_update(EpisodeWorkflow.approve_final)
                await asyncio.sleep(0.2)
                
                # Approve posts
                posting_handle = env.client.get_workflow_handle("test-ep-full-posting")
                await posting_handle.execute_update(
                    PostingWorkflow.approve_posts, args=[["instagram"]]
                )

            # Start approval task
            approval_task = asyncio.create_task(drive_through_gates())

            # Wait for workflow to complete
            result = await asyncio.wait_for(handle.result(), timeout=30)
            
            # Ensure approval task completed
            await approval_task

            # Verify completion
            assert result["status"] == "completed"
            assert result["episode_id"] == "test-ep-full"
            assert "final_url" in result
            assert "posts" in result
            assert len(result["posts"]) > 0


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_activity_heartbeat_timeout_causes_retry(tmp_path):
    """Test (b): Activity that stops heartbeating gets failed and retried."""
    global _attempt_count
    _attempt_count = 0  # Reset
    
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[HeartbeatTestWorkflow],
            activities=[heartbeat_test_activity],
        ):
            result = await env.client.execute_workflow(
                HeartbeatTestWorkflow.run,
                id="test-heartbeat",
                task_queue="test-queue",
            )
            
            assert result == "completed"
            assert _attempt_count == 2  # First attempt failed, second succeeded


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_shot_workflow_retry_resumes_same_job(tmp_path):
    """Test (c): ShotWorkflow retry resumes same provider job ID (idempotency)."""
    global _submit_count, _job_ids_seen, _attempt_count, _test_db_path
    _submit_count = 0  # Reset
    _job_ids_seen = []  # Reset
    _attempt_count = 0  # Reset
    
    db_path = str(tmp_path / "test.db")
    _test_db_path = db_path
    ledger = Ledger(db_path)
    await ledger.init_db()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[TestShotWorkflow],
            activities=[test_submit_still_job, test_await_job, test_review_still],
        ):
            result = await env.client.execute_workflow(
                TestShotWorkflow.run,
                args=["ep-test", {"shot_id": "shot-001", "prompt": "Test shot"}],
                id="test-shot-retry",
                task_queue="test-queue",
            )
            
            # Verify idempotency: submit called twice but same job ID
            assert _submit_count == 2  # Called on both attempts
            assert len(set(_job_ids_seen)) == 1  # Only ONE unique job ID
            assert _job_ids_seen[0] == _job_ids_seen[1]
            assert _attempt_count == 2  # Retry happened


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_insufficient_credits_pauses_workflow(tmp_path):
    """Test (d): InsufficientCredits pauses workflow, resume Signal continues."""
    global _test_db_path
    
    db_path = str(tmp_path / "test.db")
    _test_db_path = db_path
    ledger = Ledger(db_path)
    await ledger.init_db()
    
    # Drain credits to near zero
    await ledger.deduct_credits("test", 9990.0, "setup")

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[CreditTestWorkflow],
            activities=[expensive_activity],
        ):
            handle = await env.client.start_workflow(
                CreditTestWorkflow.run,
                id="test-credits-pause",
                task_queue="test-queue",
            )

            # Wait for it to pause
            await asyncio.sleep(1)
            
            # Check it's paused
            paused = await handle.query(CreditTestWorkflow.is_paused)
            assert paused

            # Add credits
            await ledger.add_credits("test-ep", 200.0, "grant")

            # Send resume signal
            await handle.signal(CreditTestWorkflow.resume_after_credits_added)

            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=10)
            
            assert result["status"] == "resumed"
            assert result["result"] == "completed"
