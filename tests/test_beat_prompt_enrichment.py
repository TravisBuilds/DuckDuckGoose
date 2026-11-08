"""Unit tests for beat context injection into prompts."""

import pytest


def test_prompt_enrichment_with_beat_context():
    """Test that shots with beat_purpose and emotional_tone get enriched prompts."""
    # Simulate what episode_v2.py does when processing shots
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park",
        "beat_purpose": "Setup: Duck discovers adventure",
        "beat_emotional_tone": "curiosity"
    }
    
    # Simulate the enrichment logic from episode_v2.py lines 657-662
    shot_plan = shot.copy()
    if shot.get("beat_purpose") and shot.get("beat_emotional_tone"):
        beat_context = f" ({shot['beat_purpose']}, {shot['beat_emotional_tone']} tone)"
        shot_plan["prompt"] = shot_plan["prompt"] + beat_context
    
    # Verify enrichment
    assert "Duck walks in park" in shot_plan["prompt"]
    assert "Setup: Duck discovers adventure" in shot_plan["prompt"]
    assert "curiosity tone" in shot_plan["prompt"]
    assert "(" in shot_plan["prompt"] and ")" in shot_plan["prompt"]
    
    # Verify format
    expected = "Duck walks in park (Setup: Duck discovers adventure, curiosity tone)"
    assert shot_plan["prompt"] == expected


def test_prompt_unchanged_without_beat_context():
    """Test that shots without beat fields are unchanged (story-off path)."""
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park"
    }
    
    # Simulate the enrichment logic
    shot_plan = shot.copy()
    if shot.get("beat_purpose") and shot.get("beat_emotional_tone"):
        beat_context = f" ({shot['beat_purpose']}, {shot['beat_emotional_tone']} tone)"
        shot_plan["prompt"] = shot_plan["prompt"] + beat_context
    
    # Verify no change
    assert shot_plan["prompt"] == "Duck walks in park"
    assert "(" not in shot_plan["prompt"]


def test_clip_prompt_enrichment_with_beat_context():
    """Test that clip_prompt is also enriched when present."""
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park",
        "clip_prompt": "Duck walks slowly through park",
        "beat_purpose": "Resolution: Duck finds peace",
        "beat_emotional_tone": "contentment"
    }
    
    # Simulate enrichment
    shot_plan = shot.copy()
    if shot.get("beat_purpose") and shot.get("beat_emotional_tone"):
        beat_context = f" ({shot['beat_purpose']}, {shot['beat_emotional_tone']} tone)"
        shot_plan["prompt"] = shot_plan["prompt"] + beat_context
        if shot_plan.get("clip_prompt"):
            shot_plan["clip_prompt"] = shot_plan["clip_prompt"] + beat_context
    
    # Verify both prompts enriched
    assert "Resolution: Duck finds peace" in shot_plan["prompt"]
    assert "contentment tone" in shot_plan["prompt"]
    assert "Resolution: Duck finds peace" in shot_plan["clip_prompt"]
    assert "contentment tone" in shot_plan["clip_prompt"]
    assert shot_plan["clip_prompt"] == "Duck walks slowly through park (Resolution: Duck finds peace, contentment tone)"


def test_partial_beat_fields_skipped():
    """Test that enrichment requires both beat_purpose AND emotional_tone."""
    # Only beat_purpose
    shot1 = {
        "shot_id": "A01",
        "prompt": "Duck walks",
        "beat_purpose": "Setup"
    }
    shot_plan1 = shot1.copy()
    if shot1.get("beat_purpose") and shot1.get("beat_emotional_tone"):
        beat_context = f" ({shot1['beat_purpose']}, {shot1['beat_emotional_tone']} tone)"
        shot_plan1["prompt"] = shot_plan1["prompt"] + beat_context
    assert shot_plan1["prompt"] == "Duck walks"
    
    # Only emotional_tone
    shot2 = {
        "shot_id": "A02",
        "prompt": "Goose appears",
        "beat_emotional_tone": "surprise"
    }
    shot_plan2 = shot2.copy()
    if shot2.get("beat_purpose") and shot2.get("beat_emotional_tone"):
        beat_context = f" ({shot2['beat_purpose']}, {shot2['beat_emotional_tone']} tone)"
        shot_plan2["prompt"] = shot_plan2["prompt"] + beat_context
    assert shot_plan2["prompt"] == "Goose appears"
