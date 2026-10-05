"""Test episode workflow with time-skipping."""

import asyncio

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.ledger import Ledger
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_episode_state_query(tmp_path):
    """Test querying episode state during execution."""
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
                args=["test-ep-002", "Test idea", []],
                id="test-ep-002",
                task_queue="test-queue",
            )

            await asyncio.sleep(0.1)
            state = await handle.query(EpisodeWorkflow.get_state)
            assert state["stage"] == "readback"
            assert not state["readback_approved"]

            await handle.execute_update(EpisodeWorkflow.approve_readback)

            await asyncio.sleep(0.1)
            state = await handle.query(EpisodeWorkflow.get_state)
            assert state["readback_approved"]

            await handle.cancel()
            try:
                await handle.result()
            except:
                pass
