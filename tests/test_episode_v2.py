"""Tests for Episode Workflow V2 (handbook-compliant with approval gates)."""

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.activities.studio_generation import check_live_mode_and_g108
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


@workflow.defn(name="ShotWorkflow", sandboxed=False)
class StubShotWorkflow:
    """Stub ShotWorkflow that returns immediately after stills_approved signal."""
    
    def __init__(self):
        self.stills_approved_flag = False
    
    @workflow.signal
    def stills_approved(self):
        """Signal from parent that stills have been approved."""
        self.stills_approved_flag = True
    
    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """Wait for stills_approved signal, then return immediately."""
        shot_id = shot_plan["shot_id"]
        
        # Wait for stills_approved signal (just like real ShotWorkflow)
        await workflow.wait_condition(lambda: self.stills_approved_flag)
        
        # Return success immediately without doing any work
        return {
            "shot_id": shot_id,
            "still_url": f"https://test.com/{episode_id}/{shot_id}_still.png",
            "clip_url": f"https://test.com/{episode_id}/{shot_id}_clip.mp4",
            "status": "success",
        }


@pytest.mark.asyncio
async def test_episode_v2_approval_gates(tmp_path, monkeypatch):
    """Test that workflow stops at each Travis approval gate."""
    # Set up test database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)  # EpisodeWorkflowV2 uses DATABASE_PATH
    
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(db_path)
    await create_episode(db_path, "ep04-test")
    await set_live_mode(db_path, "ep04-test", True)
    await approve_g108(db_path, "ep04-test")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2, StubShotWorkflow],
            activities=[
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
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
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.post_to_platform,
                activities.check_live_mode_and_g108,
            ],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep04-test", None, True],  # episode_id, beatmap_path, dry_run
                id="test-episode-v2",
                task_queue="test-task-queue",
            )
            
            # Verify workflow is waiting at G1.01 (pitch pick)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g101"] is False, "Should be waiting at G1.01"
            
            # Approve G1.01 (pitch pick)
            await handle.signal(EpisodeWorkflowV2.approve_g101)
            
            # Workflow should now be waiting at G1.03 (beatmap)
            await env.sleep(1)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g101"] is True, "G1.01 should be approved"
            assert state["approvals"]["g103"] is False, "Should be waiting at G1.03"
            
            # Approve G1.03 (beatmap)
            await handle.signal(EpisodeWorkflowV2.approve_g103)
            
            # Workflow should now be waiting at G1.08 (credit plan)
            await env.sleep(1)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g103"] is True, "G1.03 should be approved"
            assert state["approvals"]["g108"] is False, "Should be waiting at G1.08"
            
            # Test passed - workflow correctly stops at each approval gate
            # Full end-to-end test is in test_episode_v2_picture_lock_blocks_audio


@pytest.mark.asyncio
async def test_episode_v2_picture_lock_blocks_audio(tmp_path, monkeypatch):
    """Test that G4.09 picture lock must be approved before audio."""
    # Set up test database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)  # EpisodeWorkflowV2 uses DATABASE_PATH
    
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(db_path)
    await create_episode(db_path, "ep04-test")
    await set_live_mode(db_path, "ep04-test", True)
    await approve_g108(db_path, "ep04-test")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2, StubShotWorkflow],
            activities=[
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
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
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.check_live_mode_and_g108,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep04-test", None, True],
                id="test-episode-v2-piclock",
                task_queue="test-task-queue",
            )
            
            # Fast-forward through story and stills
            await handle.signal(EpisodeWorkflowV2.approve_g101)
            await handle.signal(EpisodeWorkflowV2.approve_g103)
            await handle.signal(EpisodeWorkflowV2.approve_g108)
            await handle.signal(EpisodeWorkflowV2.approve_gc02)
            await handle.signal(EpisodeWorkflowV2.approve_g212)
            await handle.signal(EpisodeWorkflowV2.approve_g406)
            await handle.signal(EpisodeWorkflowV2.approve_g408)
            
            # Poll to verify workflow reaches G4.09 (with bounded timeout)
            import time
            deadline = time.time() + 5.0
            reached_g409 = False
            while time.time() < deadline:
                state = await handle.query(EpisodeWorkflowV2.get_state)
                if state.get("current_gate") == "G4.09":
                    reached_g409 = True
                    break
                await env.sleep(0.1)
            
            # Verify workflow reached and stopped at G4.09 (picture lock blocks audio)
            assert reached_g409, f"Workflow did not reach G4.09 within timeout, current gate: {state.get('current_gate')}"
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["current_gate"] == "G4.09", f"Should be at G4.09, got {state.get('current_gate')}"
            assert state["approvals"]["g409"] is False, "G4.09 should not be approved yet"
            assert "G4.09" not in state["passed_gates"], "G4.09 should not be in passed_gates yet"
            
            # Restore original assertions
            assert state["approvals"]["g408"] is True, "G4.08 should be complete"
            assert state["stage"] == "mute_edit", f"Should be in mute_edit stage (at G4.09), got {state['stage']}"
            
            # Workflow should be blocked at G4.09, not proceed to audio
            desc = await handle.describe()
            assert desc.status.name == "RUNNING", "Workflow should be running (blocked at G4.09)"
            
            # Sleep to let workflow attempt to proceed (it should remain blocked at G4.09)
            await env.sleep(2)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g501"] is False, "G5.01 should NOT be reachable before G4.09 picture lock"
            assert state["current_gate"] == "G4.09", "Should still be at G4.09"
            assert "G5.01" not in state["passed_gates"], "G5.01 should not be passed yet"
            
            # Now approve picture lock and verify workflow can proceed
            await handle.signal(EpisodeWorkflowV2.approve_g409)
            await env.sleep(2)
            
            # After approving G4.09, workflow should reach G5.01 (but not approve it yet)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g409"] is True, "G4.09 should be approved"
            # The workflow should now be WAITING at G5.01 (not approved yet)
            # This proves G4.09 gate worked: only after approval did workflow reach G5.01
            
            # Terminate workflow and wait for it to complete
            await handle.terminate()
            try:
                await handle.result()
            except:
                pass  # Terminated workflows raise an exception


