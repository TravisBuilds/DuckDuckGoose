"""
Live-mode ShotWorkflow tests with mocked providers.

Tests that human approval after QC escalation leads to clip submission.
"""

import pytest
import asyncio
from datetime import timedelta
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.shot import ShotWorkflow
from hfvg import activities


@pytest.mark.asyncio
async def test_qc_fail_closed_without_episode_id(tmp_path, monkeypatch):
    """
    Test: QC fails closed when episode_id is not provided.
    
    This verifies fix #1: QC must receive episode_id explicitly and fail
    closed if it's missing, escalating to human review.
    """
    from hfvg.activities.qc import review_still
    
    # Set up database
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_qc_fail_closed.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")  # Simulate live mode env
    
    # Call review_still without episode_id (simulates the old bug)
    result = await review_still(
        asset_url="https://example.com/some-asset.jpg",
        rubric={},
        episode_id=None,  # Missing episode_id
        shot_id="A01"
    )
    
    # Should fail closed with escalate=True
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "episode_id not provided" in result["issues"][0]


@pytest.mark.asyncio
async def test_qc_escalates_in_live_mode(tmp_path, monkeypatch):
    """
    Test: QC escalates to human review in live mode.
    
    This verifies fix #1: When DRY_RUN=false and live_mode=true,
    QC must escalate (needs_review) rather than auto-passing.
    """
    from hfvg.activities.qc import review_still
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_qc_escalate.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set episode to live mode + g108 approved
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")  # Live mode env
    
    # Call review_still with valid episode_id
    result = await review_still(
        asset_url="https://example.com/some-asset.jpg",
        rubric={},
        episode_id="ep99",
        shot_id="A01"
    )
    
    # Should escalate in live mode (real QC not implemented)
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "Real QC agent not implemented" in result["issues"][0]


@pytest.mark.asyncio
async def test_shot_workflow_human_approval_proceeds_to_clip(tmp_path, monkeypatch):
    """
    Test: After QC escalation and human approval, workflow proceeds to clip.
    
    This is the critical test for fix #1: verifying that the clip is actually
    generated after human approval of an escalated still (in dry mode, which
    simulates live behavior for workflow logic).
    """
    # Set up database for this test
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_live_shot.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set environment variables for activities - DRY MODE (simulates workflow logic)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")  # Dry mode for simplicity
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
            ],
        ):
            shot_plan = {
                "shot_id": "A01",
                "prompt": "Test shot requiring human approval",
                "refs": [],
                "params": {"duration": 5},
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", shot_plan],
                id="test-shot-human-approval",
                task_queue="test-task-queue",
            )
            
            # Wait for still to be generated and reviewed
            # In dry mode, QC review will escalate (review_still returns escalate=True in live)
            # The workflow should then wait for human approval
            # Wait (bounded) for the still to exist
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5.0
            state = await handle.query(ShotWorkflow.get_state)
            while not state["has_still"] and loop.time() < deadline:
                await asyncio.sleep(0.05)
                state = await handle.query(ShotWorkflow.get_state)
            assert state["has_still"], "Still should be generated by now"

            # Hold window: far longer than the whole dry clip pipeline (~0.3s).
            # Without the stills_approved wait, a clip would appear here.
            hold_until = loop.time() + 3.0
            while loop.time() < hold_until:
                state = await handle.query(ShotWorkflow.get_state)
                assert not state["has_clip"], \
                    "Clip should NOT be generated before approval signal (workflow must wait)"
                await asyncio.sleep(0.1)
            
            # Send human approval signal (this is what the API route does)
            await handle.signal("stills_approved")
            
            # Wait for workflow to process approval and continue to clip
            await asyncio.sleep(0.5)
            
            # Wait for completion
            result = await asyncio.wait_for(handle.result(), timeout=15)
            
            # Verify workflow completed successfully with BOTH still and clip
            assert result["status"] == "completed", f"Expected completed, got {result['status']}"
            assert result["still_url"] is not None, "Still URL should be present"
            assert result["clip_url"] is not None, "Clip URL should be present after approval"
            
            # This verifies fix #1: that human approval leads to clip generation
            # Before the fix, the workflow would fail at line 231 with "Max retries exceeded"


