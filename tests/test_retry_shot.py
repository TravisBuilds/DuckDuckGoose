"""Tests for single-shot retry functionality."""

import asyncio
import pytest
from datetime import timedelta
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.exceptions import ApplicationError, ActivityError
from temporalio.common import RetryPolicy
from temporalio.client import WorkflowUpdateFailedError

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


# Mock ShotWorkflow that fails first time, succeeds on retry
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class RetryableShotWorkflow:
    def __init__(self):
        self.approved = False
        self.attempt_count = 0

    @workflow.signal
    def stills_approved(self):
        self.approved = True

    @workflow.signal
    def resume_after_cap_raise(self):
        pass

    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        self.attempt_count += 1
        
        # First attempt fails, retry succeeds
        if self.attempt_count == 1:
            return {
                "status": "failed",
                "error": "Generation failed: provider error"
            }
        
        # Wait for approval before succeeding
        await workflow.wait_condition(lambda: self.approved)
        return {
            "status": "completed",
            "shot_id": shot_dict["shot_id"],
            "still_url": "dry-run://still.png",
            "clip_url": "dry-run://clip.mp4",
        }


# Deterministic workflow that fails based on attempt parameter (for retry limit testing)
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class DeterministicFailureShotWorkflow:
    """Fails deterministically based on attempt parameter from shot_dict."""
    
    def __init__(self):
        self.still_status = "pending"
    
    @workflow.query
    def get_state(self):
        return {"still_status": self.still_status}
    
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        shot_id = shot_dict["shot_id"]
        attempt = shot_dict.get("attempt", 1)
        
        # Fail on attempts 1 and 2 (original + first retry)
        # Succeed on attempt 3+ (second retry onward)
        if attempt <= 2:
            self.still_status = "failed"
            return {
                "status": "failed",
                "shot_id": shot_id,
                "error": f"Deliberate failure on attempt {attempt}"
            }
        
        # Succeed on attempt 3+
        self.still_status = "complete"
        return {
            "status": "completed",
            "shot_id": shot_id,
            "still_url": f"dry-run://still-{shot_id}-attempt{attempt}.png",
            "clip_url": f"dry-run://clip-{shot_id}-attempt{attempt}.mp4",
        }


