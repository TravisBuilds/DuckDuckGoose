"""Integration tests for story shaping in EpisodeWorkflowV2."""

import pytest
from datetime import timedelta
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from hfvg.models import StoryBeat, StoryShape
from hfvg.activities.story_shaping import (
    generate_story_shape_activity,
    critique_story_shape_activity,
    revise_story_shape_activity,
    assign_shots_to_beats_activity,
    set_test_stub_story_shape,
)


@pytest.fixture
def test_story_shape():
    """Create a valid test story shape."""
    return StoryShape(
        logline="A duck's adventure in a mysterious pond",
        hook="Duck discovers a glowing pond",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Setup: Duck finds the pond",
                emotional_tone="curiosity",
                target_duration_s=15.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Setup continued: Duck investigates",
                emotional_tone="curiosity",
                target_duration_s=15.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Conflict: Something lurks below",
                emotional_tone="tension",
                target_duration_s=20.0,
                shot_ids=["A03"],
            ),
            StoryBeat(
                beat_id="beat_4",
                description="Midpoint: Duck sees the creature",
                emotional_tone="fear",
                target_duration_s=15.0,
                shot_ids=["A04"],
            ),
            StoryBeat(
                beat_id="beat_5",
                description="Resolution: Creature is friendly",
                emotional_tone="relief",
                target_duration_s=20.0,
                shot_ids=["A05"],
            ),
            StoryBeat(
                beat_id="beat_6",
                description="Payoff: Duck befriends creature",
                emotional_tone="joy",
                target_duration_s=15.0,
                shot_ids=["A06"],
            ),
            StoryBeat(
                beat_id="beat_7",
                description="Closing: Duck returns home happy",
                emotional_tone="contentment",
                target_duration_s=20.0,
                shot_ids=["A07"],
            ),
        ],
        midpoint_beat_id="beat_4",
        payoff="The glowing pond becomes Duck's new home",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )


@pytest.mark.asyncio
async def test_story_shape_generation_dry_run(test_story_shape):
    """Test story shape generation in dry run mode."""
    set_test_stub_story_shape(test_story_shape)
    
    try:
        result = await generate_story_shape_activity(
            episode_id="test-ep",
            idea="A duck discovers a mysterious pond",
            genre_preset="adventure",
            target_duration=120.0,
            dry_run=True,
        )
        
        assert result["dry_run"] is True
        assert "story_shape" in result
        assert result["story_shape"]["logline"]
        assert len(result["story_shape"]["beats"]) >= 3
    finally:
        set_test_stub_story_shape(None)


@pytest.mark.asyncio
async def test_story_shape_critique(test_story_shape):
    """Test story shape critique."""
    result = await critique_story_shape_activity(test_story_shape.model_dump())
    
    assert "critique" in result
    assert result["critique"]["score"] >= 0
    assert result["critique"]["score"] <= 100
    assert "needs_revision" in result
    
    # Valid story should pass
    assert result["critique"]["passes"]
    assert not result["needs_revision"]


@pytest.mark.asyncio
async def test_story_shape_revision(test_story_shape):
    """Test story shape automatic revision."""
    # Create a story with issues
    bad_story = StoryShape(
        logline="Bad story",
        hook="Short",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Too long",
                emotional_tone="neutral",
                target_duration_s=70.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Normal",
                emotional_tone="neutral",
                target_duration_s=30.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Short",
                emotional_tone="neutral",
                target_duration_s=20.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="Unrelated ending",
        payoff_references_hook=False,
        total_duration_s=120.0,
    )
    
    critique_result = await critique_story_shape_activity(bad_story.model_dump())
    
    revision_result = await revise_story_shape_activity(
        bad_story.model_dump(),
        critique_result["critique"],
        dry_run=True,
    )
    
    assert "story_shape" in revision_result
    assert "changes_made" in revision_result
    assert len(revision_result["changes_made"]) > 0


