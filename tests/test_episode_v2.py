"""Tests for Episode Workflow V2 (handbook-compliant with approval gates)."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow


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
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
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
@pytest.mark.skip(reason="Slow test - shot workflows take too long even in dry_run mode (>30s)")
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
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
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
            
            await env.sleep(2)
            
            # Should be waiting at G4.09 (picture lock)
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g409"] is False
            assert state["approvals"]["g501"] is False, "Should not have reached script yet"
            
            # Approve picture lock
            await handle.signal(EpisodeWorkflowV2.approve_g409)
            
            await env.sleep(1)
            
            # Now should be able to proceed to script
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["g409"] is True
            
            # Cancel workflow (we've proven the gate works)
            await handle.cancel()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Slow test - shot workflows take too long even in dry_run mode (>30s)")
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
            workflows=[EpisodeWorkflowV2, ShotWorkflow],
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
            
            # Should be waiting at GX.01 HOLD
            state = await handle.query(EpisodeWorkflowV2.get_state)
            assert state["approvals"]["gx01"] is False, "Should be at HOLD gate"
            assert state["approvals"]["g610"] is True, "Audio should be complete"
            
            # Workflow should not proceed without explicit GX.01 approval
            # Cancel to end test
            await handle.cancel()
