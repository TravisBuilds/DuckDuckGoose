"""Unit tests for StoryShape models and validation."""

import pytest
from hfvg.models import StoryBeat, StoryShape


def test_story_beat_basic():
    """Test basic StoryBeat creation."""
    beat = StoryBeat(
        beat_id="beat_1",
        description="Opening scene",
        emotional_tone="excitement",
        target_duration_s=15.0,
        shot_ids=["A01", "A02"],
    )
    assert beat.beat_id == "beat_1"
    assert beat.target_duration_s == 15.0
    assert len(beat.shot_ids) == 2


def test_story_shape_valid():
    """Test valid StoryShape passes validation."""
    story = StoryShape(
        logline="A duck discovers a mysterious pond.",
        hook="Duck spots something glowing in the water",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Discovery",
                emotional_tone="curiosity",
                target_duration_s=40.0,
                shot_ids=["A01", "A02"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Investigation",
                emotional_tone="tension",
                target_duration_s=40.0,
                shot_ids=["A03", "A04"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Resolution",
                emotional_tone="relief",
                target_duration_s=40.0,
                shot_ids=["A05"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="The glowing object is revealed",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert len(issues) == 0


def test_story_shape_hook_too_short():
    """Test that short hooks are caught."""
    story = StoryShape(
        logline="A story",
        hook="Short",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Something",
                emotional_tone="neutral",
                target_duration_s=120.0,
                shot_ids=["A01"],
            ),
        ],
        midpoint_beat_id="beat_1",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("Hook is too short" in issue for issue in issues)


def test_story_shape_too_few_beats():
    """Test that stories with <3 beats fail validation."""
    story = StoryShape(
        logline="A story with too few beats",
        hook="Opening moment",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Only beat",
                emotional_tone="neutral",
                target_duration_s=60.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Second beat",
                emotional_tone="neutral",
                target_duration_s=60.0,
                shot_ids=["A02"],
            ),
        ],
        midpoint_beat_id="beat_1",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("at least 3 beats" in issue for issue in issues)


def test_story_shape_duration_mismatch():
    """Test that mismatched durations are caught."""
    story = StoryShape(
        logline="Duration mismatch test",
        hook="Opening moment",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Beat 1",
                emotional_tone="neutral",
                target_duration_s=30.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Beat 2",
                emotional_tone="neutral",
                target_duration_s=30.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Beat 3",
                emotional_tone="neutral",
                target_duration_s=30.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("Beat durations" in issue and "don't match" in issue for issue in issues)


def test_story_shape_invalid_midpoint():
    """Test that invalid midpoint beat_id is caught."""
    story = StoryShape(
        logline="Invalid midpoint test",
        hook="Opening moment",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Beat 1",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Beat 2",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Beat 3",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_99",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("Midpoint beat" in issue and "not found" in issue for issue in issues)


def test_story_shape_dead_beat():
    """Test that beats over 1/3 of total duration are caught."""
    story = StoryShape(
        logline="Dead beat test",
        hook="Opening moment",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Long beat (over 1/3 of total)",
                emotional_tone="neutral",
                target_duration_s=50.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Normal beat",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Short beat",
                emotional_tone="neutral",
                target_duration_s=30.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("too long" in issue for issue in issues)


def test_story_shape_unassigned_beats():
    """Test that beats without shots are caught."""
    story = StoryShape(
        logline="Unassigned beats test",
        hook="Opening moment",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Beat with shots",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Beat without shots",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=[],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Another beat with shots",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=["A02"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    issues = story.validate_structure()
    assert any("without shots" in issue for issue in issues)