@pytest.mark.asyncio
async def test_shot_workflow_requires_approval_for_clip(tmp_path, monkeypatch):
    """
    Test: ShotWorkflow requires approval signal before generating clip.
    
    This verifies the critical protection at line 243 of shot.py:
        await workflow.wait_condition(lambda: self.stills_approved)
    
    The workflow MUST wait for approval after still generation before
    proceeding to expensive clip generation. This test confirms the
    workflow completes successfully when approval is sent.
    
    Note: Testing the negative case (blocking forever without approval) is
    difficult in time-skipping test environments due to workflow timeouts.
    The protection itself is enforced by the wait_condition in the workflow code,
    and all production paths that reach clip generation send the approval signal.
    """
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_approval_gate.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
            ],
        ):
            shot_plan = {
                "shot_id": "B01",
                "prompt": "Test approval gate",
                "refs": [],
                "params": {"duration": 5},
            }
            
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", shot_plan],
                id="test-shot-approval-gate",
                task_queue="test-task-queue",
            )
            
            # Wait for still generation to complete
            await asyncio.sleep(1.0)
            
            # Send required approval signal
            await handle.signal("stills_approved")
            
            # Wait for workflow to complete
            result = await asyncio.wait_for(handle.result(), timeout=15)
            
            # Verify both still and clip were generated
            assert result["status"] == "completed"
            assert result["still_url"] is not None, "Still should be generated"
            assert result["clip_url"] is not None, "Clip should be generated after approval"


@pytest.mark.asyncio
async def test_review_clip_fail_closed_without_episode_id(tmp_path, monkeypatch):
    """
    Test: review_clip fails closed when episode_id is not provided.
    
    This verifies R5: review_clip must have the same fail-closed behavior as review_still.
    """
    from hfvg.activities.qc import review_clip
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_clip_qc_fail_closed.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    
    # Call review_clip without episode_id
    result = await review_clip(
        asset_url="https://example.com/some-clip.mp4",
        rubric={},
        episode_id=None,  # Missing episode_id
        shot_id="A01"
    )
    
    # Should fail closed with escalate=True
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "episode_id not provided" in result["issues"][0]


@pytest.mark.asyncio
async def test_review_clip_escalates_in_live_mode(tmp_path, monkeypatch):
    """
    Test: review_clip escalates to human review in live mode.
    
    This verifies R5: review_clip must have the same escalation behavior as review_still.
    """
    from hfvg.activities.qc import review_clip
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_clip_qc_escalate.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set episode to live mode + g108 approved
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    
    # Call review_clip with valid episode_id
    result = await review_clip(
        asset_url="https://example.com/some-clip.mp4",
        rubric={},
        episode_id="ep99",
        shot_id="A01"
    )
    
    # Should escalate in live mode
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "Real QC agent not implemented" in result["issues"][0]


@pytest.mark.asyncio
async def test_review_clip_dry_run_false_live_off_escalates(tmp_path, monkeypatch):
    """
    Test: review_clip fails closed when DRY_RUN=false but DB live mode off.
    
    This verifies R5: DRY_RUN=false + DB live off must NOT return passed=True.
    """
    from hfvg.activities.qc import review_clip
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_clip_dry_false_live_off.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Episode exists but live_mode=false
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 0, g108_approved = 0 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")  # DRY_RUN=false but live requirements not met
    
    # Call review_clip
    result = await review_clip(
        asset_url="https://example.com/some-clip.mp4",
        rubric={},
        episode_id="ep99",
        shot_id="A01"
    )
    
    # Should fail closed and escalate, NOT return passed=True
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "DRY_RUN=false but episode not in live mode" in result["issues"][0]


@pytest.mark.asyncio
async def test_review_still_dry_run_false_live_off_escalates(tmp_path, monkeypatch):
    """
    Test: review_still fails closed when DRY_RUN=false but DB live mode off.
    
    This verifies R5: DRY_RUN=false + DB live off must NOT return passed=True.
    """
    from hfvg.activities.qc import review_still
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test_still_dry_false_live_off.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Episode exists but live_mode=false
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 0, g108_approved = 0 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")  # DRY_RUN=false but live requirements not met
    
    # Call review_still
    result = await review_still(
        asset_url="https://example.com/some-still.jpg",
        rubric={},
        episode_id="ep99",
        shot_id="A01"
    )
    
    # Should fail closed and escalate, NOT return passed=True
    assert result["passed"] is False
    assert result.get("escalate") is True
    assert "DRY_RUN=false but episode not in live mode" in result["issues"][0]



@pytest.mark.asyncio
async def test_canary_refuses_local_refs_in_live(tmp_path, monkeypatch):
    """
    Test: Canary route refuses local file paths for refs in live mode.
    
    M-R3c will allow local paths - this test must fail with AssertionError.
    """
    # This test verifies the logic exists in the code
    import inspect
    import sys
    sys.path.insert(0, '/workspace')
    from api.main import run_canary
    
    source = inspect.getsource(run_canary)
    
    # Must check for local refs in live mode and refuse them
    assert "not dry_run and refs" in source, \
        "Canary must check for refs in live mode"
    assert "501" in source or "not yet implemented" in source.lower(), \
        "Canary must refuse local refs in live mode with 501"
    
    # The check should explicitly refuse refs, not silently ignore them
    lines = source.split('\n')
    found_ref_check = False
    for line in lines:
        if 'not dry_run and refs' in line:
            found_ref_check = True
            break
    
    assert found_ref_check, "Must have explicit check for refs in live mode"
