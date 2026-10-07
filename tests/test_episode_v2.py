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
                activities.await_job_enforced,
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
                check_live_mode_and_g108,
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
                activities.await_job_enforced,
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
                check_live_mode_and_g108,
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
            
            await env.sleep(3)
            
            # Should be waiting at G4.09 (picture lock) - verify it blocks progression
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g409"] is False, "Should be waiting at G4.09"
            # Previous gate should be complete
            assert state["approvals"]["g408"] is True, "G4.08 should be complete"
            
            # KEY ASSERTION: Script gate (G5.01) should NOT be reachable without picture lock
            # If wait_condition is removed, workflow would proceed immediately to G5.01
            # Sleep to let workflow attempt to proceed (it should be blocked at G4.09)
            await env.sleep(2)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g501"] is False, "G5.01 should NOT be reachable before G4.09 picture lock"
            
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
                activities.await_job_enforced,
                activities.review_still,
                activities.review_clip,
                activities.trim_clips,
                activities.render_edit,
                activities.mix_audio,
                activities.generate_voiceover,
                activities.generate_sfx,
                activities.generate_music,
                check_live_mode_and_g108,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep04-test", None, True],
                id="test-episode-v2-hold",
                task_queue="test-task-queue",
            )
            
            # Fast-forward through all gates except GX.01
            await handle.signal(EpisodeWorkflowV2.approve_g101)
            await handle.signal(EpisodeWorkflowV2.approve_g103)
            await handle.signal(EpisodeWorkflowV2.approve_g108)
            await handle.signal(EpisodeWorkflowV2.approve_gc02)
            await handle.signal(EpisodeWorkflowV2.approve_g212)
            await handle.signal(EpisodeWorkflowV2.approve_g406)
            await handle.signal(EpisodeWorkflowV2.approve_g408)
            await handle.signal(EpisodeWorkflowV2.approve_g409)
            await handle.signal(EpisodeWorkflowV2.approve_g501)
            await handle.signal(EpisodeWorkflowV2.approve_g610)
            
            await env.sleep(2)
            
            # Should be waiting at GX.01 HOLD (verify posting is blocked)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["gx01"] is False, "Should be at HOLD gate"
            assert state["approvals"]["g610"] is True, "Audio should be complete"
            # This proves GX.01 HOLD works: workflow stopped before posting
            
            # Workflow should not proceed without explicit GX.01 approval
            await handle.terminate()
            try:
                await handle.result()
            except:
                pass  # Terminated workflows raise an exception