@pytest.mark.asyncio
async def test_assign_shots_to_beats():
    """Test shot assignment to beats."""
    story = StoryShape(
        logline="Test story",
        hook="Opening",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Act 1",
                emotional_tone="setup",
                target_duration_s=30.0,
                shot_ids=[],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Act 2",
                emotional_tone="conflict",
                target_duration_s=60.0,
                shot_ids=[],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Act 3",
                emotional_tone="resolution",
                target_duration_s=30.0,
                shot_ids=[],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    shots = [
        {"shot_id": "A01", "screen_time": 15.0},
        {"shot_id": "A02", "screen_time": 15.0},
        {"shot_id": "A03", "screen_time": 30.0},
        {"shot_id": "A04", "screen_time": 30.0},
        {"shot_id": "A05", "screen_time": 15.0},
        {"shot_id": "A06", "screen_time": 15.0},
    ]
    
    result = await assign_shots_to_beats_activity(
        story.model_dump(),
        shots,
    )
    
    assert "story_shape" in result
    assert "assignments" in result
    
    updated_story = StoryShape(**result["story_shape"])
    
    # Check that all beats have shots assigned
    for beat in updated_story.beats:
        assert len(beat.shot_ids) > 0
    
    # Check that all shots are assigned
    all_assigned_shots = []
    for beat in updated_story.beats:
        all_assigned_shots.extend(beat.shot_ids)
    
    assert len(all_assigned_shots) == len(shots)
    assert set(all_assigned_shots) == {s["shot_id"] for s in shots}


@pytest.mark.asyncio
async def test_workflow_story_shape_approval(test_story_shape):
    """Test story shape approval gate in workflow."""
    from temporalio import activity
    
    # Stub activities for workflow infrastructure
    @activity.defn(name="acquire_run_lock")
    async def stub_acquire_run_lock(*args):
        return {"acquired": True}
    
    @activity.defn(name="release_run_lock")
    async def stub_release_run_lock(*args):
        return {"released": True}
    
    @activity.defn(name="upsert_episode_activity")
    async def stub_upsert_episode(*args):
        return {}
    
    @activity.defn(name="init_episode_credit_line_activity")
    async def stub_init_credit_line(*args):
        return {}
    
    @activity.defn(name="init_episode_budget_lines_activity")
    async def stub_init_budget_lines(*args):
        return {"lines": ["L1_refs", "L2_drafts", "L3_final_stills", "L4_video", "L7_revision"]}
    
    @activity.defn(name="load_gate_policy_activity")
    async def stub_load_gate_policy(*args):
        return {
            "gates": {},
            "duck_casting": {},
        }
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        set_test_stub_story_shape(test_story_shape)
        
        try:
            async with Worker(
                env.client,
                task_queue="test-story-shaping",
                workflows=[EpisodeWorkflowV2],
                activities=[
                    generate_story_shape_activity,
                    critique_story_shape_activity,
                    revise_story_shape_activity,
                    assign_shots_to_beats_activity,
                    stub_acquire_run_lock,
                    stub_release_run_lock,
                    stub_upsert_episode,
                    stub_init_credit_line,
                    stub_init_budget_lines,
                    stub_load_gate_policy,
                ],
            ):
                handle = await env.client.start_workflow(
                    EpisodeWorkflowV2.run,
                    args=["ep-test-story", None, True, None, None, None, None, True],  # enable_story_shaping=True
                    id="ep-test-story",
                    task_queue="test-story-shaping",
                )
                
                # Wait a bit for story shape generation
                await env.sleep(5)
                
                # Query state to check story shape
                state = await handle.query("get_state")
                assert state["story_shape"] is not None
                assert state["story_shape_critique"] is not None
                
                # Approve story shape
                await handle.execute_update("approve_story_shape")
                
                # Verify approval
                state = await handle.query("get_state")
                assert state["approvals"]["story_shape"] is True
        
        finally:
            set_test_stub_story_shape(None)


@pytest.mark.asyncio
async def test_workflow_story_shape_validator():
    """Test that story shape approval validator works."""
    from temporalio import activity
    
    # Stub activities for workflow infrastructure
    @activity.defn(name="acquire_run_lock")
    async def stub_acquire_run_lock(*args):
        return {"acquired": True}
    
    @activity.defn(name="release_run_lock")
    async def stub_release_run_lock(*args):
        return {"released": True}
    
    @activity.defn(name="upsert_episode_activity")
    async def stub_upsert_episode(*args):
        return {}
    
    @activity.defn(name="init_episode_credit_line_activity")
    async def stub_init_credit_line(*args):
        return {}
    
    @activity.defn(name="init_episode_budget_lines_activity")
    async def stub_init_budget_lines(*args):
        return {"lines": ["L1_refs", "L2_drafts", "L3_final_stills", "L4_video", "L7_revision"]}
    
    @activity.defn(name="load_gate_policy_activity")
    async def stub_load_gate_policy(*args):
        return {
            "gates": {},
            "duck_casting": {},
        }
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-story-validator",
            workflows=[EpisodeWorkflowV2],
            activities=[
                generate_story_shape_activity,
                critique_story_shape_activity,
                revise_story_shape_activity,
                assign_shots_to_beats_activity,
                stub_acquire_run_lock,
                stub_release_run_lock,
                stub_upsert_episode,
                stub_init_credit_line,
                stub_init_budget_lines,
                stub_load_gate_policy,
            ],
        ):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep-test-val", None, True, None, None, None, None, True],  # enable_story_shaping=True
                id="ep-test-val",
                task_queue="test-story-validator",
            )
            
            # Try to approve before story shape exists - should fail
            # Note: The validator raises ApplicationError, but Temporal wraps it in WorkflowUpdateFailedError
            with pytest.raises(Exception):
                await handle.execute_update("approve_story_shape")
