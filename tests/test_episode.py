"""Test episode workflow with time-skipping."""

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.ledger import Ledger
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow


@pytest.mark.asyncio
async def test_episode_completes_with_all_approvals():
    """Test that an episode completes when all gates are approved."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        ledger = Ledger(":memory:")
        await ledger.init_db()

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
                args=["test-ep-001", "A duck teaches penguins", ["instagram"]],
                id="test-ep-001",
                task_queue="test-queue",
            )

            state = await handle.query(EpisodeWorkflow.get_state)
            assert state["episode_id"] == "test-ep-001"

            await handle.execute_update(EpisodeWorkflow.approve_readback)

            await handle.execute_update(EpisodeWorkflow.approve_storyboard)

            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[1])
            await handle.execute_update(EpisodeWorkflow.approve_scene_stills, args=[2])

            await handle.execute_update(EpisodeWorkflow.approve_final)

            posting_handle = env.client.get_workflow_handle("test-ep-001-posting")
            await posting_handle.execute_update(
                PostingWorkflow.approve_posts, args=[["instagram"]]
            )

            result = await handle.result()

            assert result["status"] == "completed"
            assert result["episode_id"] == "test-ep-001"
            assert "final_url" in result
            assert len(result["posts"]) > 0


@pytest.mark.asyncio
async def test_episode_state_query():
    """Test querying episode state during execution."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        ledger = Ledger(":memory:")
        await ledger.init_db()

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
                args=["test-ep-002", "Test idea", []],
                id="test-ep-002",
                task_queue="test-queue",
            )

            state = await handle.query(EpisodeWorkflow.get_state)
            assert state["stage"] == "readback"
            assert not state["readback_approved"]

            await handle.execute_update(EpisodeWorkflow.approve_readback)

            state = await handle.query(EpisodeWorkflow.get_state)
            assert state["readback_approved"]
