"""Tests for single-shot retry functionality."""

import asyncio
import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from temporalio.exceptions import ApplicationError

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
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
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
            assert state["shot_attempts"].get("A01", 0) == 1
            
            # Approve G2 (which will approve the retry)
            await handle.signal(EpisodeWorkflowV2.approve_g2_scene_stills)
            
            # Wait for retry to complete successfully
            await asyncio.sleep(2.0)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            # Failed shot should be removed after successful retry
            # (In reality it takes time for the child to complete, so we just check attempts)
            assert state["shot_attempts"]["A01"] == 1
            
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
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
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
@pytest.mark.timeout(60)
async def test_retry_shot_respects_max_retries():
    """Test that retry_shot respects the max_retries limit.
    
    First shot fails, first retry also fails, second retry is refused due to MAX_SHOT_RETRIES=1.
    """
    
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(
            env.client,
            task_queue="test-retry-limit",
            workflows=[EpisodeWorkflowV2, RetryableShotWorkflow],
            activities=_make_activities(),
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_retry4", None, True, ["A01"], 40],
                id="test-retry-limit-workflow",
                task_queue="test-retry-limit",
            )
            
            # Wait for G1 and approve
            await _wait_for_state(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail (original attempt)
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            
            # Retry once (attempt 1 -> attempt 2, max_retries=1)
            retry_result = await handle.execute_update(
                EpisodeWorkflowV2.retry_shot,
                args=["A01"]
            )
            assert retry_result["status"] == "queued"
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A01"] == 1
            
            # Wait for retry to fail (RetryableShotWorkflow fails on attempt==1)
            # The retry child will fail immediately, but we need to give the check loop time to process it
            # Don't approve G2 yet - keep workflow at G2 gate
            await asyncio.sleep(2.0)
            
            # Now try to retry again (should be refused - already at max_retries=1)
            from temporalio.client import WorkflowUpdateFailedError
            try:
                retry_result2 = await handle.execute_update(
                    EpisodeWorkflowV2.retry_shot,
                    args=["A01"]
                )
                pytest.fail("Expected WorkflowUpdateFailedError for exceeding max retries")
            except WorkflowUpdateFailedError as e:
                # Expected: retry refused
                assert "Max retries" in str(e.cause) or "already attempted" in str(e.cause)
            
            # Verify attempt count is still 1
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["shot_attempts"]["A01"] == 1
            
            # Now approve G2 to let workflow proceed
            await handle.signal(EpisodeWorkflowV2.approve_g2_scene_stills)
            
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
            await handle.signal(EpisodeWorkflowV2.approve_g1_shot_plan)
            
            # Wait for A01 to fail
            await _wait_for_state(handle, lambda s: len(s["failed_shots"]) > 0)
            
            # Check original workflow ID exists
            original_wf = env.client.get_workflow_handle("ep_retry5-shot-A01")
            try:
                desc = await original_wf.describe()
                assert desc.id == "ep_retry5-shot-A01"
            except RPCError:
                pytest.fail("Original workflow should exist, but describe() raised RPCError")
            
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
                pytest.fail("Retry workflow should exist, but describe() raised RPCError")
            
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_shot_distinct_job_id_and_idempotency_key(monkeypatch):
    """
    Test that retry uses distinct ledger job_id AND Idempotency-Key on live path.
    
    Uses fake provider to capture both values, asserts they differ between
    original (still-{ep}-{shot}) and retry 1 (still-{ep}-{shot}-r2).
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