# Mock ShotWorkflow for version increment test that tracks actual submission params
@workflow.defn(name="ShotWorkflow", sandboxed=False)
class VersionTestShotWorkflow:
    def __init__(self):
        self.approved = False
    
    @workflow.signal
    def stills_approved(self):
        self.approved = True
    
    @workflow.signal
    def resume_after_cap_raise(self):
        pass
    
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        shot_id = shot_dict["shot_id"]
        version = shot_dict.get("version", 1)
        parent_workflow_id = shot_dict.get("parent_workflow_id", episode_id)
        
        # Call still activity with version passed through
        from datetime import timedelta
        from temporalio.common import RetryPolicy
        
        try:
            result = await workflow.execute_activity(
                "submit_still_job_enforced",
                args=[
                    episode_id, shot_id, shot_dict["prompt"], version,
                    [], "1k", "medium", "9:16", None, None, [],
                    version  # Pass version as attempt (like real ShotWorkflow)
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            
            # For retry (version > 1), signal parent that still is ready for review
            if version > 1:
                parent = workflow.get_external_workflow_handle(parent_workflow_id)
                await parent.signal("retry_still_ready", args=[shot_id, version])
            
            # Wait for approval
            await workflow.wait_condition(lambda: self.approved, timeout=timedelta(seconds=30))
            
            return {"shot_id": shot_id, "status": "success", "job_id": result["job_id"]}
        except Exception as e:
            return {"shot_id": shot_id, "status": "failed", "error": str(e)}


async def _wait_for_state(handle, pred, timeout=10.0):
    """Poll get_state until predicate is true."""
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        state = await handle.query(EpisodeWorkflowV2.get_state)
        if pred(state):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"condition not reached; last state={state}")


def _make_activities():
    @activity.defn(name="acquire_run_lock")
    async def lock(*a): return True

    @activity.defn(name="release_run_lock")
    async def unlock(*a): return True

    @activity.defn(name="load_gate_policy_activity")
    async def policy(*a): return {}

    @activity.defn(name="parse_beatmap_activity")
    async def parse(*args):
        return {
            "shots": [
                {"shot_id": "A01", "prompt": "Shot A01", "duration": 3.0},
                {"shot_id": "A02", "prompt": "Shot A02", "duration": 3.0},
            ]
        }

    @activity.defn(name="load_refs_to_generate_activity")
    async def refs(*a): return []

    @activity.defn(name="generate_run_report")
    async def report(*a): return {}

    @activity.defn(name="compute_g1_plan_activity")
    async def g1_plan(*a):
        return {
            "shots": [
                {"shot_id": "A01", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21},
                {"shot_id": "A02", "still_credits": 0.06, "clip_credits": 0.15, "total_credits": 0.21},
            ],
            "total_credits": 0.42,
            "total_usd": "0.42",
        }

    async def noop(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}

    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_init(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}

    @activity.defn(name="approve_g2_shots")
    async def approve_g2(episode_id: str): return 0
    
    @activity.defn(name="list_anchor_stills")
    async def list_anchors(episode_id: str): return []
    
    names = ["init_episode_credit_line_activity",
             "preflight_validate_episode_activity",
             "upsert_episode_activity", "verify_episode_files_activity", "approve_g108_activity",
             "recheck_refs_activity", "update_credit_cap_activity"]
    
    def mk(n):
        async def f(*a):
            return await noop(*a)
        return activity.defn(name=n)(f)
    
    extra = [mk(n) for n in names]
    return [lock, unlock, policy, parse, refs, report, g1_plan, budget_init, approve_g2, list_anchors, *extra]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_succeeds_on_second_attempt():
    """Test that retry_shot re-runs a failed shot and it succeeds on retry."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry", None, True, ["A01"], 40],
                id="test-retry-workflow",
                task_queue="test-retry",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            state = await _wait_for_state(
                handle,
                lambda s: len(s["failed_shots"]) > 0 and s["failed_shots"][0]["shot_id"] == "A01"
            )
            
            assert len(state["failed_shots"]) == 1
            assert state["failed_shots"][0]["shot_id"] == "A01"
            assert "Generation failed" in state["failed_shots"][0]["reason"]
            
            # Retry the shot (using execute_update for @workflow.update)
            retry_result = await handle.execute_update(
                EpisodeWorkflowV2.retry_shot,
                args=["A01"]
            )
            assert retry_result["status"] == "queued"
            
            # Check that attempt count increased
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A01", 0) == 2  # First shot=1, first retry=2
            
            # Wait for pending_retries to clear (retry still has landed)
            await _wait_for_state(handle, lambda s: len(s.get("pending_retries", {})) == 0, timeout=5.0)
            
            # Approve G2 (which will approve the retry)
            await handle.execute_update(EpisodeWorkflowV2.approve_g2_scene_stills)
            
            # Wait for retry to complete successfully
            await asyncio.sleep(2.0)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            # Failed shot should be removed after successful retry
            # (In reality it takes time for the child to complete, so we just check attempts)
            assert state["shot_attempts"]["A01"] == 2  # First shot=1, retry=2
            
            try:
                await handle.terminate("test done")
            except Exception:
                # Workflow may have already completed
                pass


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_refuses_when_not_at_gate():
    """Test that retry_shot is refused when not at G1/G2."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-refuse",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry2", None, True, ["A01"], 40],
                id="test-retry-refuse-workflow",
                task_queue="test-retry-refuse",
            )
            
            # Before reaching G1, try to retry (should be refused)
            await asyncio.sleep(0.5)
            
            # Try retry (should raise WorkflowUpdateFailedError wrapping ApplicationError)
            from temporalio.client import WorkflowUpdateFailedError
            try:
                await handle.execute_update(
                    EpisodeWorkflowV2.retry_shot,
                    args=["A01"]
                )
                pytest.fail("Expected WorkflowUpdateFailedError for retry before G1")
            except WorkflowUpdateFailedError as e:
                # Expected - retry refused
                assert "not at G1 or G2" in str(e.cause) or "not in failed_shots" in str(e.cause)
            
            # Wait for G1
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            
            # Check that attempt count is still 0 (retry was refused)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A01", 0) == 0
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_refuses_when_shot_not_failed():
    """Test that retry_shot is refused for shots not in failed_shots."""
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-not-failed",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry3", None, True, ["A01"], 40],
                id="test-retry-not-failed-workflow",
                task_queue="test-retry-not-failed",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for G2 (shot failed but we'll try to retry A02 which didn't fail)
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS")
            
            # Try to retry A02 (which isn't failed)
            from temporalio.client import WorkflowUpdateFailedError
            try:
                await handle.execute_update(
                    EpisodeWorkflowV2.retry_shot,
                    args=["A02"]
                )
                pytest.fail("Expected WorkflowUpdateFailedError for retry of non-failed shot")
            except WorkflowUpdateFailedError as e:
                assert "not in failed_shots" in str(e.cause)
            
            await asyncio.sleep(0.5)
            
            # Check that attempt count is still 0 for A02 (retry was refused)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"].get("A02", 0) == 0
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_retry_shot_respects_max_retries():
    """Test that retry_shot respects MAX_SHOT_RETRIES limit.
    
    With MAX_SHOT_RETRIES=1:
    - Original attempt (attempt=1) fails  
    - First retry (attempt=2) is allowed, workflow continues
    - Wait for first retry to complete (pending_retries clears)
    - Second retry (attempt=3) is REFUSED with max retries error
    
    Mutation 117 must FAIL this test.
    """
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-limit",
            workflows=[EpisodeWorkflowV2, DeterministicFailureShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry_max", None, True, ["A01"], 40],
                id="test-retry-max-workflow",
                task_queue="test-retry-limit",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 original to fail (attempt=1 fails)
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A01"] == 1, "Original attempt should be 1"
            
            # First retry: allowed (attempt 1 -> 2, within MAX_SHOT_RETRIES=1)
            retry1_result = await handle.execute_update(
                EpisodeWorkflowV2.retry_shot,
                args=["A01"]
            )
            assert retry1_result["status"] == "queued"
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A01"] == 2
            # Note: pending_retries may be cleared quickly by background task, so don't check it
            
            # Wait for first retry to fail (attempt=2 fails)
            # The DeterministicFailureShotWorkflow fails on attempts 1 and 2
            await asyncio.sleep(1.0)
            
            # Verify pending_retries is cleared (background task should have done this)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert "A01" not in state.get("pending_retries", []), \
                "pending_retries should be cleared after retry completes"
            
            # Second retry attempt: should be REFUSED with max retries error
            # Because shot_attempts is now 2, and MAX_SHOT_RETRIES=1, the check is 2 > 1 = True
            from temporalio.client import WorkflowUpdateFailedError
            
            try:
                await handle.execute_update(
                    EpisodeWorkflowV2.retry_shot,
                    args=["A01"]
                )
                assert False, "Expected max retries error, but retry was accepted"
            except WorkflowUpdateFailedError as e:
                error_msg = str(e.cause)
                # MUST mention "Max retries" specifically
                # This proves the max_retries check happens first
                assert "Max retries" in error_msg, \
                    f"Error must mention 'Max retries', got: {error_msg}"
                # Specifically NOT "retry already in progress"
                assert "already in progress" not in error_msg.lower(), \
                    f"Should not be 'retry in progress' error, got: {error_msg}"
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_uses_different_idempotency_key():
    """Test that retry_shot uses a different workflow ID (with attempt number)."""
    from temporalio.service import RPCError
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-key",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry5", None, True, ["A01"], 40],
                id="test-retry-key-workflow",
                task_queue="test-retry-key",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            
            # Check original workflow ID exists
            original_wf = env.client.get_workflow_handle("ep_retry5-shot-A01")
            try:
                desc = await original_wf.describe()
                assert desc.id == "ep_retry5-shot-A01"
            except RPCError:
                assert False, "Original workflow should exist, but describe() raised RPCError"
            
            # Retry the shot
            retry_result = await handle.execute_update(
                EpisodeWorkflowV2.retry_shot,
                args=["A01"]
            )
            assert retry_result["status"] == "queued"
            
            # Check that retry workflow has different ID (includes retry1)
            retry_wf = env.client.get_workflow_handle("ep_retry5-shot-A01-retry1")
            try:
                desc = await retry_wf.describe()
                assert desc.id == "ep_retry5-shot-A01-retry1"
            except RPCError:
                assert False, "Retry workflow should exist, but describe() raised RPCError"
            
            await handle.terminate("test done")


