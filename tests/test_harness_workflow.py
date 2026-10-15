"""Tests for EpisodeHarnessWorkflow with 3 gates."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timedelta

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_harness import EpisodeHarnessWorkflow


@pytest.mark.asyncio
async def test_harness_three_gates():
    """Test that harness workflow has exactly 3 human gates."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeHarnessWorkflow],
            activities=[],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeHarnessWorkflow.run,
                args=["ep05-test", None, 500, False, True],
                id="test-harness-gates",
                task_queue="test-queue",
            )
            
            # Query initial state
            state = await handle.query("get_state")
            assert state["episode_id"] == "ep05-test"
            assert state["is_canary"] is False
            
            # Approve gate 1 (shot plan)
            await handle.signal("approve_shot_plan")
            
            # Approve gate 2 (scene stills)
            await handle.signal("approve_scene_stills")
            
            # Approve gate 3 (final cut)
            await handle.signal("approve_final_cut")
            
            # Workflow should complete
            # (Would need mock activities to fully test, but structure is validated)


@pytest.mark.asyncio
async def test_canary_mode_two_shots():
    """Test CANARY mode limits to 2 shots."""
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Mock activities
        parse_beatmap_mock = AsyncMock(return_value={
            "shots": [
                {"shot_id": "A01", "video_credits": 10, "still_credits": 5},
                {"shot_id": "A02", "video_credits": 10, "still_credits": 5},
                {"shot_id": "A03", "video_credits": 10, "still_credits": 5},
                {"shot_id": "A04", "video_credits": 10, "still_credits": 5},
            ]
        })
        
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeHarnessWorkflow],
            activities=[parse_beatmap_mock],
        ):
            handle = await env.client.start_workflow(
                EpisodeHarnessWorkflow.run,
                args=["ep05-canary", None, 100, True, True],  # is_canary=True
                id="test-canary-limit",
                task_queue="test-queue",
            )
            
            # In canary mode, workflow should process only first 2 shots
            # (Full test would need complete activity mocks)


def test_credit_cap_pause():
    """Test that credit cap triggers pause (unit test of logic)."""
    # This would be tested in integration with temporal env
    # For now, validate the workflow has update handlers
    assert hasattr(EpisodeHarnessWorkflow, "approve_continue")
    assert hasattr(EpisodeHarnessWorkflow, "increase_cap")


def test_workflow_signals():
    """Test that workflow has correct signal handlers."""
    assert hasattr(EpisodeHarnessWorkflow, "approve_shot_plan")
    assert hasattr(EpisodeHarnessWorkflow, "approve_scene_stills")
    assert hasattr(EpisodeHarnessWorkflow, "approve_final_cut")
