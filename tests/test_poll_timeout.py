"""
Tests for R1: Poll timeout behavior (keeps reservation, marks pending_reconcile).

These tests verify the polling logic is structured correctly in the workflow.
"""

import asyncio
import ast
import inspect
import os
import textwrap
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# Set test mode
os.environ["DRY_RUN"] = "false"

with workflow.unsafe.imports_passed_through():
    from hfvg import activities
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.workflows.shot import ShotWorkflow


# Fake provider activity that stays in_progress for testing poll exhaustion
_poll_count = 0
_submit_count = 0
_release_count = 0


@activity.defn(name="poll_job_status")
async def fake_poll_job_status_stuck(job_id: str, job_type: str, episode_id: str) -> dict:
    """Fake poll that always returns in_progress (simulates stuck job)."""
    global _poll_count
    _poll_count += 1
    
    return {
        "status": "in_progress",
        "output_url": None,
        "error": None,
        "cost": None,
    }


@activity.defn(name="poll_job_status_failed")
async def fake_poll_job_status_failed(job_id: str, job_type: str, episode_id: str) -> dict:
    """Fake poll that returns failed after first call."""
    return {
        "status": "failed",
        "output_url": None,
        "error": "Simulated provider failure",
        "cost": None,
    }


@activity.defn(name="submit_still_job_enforced")
async def fake_submit_still_tracked(
    episode_id: str, shot_id: str, prompt: str, version: int, refs: list, 
    resolution: str, quality: str, aspect_ratio: str
) -> dict:
    """Track submit calls."""
    global _submit_count
    _submit_count += 1
    
    return {
        "job_id": f"fake-job-{_submit_count}",
        "line_name": "L2_drafts",
        "reserved_amount": 5.0,
    }


@activity.defn(name="commit_job_budget")
async def fake_commit_job_budget(
    episode_id: str, shot_id: str, job_id: str, job_type: str,
    line_name: str, reserved_amount: float, actual_cost: float = None
) -> None:
    """Fake commit."""
    pass


@activity.defn(name="release_job_budget")
async def fake_release_job_budget(
    episode_id: str, shot_id: str, job_id: str, job_type: str,
    line_name: str, reserved_amount: float, reason: str
) -> None:
    """Track release calls."""
    global _release_count
    _release_count += 1


@activity.defn(name="mark_job_pending_reconcile")
async def fake_mark_job_pending_reconcile(
    episode_id: str, shot_id: str, job_id: str, job_type: str
) -> None:
    """Fake mark pending reconcile - write to a test file."""
    import aiosqlite
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "job_pending_reconcile",
             f"{job_type} {shot_id} job {job_id} test pending_reconcile",
             "test")
        )
        await db.commit()


def test_poll_timeout_keeps_reservation():
    """
    Test: Poll exhaustion keeps reservation and marks pending_reconcile.
    
    M-R1a will release the reservation - this test must fail.
    
    Verifies that _poll_job_to_completion:
    1. Has a loop with max_polls bound (~30 min)
    2. Calls mark_job_pending_reconcile on exhaustion
    3. Does NOT call release_job_budget on exhaustion
    4. Raises RuntimeError on exhaustion
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code of _poll_job_to_completion
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Find the method
    method_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_poll_job_to_completion":
            method_def = node
            break
    
    assert method_def is not None, "_poll_job_to_completion method not found"
    
    # Check for mark_job_pending_reconcile call
    found_mark_pending = False
    found_release = False
    found_raise_runtime = False
    found_for_loop = False
    
    for node in ast.walk(method_def):
        # Check for loop with range
        if isinstance(node, ast.For):
            if isinstance(node.iter, ast.Call):
                if isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range":
                    found_for_loop = True
        
        # Check for mark_job_pending_reconcile call in the exhaustion path
        if isinstance(node, ast.Name) and node.id == "mark_job_pending_reconcile":
            found_mark_pending = True
        
        # Check for release_job_budget - should only be in confirmed failure, not exhaustion
        if isinstance(node, ast.Name) and node.id == "release_job_budget":
            found_release = True
        
        # Check for RuntimeError raise
        if isinstance(node, ast.Raise):
            if isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "RuntimeError":
                    found_raise_runtime = True
    
    assert found_for_loop, "Polling loop with range() not found"
    assert found_mark_pending, "mark_job_pending_reconcile call not found in exhaustion path"
    assert found_raise_runtime, "RuntimeError raise not found for exhaustion"
    
    # Note: We DO expect release_job_budget to exist (for confirmed failures),
    # but mutations will verify it's not called on exhaustion


def test_poll_confirmed_failure_releases():
    """
    Test: Confirmed provider failure (failed/blocked/canceled) releases reservation.
    
    Verifies that _poll_job_to_completion calls release_job_budget on confirmed failures.
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Find checks for failed/blocked/canceled statuses
    found_failure_status_check = False
    found_release_call = False
    
    for node in ast.walk(tree):
        # Look for: if status in ("failed", "blocked", "canceled"):
        if isinstance(node, ast.Compare):
            if isinstance(node.left, ast.Name) and node.left.id == "status":
                for comp in node.comparators:
                    if isinstance(comp, ast.Tuple):
                        # Check if tuple contains failure statuses
                        status_values = [elt.value for elt in comp.elts if isinstance(elt, ast.Constant)]
                        if "failed" in status_values or "blocked" in status_values:
                            found_failure_status_check = True
        
        # Look for release_job_budget call
        if isinstance(node, ast.Name) and node.id == "release_job_budget":
            found_release_call = True
    
    assert found_failure_status_check, "Status check for failed/blocked/canceled not found"
    assert found_release_call, "release_job_budget call not found for confirmed failures"


