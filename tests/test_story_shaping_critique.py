"""Unit tests for story shape critique logic."""

import pytest
from hfvg.models import StoryBeat, StoryShape, StoryShapeCritique
from hfvg.activities.story_shaping import _critique_story_shape


def test_critique_valid_story():
    """Test critique of a valid story gives high score."""
    story = StoryShape(
        logline="A duck discovers a mysterious pond and learns to be brave.",
        hook="Duck spots something glowing in the water",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Duck discovers glowing pond",
                emotional_tone="curiosity",
                target_duration_s=40.0,
                shot_ids=["A01", "A02"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Duck investigates despite fear",
                emotional_tone="tension",
                target_duration_s=40.0,
                shot_ids=["A03", "A04"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Duck finds courage and dives in",
                emotional_tone="triumph",
                target_duration_s=40.0,
                shot_ids=["A05"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="Duck surfaces with the glowing treasure",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert critique.score >= 70
    assert critique.passes
    assert critique.hook_present_early
    assert critique.payoff_references_hook
    assert critique.durations_fit
    assert critique.all_beats_have_shots
    assert critique.no_dead_beats


def test_critique_hook_too_late():
    """Test that hook past 5s is penalized."""
    story = StoryShape(
        logline="Test story",
        hook="Opening hook",
        hook_deadline_s=10.0,
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
        midpoint_beat_id="beat_2",
        payoff="Payoff",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert not critique.hook_present_early
    assert critique.score < 100
    assert any("Hook deadline" in issue for issue in critique.issues)


def test_critique_payoff_no_hook_reference():
    """Test that payoff without hook reference is penalized."""
    story = StoryShape(
        logline="Test story",
        hook="Duck finds a mysterious object",
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
        midpoint_beat_id="beat_2",
        payoff="Everything turns out fine",
        payoff_references_hook=False,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    # Should catch the missing reference
    assert critique.score < 100
    assert any("hook" in issue.lower() for issue in critique.issues)


def test_critique_duration_mismatch():
    """Test that duration mismatches are caught and penalized."""
    story = StoryShape(
        logline="Test story",
        hook="Opening",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Beat 1",
                emotional_tone="neutral",
                target_duration_s=20.0,
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
                target_duration_s=40.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert not critique.durations_fit
    assert critique.score < 100


def test_critique_dead_beats():
    """Test that overly long beats (>1/3 total) are penalized."""
    story = StoryShape(
        logline="Test story",
        hook="Opening",
        hook_deadline_s=5.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Very long beat (>1/3 of 120s = >40s)",
                emotional_tone="neutral",
                target_duration_s=50.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Short beat",
                emotional_tone="neutral",
                target_duration_s=35.0,
                shot_ids=["A02"],
            ),
            StoryBeat(
                beat_id="beat_3",
                description="Another beat",
                emotional_tone="neutral",
                target_duration_s=35.0,
                shot_ids=["A03"],
            ),
        ],
        midpoint_beat_id="beat_2",
        payoff="End",
        payoff_references_hook=True,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert not critique.no_dead_beats
    assert critique.score < 100
    assert any("too long" in issue for issue in critique.issues)


def test_critique_multiple_issues():
    """Test that multiple issues compound the score penalty."""
    story = StoryShape(
        logline="Bad story",
        hook="Hook",
        hook_deadline_s=15.0,
        beats=[
            StoryBeat(
                beat_id="beat_1",
                description="Long beat (>1/3 of 120s)",
                emotional_tone="neutral",
                target_duration_s=50.0,
                shot_ids=["A01"],
            ),
            StoryBeat(
                beat_id="beat_2",
                description="Short beat",
                emotional_tone="neutral",
                target_duration_s=40.0,
                shot_ids=[],
            ),
        ],
        midpoint_beat_id="beat_1",
        payoff="Unrelated ending",
        payoff_references_hook=False,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert critique.score < 70
    assert not critique.passes
    assert len(critique.issues) >= 3
    assert len(critique.suggestions) >= 2


def test_critique_suggestions():
    """Test that critique provides actionable suggestions."""
    story = StoryShape(
        logline="Test story",
        hook="Hook",
        hook_deadline_s=8.0,
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
                target_duration_s=45.0,
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
        midpoint_beat_id="beat_2",
        payoff="Something else happens",
        payoff_references_hook=False,
        total_duration_s=120.0,
    )
    
    critique = _critique_story_shape(story)
    
    assert len(critique.suggestions) > 0
    assert any("hook" in s.lower() for s in critique.suggestions)