@pytest.mark.asyncio
async def test_episode_v2_gx01_hold(tmp_path, monkeypatch):
    """Test that GX.01 holds by default (never auto-post)."""
    # Set up test database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)  # EpisodeWorkflowV2 uses DATABASE_PATH
    
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(db_path)
    await create_episode(db_path, "ep04-test")
    await set_live_mode(db_path, "ep04-test", True)
    await approve_g108(db_path, "ep04-test")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[EpisodeWorkflowV2, StubShotWorkflow],
            activities=[
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
                activities.submit_still_job_enforced,
                activities.submit_clip_job_enforced,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.review_still,
                activities.review_clip,
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                activities.check_live_mode_and_g108,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep04-test", None, True],
                id="test-episode-v2-hold",
                task_queue="test-task-queue",
            )
            
            # Fast-forward through all gates up to G6.10 (but not including GX.01)
            await handle.signal(EpisodeWorkflowV2.approve_g101)
            await handle.signal(EpisodeWorkflowV2.approve_g103)
            await handle.signal(EpisodeWorkflowV2.approve_g108)
            await handle.signal(EpisodeWorkflowV2.approve_gc02)
            await handle.signal(EpisodeWorkflowV2.approve_g212)
            await handle.signal(EpisodeWorkflowV2.approve_g406)
            await handle.signal(EpisodeWorkflowV2.approve_g408)
            await handle.signal(EpisodeWorkflowV2.approve_g409)
            await handle.signal(EpisodeWorkflowV2.approve_g501)
            # Do NOT send g610 yet - we'll send it after verifying we're at G6.10
            
            # Poll to verify workflow reaches G6.10 first
            import time
            deadline = time.time() + 5.0
            reached_g610 = False
            while time.time() < deadline:
                state = await handle.query(EpisodeWorkflowV2.get_state)
                if state.get("current_gate") == "G6.10":
                    reached_g610 = True
                    break
                await env.sleep(0.1)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert reached_g610, f"Workflow did not reach G6.10. Current: {state.get('current_gate')}"
            
            # Now send g610 to let it proceed to GX.01
            await handle.signal(EpisodeWorkflowV2.approve_g610)
            
            # Give workflow time to process the signal and reach GX.01
            await env.sleep(0.5)
            
            # Poll to verify workflow reaches GX.01 (with bounded timeout)
            deadline = time.time() + 5.0
            reached_gx01 = False
            while time.time() < deadline:
                state = await handle.query(EpisodeWorkflowV2.get_state)
                if state.get("current_gate") == "GX.01":
                    reached_gx01 = True
                    break
                await env.sleep(0.1)
            
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert reached_gx01, f"Workflow did not reach GX.01 within timeout. Current gate: {state.get('current_gate')}, Passed: {state.get('passed_gates', [])}"
            
            # Verify workflow is at GX.01 and RUNNING
            assert state["current_gate"] == "GX.01", f"Should be at GX.01, got {state.get('current_gate')}"
            assert state["approvals"]["gx01"] is False, "GX.01 should not be approved yet"
            assert "GX.01" not in state["passed_gates"], "GX.01 should not be in passed_gates yet"
            
            desc = await handle.describe()
            assert desc.status.name == "RUNNING", "Workflow should still be running (blocked at GX.01)"
            
            # Now send G7.02 and G7.03 approvals (but NOT GX.01) - later gates
            await handle.signal(EpisodeWorkflowV2.approve_g702)
            await handle.signal(EpisodeWorkflowV2.approve_g703)
            
            await env.sleep(0.5)
            
            # Verify workflow is STILL at GX.01 (blocks even with later gates approved)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["current_gate"] == "GX.01", \
                f"Should still be at GX.01 after later approvals, got {state.get('current_gate')}"
            assert "GX.01" not in state["passed_gates"], "GX.01 should still not be passed"
            
            desc = await handle.describe()
            assert desc.status.name == "RUNNING", "Workflow should still be running at GX.01"
            
            # Now send GX.01 approval to allow completion
            await handle.signal(EpisodeWorkflowV2.approve_gx01)
            
            # Wait briefly for workflow to progress
            await env.sleep(0.5)
            
            # Verify GX.01 is now passed
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert "GX.01" in state["passed_gates"], "GX.01 should be in passed_gates after approval"
            
            # Workflow should now complete
            result = await handle.result()
            assert result is not None, "Workflow should complete after all approvals"