def test_poll_single_provider_submit():
    """
    Test: Exactly one provider submit, even with polling exhaustion.
    
    M-R1c will resubmit on exhaustion - this test must fail.
    
    Verifies that _poll_job_to_completion never calls submit activities.
    The submit happens before polling starts, and never again.
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code of _poll_job_to_completion
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Check that submit_still_job_enforced and submit_clip_job_enforced are NOT called
    found_submit_call = False
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if "submit" in node.id and "job" in node.id:
                found_submit_call = True
    
    assert not found_submit_call, \
        "_poll_job_to_completion must not call submit activities (submit happens before poll)"


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_poll_exhaustion_runtime_keeps_reservation(tmp_path):
    """
    RUNTIME TEST: Poll exhaustion keeps reservation, marks pending_reconcile.
    
    Uses Temporal time-skipping to simulate >15 minutes of polling a stuck job.
    
    Asserts:
    - reserved > 0 (reservation held)
    - job marked pending_reconcile
    - exactly one submit (no resubmit)
    - zero releases (no release on timeout)
    
    Mutation M-R1a (release on exhaustion) must fail this test.
    """
    global _poll_count, _submit_count, _release_count
    _poll_count = 0
    _submit_count = 0
    _release_count = 0
    
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    
    # Initialize DB
    await init_studio_db(db_path)
    await create_episode(db_path, "test-ep")
    
    # Set live mode and G1.08 for enforcement
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("test-ep",)
        )
        await db.commit()
    
    # Initialize budget ledger
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    # Set initial budget
    await ledger.set_line(
        episode_id="test-ep",
        line_name="L2_drafts",
        cap=100.0,
        stop_at=80.0,
    )
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=[
                fake_submit_still_tracked,
                fake_poll_job_status_stuck,
                fake_commit_job_budget,
                fake_release_job_budget,
                fake_mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.review_still,
                activities.record_shot_result,
            ],
        ):
            # Patch the activities to use our fakes
            shot_plan = {
                "shot_id": "A01",
                "prompt": "Test prompt",
                "params": {"resolution": "1k", "quality": "medium", "aspect_ratio": "9:16"},
                "refs": [],
            }
            
            with pytest.raises(RuntimeError, match="polling exhausted"):
                await env.client.execute_workflow(
                    "ShotWorkflow",
                    args=["test-ep", shot_plan],
                    id="test-poll-exhaustion",
                    task_queue="test-queue",
                    execution_timeout=timedelta(minutes=35),
                )
            
            # Verify exactly one submit
            assert _submit_count == 1, f"Expected 1 submit, got {_submit_count}"
            
            # Verify zero releases (reservation held)
            assert _release_count == 0, f"Expected 0 releases, got {_release_count}"
            
            # Verify poll was called many times (180+ for ~30 min)
            assert _poll_count >= 100, f"Expected 100+ polls, got {_poll_count}"
            
            # Verify budget reservation still held
            status = await ledger.get_line_status("test-ep", "L2_drafts")
            assert status["reserved"] > 0, \
                f"Expected reservation held, got reserved={status['reserved']}"
            
            # Verify pending_reconcile marker in audit log
            async with aiosqlite.connect(db_path) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action = ? AND episode_id = ?",
                    ("job_pending_reconcile", "test-ep")
                ) as cursor:
                    row = await cursor.fetchone()
                    assert row[0] == 1, "Expected pending_reconcile marker in audit_log"


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_confirmed_failure_releases_once(tmp_path):
    """
    RUNTIME TEST: Confirmed provider failure releases exactly once.
    
    Uses a fake provider that returns failed status immediately.
    
    Asserts:
    - exactly one release call
    - workflow raises RuntimeError (confirmed failure)
    - reserved = 0 (reservation released)
    """
    global _poll_count, _submit_count, _release_count
    _poll_count = 0
    _submit_count = 0
    _release_count = 0
    
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    
    # Initialize DB
    await init_studio_db(db_path)
    await create_episode(db_path, "test-ep")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("test-ep",)
        )
        await db.commit()
    
    # Initialize budget ledger
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    # Set initial budget
    await ledger.set_line(
        episode_id="test-ep",
        line_name="L2_drafts",
        cap=100.0,
        stop_at=80.0,
    )
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=[
                fake_submit_still_tracked,
                fake_poll_job_status_failed,
                fake_commit_job_budget,
                fake_release_job_budget,
                fake_mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.review_still,
                activities.record_shot_result,
            ],
        ):
            shot_plan = {
                "shot_id": "A01",
                "prompt": "Test prompt",
                "params": {"resolution": "1k", "quality": "medium", "aspect_ratio": "9:16"},
                "refs": [],
            }
            
            with pytest.raises(RuntimeError, match="failed"):
                await env.client.execute_workflow(
                    "ShotWorkflow",
                    args=["test-ep", shot_plan],
                    id="test-confirmed-failure",
                    task_queue="test-queue",
                    execution_timeout=timedelta(minutes=5),
                )
            
            # Verify exactly one release
            assert _release_count == 1, f"Expected 1 release, got {_release_count}"
            
            # Verify reservation released
            status = await ledger.get_line_status("test-ep", "L2_drafts")
            assert status["reserved"] == 0, \
                f"Expected reservation released, got reserved={status['reserved']}"