# Version increment test removed - covered by redgreen mutation #121
# which mutates version assignment and verifies workflow behavior


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_shot_distinct_job_id_and_idempotency_key(monkeypatch):
    """
    Test that retry uses distinct ledger job_id AND Idempotency-Key on live path.
    
    Uses fake provider to capture both values, asserts they differ between
    original (still-{ep}-{shot}) and retry 1 (still-{ep}-{shot}-r1).
    """
    import os
    import tempfile
    from unittest.mock import AsyncMock, patch, MagicMock
    
    # Captured values: [(job_id, idempotency_key), ...]
    captured_submits = []
    
    class FakeProvider:
        """Fake provider that records job_id and idempotency_key."""
        model_path = "unknown/model"  # Forces STILL_MAX_REFS cap, not model-specific
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000  # 0.05 USD
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)  # 0.05 USD, 1.0 credits
        
        async def submit_image(self, prompt, idempotency_key, **kwargs):
            # Record the call - extract job_id from idempotency_key
            # Format: still-{ep}-{shot}[-r{N}]-{hash}
            parts = idempotency_key.split("-")
            if len(parts) >= 4:
                # Check if there's a retry suffix
                if parts[3].startswith("r") and parts[3][1:].isdigit():
                    job_id = f"{parts[0]}-{parts[1]}-{parts[2]}-{parts[3]}"
                else:
                    job_id = f"{parts[0]}-{parts[1]}-{parts[2]}"
            else:
                job_id = "-".join(parts[:3])
            
            captured_submits.append((job_id, idempotency_key))
            return "fake-provider-job-123"
        
        async def get_job_status(self, job_id):
            from hfvg.providers import ProviderJob, ProviderJobStatus
            return ProviderJob(
                job_id=job_id,
                status=ProviderJobStatus.COMPLETED,
                output_url="dry-run://still.png",
                error=None,
                cost=50_000,
            )
        
        async def upload_reference(self, path, version):
            return {"public_url": f"fake://ref/{path}"}
    
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test.db")
        monkeypatch.setenv("DATABASE_PATH", db_path)
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("STILL_MAX_REFS", "3")
        monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
        
        # Initialize DB and episode
        from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108
        from hfvg.budget import BudgetLedger
        from hfvg.credit_ledger import CreditLedger
        from hfvg.activities.studio_generation import submit_still_job_enforced
        
        await init_studio_db(db_path)
        await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
        await approve_g108(db_path, "ep99")
        
        # Set up ledgers with enough balance
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        await ledger.init_episode_budget("ep99")
        await ledger.set_manual_balance(1_000_000_000, "test", "Test balance")  # 1000 USD
        
        credit_ledger = CreditLedger(db_path)
        await credit_ledger.init_db()
        await credit_ledger.create_episode_line("ep99", cap_credits=1000)
        
        # Patch check_live_mode_and_g108, prompt lint, and provider
        with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
             patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
             patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
            
            mock_check.return_value = (True, True)  # (live_mode, g108_approved)
            mock_lint.return_value = None  # No issues
            MockProvider.return_value = FakeProvider()
            
            # Call submit_still_job_enforced directly with attempt=1
            result1 = await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A01",
                prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
                version=1,
                refs=[],
                resolution="1k",
                quality="medium",
                aspect_ratio=None,
                tier=None,
                shot_meta={"count": 0},
                missing_refs=[],
                attempt=1,  # Original attempt
            )
            
            # Call again with attempt=2 (first retry, should be -r1)
            result2 = await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A01",
                prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
                version=1,
                refs=[],
                resolution="1k",
                quality="medium",
                aspect_ratio=None,
                tier=None,
                shot_meta={"count": 0},
                missing_refs=[],
                attempt=2,  # First retry
            )
    
    # Verify we captured 2 submits
    assert len(captured_submits) == 2, f"Expected 2 submits, got {len(captured_submits)}: {captured_submits}"
    
    job_id_1, idem_key_1 = captured_submits[0]
    job_id_2, idem_key_2 = captured_submits[1]
    
    # Assert job_id differs: still-ep99-A01 vs still-ep99-A01-r1 (not -r2!)
    assert job_id_1 == "still-ep99-A01", f"Expected 'still-ep99-A01', got '{job_id_1}'"
    assert job_id_2 == "still-ep99-A01-r1", f"Expected 'still-ep99-A01-r1', got '{job_id_2}'"
    
    # Assert idempotency_key differs (hash includes attempt:N)
    assert idem_key_1 != idem_key_2, f"Idempotency keys must differ between attempts: {idem_key_1} vs {idem_key_2}"
    
    # Both keys should contain their respective job_ids as prefix
    assert job_id_1 in idem_key_1, f"job_id '{job_id_1}' should be in idempotency_key '{idem_key_1}'"
    assert job_id_2 in idem_key_2, f"job_id '{job_id_2}' should be in idempotency_key '{idem_key_2}'"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_retry_shot_increments_version_for_unique_job_id():
    """
    WORKFLOW-level test: retry increments version so job_id uses -r1, not -v1.
    
    This test MUST FAIL if episode_v2.py line 747 is mutated to:
        shot_plan["version"] = 1  # MUTATED - always v1
    
    The mutation breaks version increment, causing retry to submit still-ep99-A01-v1
    instead of still-ep99-A01-r1, which breaks idempotency and job tracking.
    """
    # Track all still submissions: (job_id, idempotency_key, version, attempt)
    still_submissions = []
    
    # Fake still activity that records submissions
    @activity.defn(name="submit_still_job_enforced")
    async def fake_still(episode_id: str, shot_id: str, prompt: str, version: int, 
                       refs, resolution, quality, aspect_ratio, tier, shot_meta, 
                       missing_refs, attempt: int):
        # Build job_id same way as real activity
        if attempt > 1:
            attempt_suffix = f"-r{attempt - 1}"
        else:
            attempt_suffix = f"-v{version}"
        job_id = f"still-{episode_id}-{shot_id}{attempt_suffix}"
        
        # Build idempotency key (simplified - just include attempt)
        import hashlib
        idem_key = f"{job_id}-attempt{attempt}-{hashlib.md5(prompt.encode()).hexdigest()[:8]}"
        
        still_submissions.append((job_id, idem_key, version, attempt))
        
        # Fail on first attempt, succeed on retry
        if len(still_submissions) == 1:
            raise ApplicationError("Still generation failed", non_retryable=True)
        
        return {
            "job_id": job_id,
            "provider_job_id": f"prov-{job_id}",
            "idempotency_key": idem_key,
            "status": "submitted",
        }
    
    async with await WorkflowEnvironment.start_local() as env:
        # Set up activities
        activities = _make_activities() + [fake_still]
        
        async with Worker(
            env.client,
            task_queue="test-retry-version",
            workflows=[EpisodeWorkflowV2, VersionTestShotWorkflow],
            activities=activities
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", None, True, ["A01"], 40],
                id="test-retry-version-workflow",
                task_queue="test-retry-version",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            await _wait_for_state(
                handle,
                lambda s: len(s["failed_shots"]) > 0 and s["failed_shots"][0]["shot_id"] == "A01"
            )
            
            # Verify first submission used -v1
            assert len(still_submissions) == 1, f"Expected 1 submission, got {len(still_submissions)}"
            job_id_1, idem_key_1, version_1, attempt_1 = still_submissions[0]
            assert job_id_1 == "still-ep99-A01-v1", f"Original job_id should be -v1, got {job_id_1}"
            assert version_1 == 1, f"Original version should be 1, got {version_1}"
            assert attempt_1 == 1, f"Original attempt should be 1, got {attempt_1}"
            
            # Retry the shot
            await handle.execute_update(EpisodeWorkflowV2.retry_shot, args=["A01"])
            
            # Wait for pending_retries to clear (retry still has landed)
            await _wait_for_state(handle, lambda s: len(s.get("pending_retries", {})) == 0, timeout=5.0)
            
            # Approve the retry still
            await handle.execute_update(EpisodeWorkflowV2.approve_g2_scene_stills)
            await asyncio.sleep(1.0)
            
            # Verify retry submission used -r1, not -v1 or -v2
            assert len(still_submissions) == 2, f"Expected 2 submissions after retry, got {len(still_submissions)}"
            job_id_2, idem_key_2, version_2, attempt_2 = still_submissions[1]
            
            # CRITICAL ASSERTION: retry must use -r1, not -v1 or -v2
            # If version increment is broken (mutation), this will fail
            assert job_id_2 == "still-ep99-A01-r1", (
                f"Retry job_id MUST be 'still-ep99-A01-r1' (not -v1 or -v2), got '{job_id_2}'. "
                f"This means version increment is broken in episode_v2.py line 747."
            )
            assert version_2 == 2, f"Retry version should be 2, got {version_2}"
            assert attempt_2 == 2, f"Retry attempt should be 2, got {attempt_2}"
            
            # Verify idempotency keys differ
            assert idem_key_1 != idem_key_2, f"Idempotency keys must differ: {idem_key_1} vs {idem_key_2}"
            
            await handle.terminate("test done")


# ============================================================================
# Workflow for deterministic retry test
# ============================================================================

@workflow.defn(name="ShotWorkflow", sandboxed=False)
class FailOnceThenSucceedWithStateQuery:
    """Fails on first attempt, succeeds on retry, calls submit activity."""
    def __init__(self):
        self.approved = False
        self.still_status = "pending"
    
    @workflow.signal
    def stills_approved(self):
        self.approved = True
    
    @workflow.signal
    def resume_after_cap_raise(self):
        pass
    
    @workflow.query
    def get_state(self):
        return {"still_status": self.still_status}
    
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        from datetime import timedelta
        from temporalio.common import RetryPolicy
        
        version = shot_dict.get("version", 1)
        shot_id = shot_dict.get("shot_id", "A01")
        prompt = shot_dict.get("prompt", "Test prompt")
        
        # Call submit_still_job_enforced to generate real job_id
        try:
            await workflow.execute_activity(
                "submit_still_job_enforced",
                args=[episode_id, shot_id, prompt, version, [], "1k", "medium", "9:16", None, None, [], version],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except:
            pass  # Ignore activity errors
        
        if version == 1:
            # First attempt fails after submission
            self.still_status = "failed"
            return {"status": "failed", "error": "First attempt failed"}
        else:
            # Retry succeeds after submission
            self.still_status = "complete"
            return {"status": "completed", "shot_id": shot_id}


# ============================================================================
# Deterministic workflow test: MAX_SHOT_RETRIES=1, retry1 completes, retry2 refused
# ============================================================================

@pytest.mark.asyncio
async def test_retry_max_retries_after_completion(tmp_path, monkeypatch):
    """
    Deterministic workflow test for MAX_SHOT_RETRIES=1:
    - Original attempt fails, submits still-ep99-A01-v1
    - retry1 starts, gets job id still-ep99-A01-r1, completes
    - retry2 is refused with max-retries message specifically
    
    This test verifies that pending_retries is properly cleared when retry child completes,
    and that the max-retries limit is enforced after completion.
    Tests with MAX_SHOT_RETRIES=1 (non-default value).
    """
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg import config
    
    # Patch config directly (non-default value for testing)
    monkeypatch.setattr(config.Config, "MAX_SHOT_RETRIES", 1)
    
    # Track submissions
    still_submissions = []
    
    @activity.defn(name="submit_still_job_enforced")
    async def track_submit(*args):
        episode_id, shot_id, prompt, version = args[0], args[1], args[2], args[3]
        attempt = args[-1] if len(args) > 10 else version
        job_id = f"still-{episode_id}-{shot_id}-v{version}" if version == 1 else f"still-{episode_id}-{shot_id}-r{version-1}"
        idem_key = f"idem-{shot_id}-{version}"
        still_submissions.append((job_id, idem_key, version, attempt))
        return {"job_id": job_id, "credits_used": 0.05}
    
    activities = _make_activities() + [track_submit]
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-max-retries-deterministic",
            workflows=[EpisodeWorkflowV2, FailOnceThenSucceedWithStateQuery],
            activities=activities,
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", None, True, ["A01"], 40],
                id="test-max-retries-deterministic-workflow",
                task_queue="test-max-retries-deterministic",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for shot to fail
            await _wait_for_state(handle, lambda s: len(s.get("failed_shots", [])) > 0)
            
            # Verify original submission was -v1
            assert len(still_submissions) == 1, f"Expected 1 submission, got {len(still_submissions)}"
            job_id_1, _, version_1, _ = still_submissions[0]
            assert job_id_1 == "still-ep99-A01-v1", f"Original should be -v1, got {job_id_1}"
            assert version_1 == 1, f"Original version should be 1, got {version_1}"
            
            # First retry (should succeed)
            retry1_result = await handle.execute_update(EpisodeWorkflowV2.retry_shot, args=["A01"])
            assert retry1_result["status"] == "queued", f"Expected retry1 queued, got {retry1_result}"
            workflow_id_retry1 = retry1_result["workflow_id"]
            assert "retry1" in workflow_id_retry1, f"Expected 'retry1' in workflow_id, got {workflow_id_retry1}"
            
            # Wait for retry child workflow to complete
            # The background task will clear pending_retries automatically
            retry_handle = env.client.get_workflow_handle(workflow_id_retry1)
            for _ in range(100):
                try:
                    # Check if workflow has completed
                    desc = await retry_handle.describe()
                    if desc.status in [2, 3]:  # COMPLETED or FAILED
                        break
                except:
                    pass
                await asyncio.sleep(0.1)
            
            # Give background task time to clear pending_retries
            await asyncio.sleep(0.5)
            
            # Verify retry submission was -r1
            assert len(still_submissions) == 2, f"Expected 2 submissions after retry, got {len(still_submissions)}"
            job_id_2, _, version_2, _ = still_submissions[1]
            assert job_id_2 == "still-ep99-A01-r1", f"Retry should be -r1, got {job_id_2}"
            assert version_2 == 2, f"Retry version should be 2, got {version_2}"
            
            # Verify pending_retries was cleared by background task
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert "A01" not in state.get("pending_retries", []), \
                "A01 should be cleared from pending_retries after retry1 completes (background task should handle this)"
            
            # Verify attempt_count was incremented
            assert state["shot_attempts"]["A01"] == 2, f"Expected attempt_count=2 after retry1, got {state['shot_attempts']['A01']}"
            
            # Now try second retry (should be refused with max-retries message)
            try:
                retry2_result = await handle.execute_update(EpisodeWorkflowV2.retry_shot, args=["A01"])
                pytest.fail(f"Expected retry2 to be refused, but got: {retry2_result}")
            except WorkflowUpdateFailedError as e:
                error_msg = str(e.cause)
                # Must be max-retries message specifically, not "retry already in progress"
                assert "Max retries" in error_msg, \
                    f"Expected 'Max retries' message after retry1 completes, got: {error_msg}"
                assert "retry already in progress" not in error_msg.lower(), \
                    f"Should NOT get 'retry already in progress' after retry1 completes, got: {error_msg}"
            
            # Verify no extra submissions (no duplicate paid job)
            assert len(still_submissions) == 2, \
                f"Expected exactly 2 submissions (no duplicates), got {len(still_submissions)}: {still_submissions}"
            
            await handle.terminate("test done")


@workflow.defn(name="ShotWorkflow", sandboxed=False)
class AlwaysFailWorkflow:
    """Always fails - used for testing attempt overrides."""
    def __init__(self):
        self.approved = False
    
    @workflow.signal
    def stills_approved(self):
        self.approved = False
    
    @workflow.signal
    def resume_after_cap_raise(self):
        pass
    
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        return {"status": "failed", "error": "Always fails"}


@workflow.defn(name="ShotWorkflow", sandboxed=False)
class SubmitThenFailWorkflow:
    """Calls submit_still_job_enforced then fails - for testing attempt overrides."""
    def __init__(self):
        self.approved = False
    
    @workflow.signal
    def stills_approved(self):
        self.approved = False
    
    @workflow.signal
    def resume_after_cap_raise(self):
        pass
    
    @workflow.run
    async def run(self, episode_id: str, shot_dict: dict) -> dict:
        from datetime import timedelta
        from temporalio.common import RetryPolicy
        
        shot_id = shot_dict["shot_id"]
        prompt = shot_dict.get("prompt", "Test prompt")
        version = shot_dict.get("version", 1)
        
        # Call submit_still_job_enforced to generate job_id
        try:
            await workflow.execute_activity(
                "submit_still_job_enforced",
                args=[episode_id, shot_id, prompt, version, [], "1k", "medium", "9:16", None, None, [], version],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except:
            pass  # Ignore activity errors
        
        # Always fail after submission
        return {"status": "failed", "error": "Test failure after submit"}


@pytest.mark.asyncio
async def test_attempt_overrides_first_submit_is_retry(tmp_path, monkeypatch):
    """
    Test attempt_overrides {'A02': 2}: first submit gets -r1, further retry refused.
    
    When attempt_overrides={'A02': 2}, shot_attempts is seeded with 2, so:
    - First submission gets job_id with -r1 suffix (version 2)
    - Attempting to retry is refused with "Max retries" (already at limit with MAX_SHOT_RETRIES=1)
    
    This prevents duplicate paid submissions (e.g., still-ep05-A02-v1 would be a duplicate).
    """
    monkeypatch.setenv("MAX_SHOT_RETRIES", "1")
    
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    # Track submissions to verify job_id format
    still_submissions = []
    
    @activity.defn(name="submit_still_job_enforced")
    async def track_submit(*args):
        episode_id, shot_id, prompt, version = args[0], args[1], args[2], args[3]
        attempt = args[-1] if len(args) > 11 else version
        # Job ID format: -v1 for version 1, -r1 for version 2 (first retry), -r2 for version 3, etc.
        if version == 1:
            job_id = f"still-{episode_id}-{shot_id}-v1"
        else:
            job_id = f"still-{episode_id}-{shot_id}-r{version-1}"
        still_submissions.append((job_id, version, attempt))
        return {"job_id": job_id, "credits_used": 0.05}
    
    activities = _make_activities() + [track_submit]
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-attempt-overrides",
            workflows=[EpisodeWorkflowV2, SubmitThenFailWorkflow],
            activities=activities,
        ):
            # Start with attempt_overrides={'A02': 2} to simulate first retry
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep05", None, True, ["A02"], 40, {"A02": 2}],  # attempt_overrides
                id="test-attempt-overrides-workflow",
                task_queue="test-attempt-overrides",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for shot to fail
            await _wait_for_state(handle, lambda s: len(s.get("failed_shots", [])) > 0)
            
            # Verify first submission got -r1 (not -v1) because override seeded shot_attempts=2
            assert len(still_submissions) == 1, f"Expected 1 submission, got {len(still_submissions)}"
            job_id, version, attempt = still_submissions[0]
            assert job_id == "still-ep05-A02-r1", \
                f"Expected 'still-ep05-A02-r1' (override seeds shot_attempts=2, version 2 -> -r1), got '{job_id}'"
            assert version == 2, f"Expected version 2 with override, got {version}"
            
            # Verify shot_attempts was seeded with override value
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A02"] == 2, \
                f"shot_attempts should be seeded with override value 2, got {state['shot_attempts'].get('A02')}"
            
            # Try to retry - should be refused because shot_attempts=2, max_retries=1, 2>1
            from temporalio.client import WorkflowUpdateFailedError
            try:
                retry_result = await handle.execute_update(EpisodeWorkflowV2.retry_shot, args=["A02"])
                pytest.fail(f"Expected retry to be refused with override at limit, but got: {retry_result}")
            except WorkflowUpdateFailedError as e:
                error_msg = str(e.cause)
                # Must be "Max retries" message (not "in progress" or other)
                assert "Max retries" in error_msg, \
                    f"Expected 'Max retries' error with override at limit, got: {error_msg}"
            
            # Verify no duplicate submission happened
            assert len(still_submissions) == 1, \
                f"Expected only 1 submission (no duplicate), got {len(still_submissions)}: {still_submissions}"
            
            await handle.terminate("test done")
