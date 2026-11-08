"""Unit tests for beat context injection into prompts."""

import pytest
from hfvg.workflows.episode_v2 import enrich_shot_prompts_with_beat_context


def test_prompt_enrichment_with_beat_context():
    """Test that shots with beat_purpose and emotional_tone get enriched prompts."""
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park",
        "beat_purpose": "Setup: Duck discovers adventure",
        "beat_emotional_tone": "curiosity"
    }
    
    # Use the actual helper function
    enriched = enrich_shot_prompts_with_beat_context(shot.copy())
    
    # Verify enrichment
    assert "Duck walks in park" in enriched["prompt"]
    assert "Setup: Duck discovers adventure" in enriched["prompt"]
    assert "curiosity tone" in enriched["prompt"]
    assert "(" in enriched["prompt"] and ")" in enriched["prompt"]
    
    # Verify format
    expected = "Duck walks in park (Setup: Duck discovers adventure, curiosity tone)"
    assert enriched["prompt"] == expected


def test_prompt_unchanged_without_beat_context():
    """Test that shots without beat fields are unchanged (story-off path)."""
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park"
    }
    
    # Use the actual helper function
    enriched = enrich_shot_prompts_with_beat_context(shot.copy())
    
    # Verify no change
    assert enriched["prompt"] == "Duck walks in park"
    assert "(" not in enriched["prompt"]


def test_clip_prompt_enrichment_with_beat_context():
    """Test that clip_prompt is also enriched when present."""
    shot = {
        "shot_id": "A01",
        "prompt": "Duck walks in park",
        "clip_prompt": "Duck walks slowly through park",
        "beat_purpose": "Resolution: Duck finds peace",
        "beat_emotional_tone": "contentment"
    }
    
    # Use the actual helper function
    enriched = enrich_shot_prompts_with_beat_context(shot.copy())
    
    # Verify both prompts enriched
    assert "Resolution: Duck finds peace" in enriched["prompt"]
    assert "contentment tone" in enriched["prompt"]
    assert "Resolution: Duck finds peace" in enriched["clip_prompt"]
    assert "contentment tone" in enriched["clip_prompt"]
    assert enriched["clip_prompt"] == "Duck walks slowly through park (Resolution: Duck finds peace, contentment tone)"


def test_partial_beat_fields_skipped():
    """Test that enrichment requires both beat_purpose AND emotional_tone."""
    # Only beat_purpose
    shot1 = {
        "shot_id": "A01",
        "prompt": "Duck walks",
        "beat_purpose": "Setup"
    }
    enriched1 = enrich_shot_prompts_with_beat_context(shot1.copy())
    assert enriched1["prompt"] == "Duck walks"
    
    # Only emotional_tone
    shot2 = {
        "shot_id": "A02",
        "prompt": "Goose appears",
        "beat_emotional_tone": "surprise"
    }
    enriched2 = enrich_shot_prompts_with_beat_context(shot2.copy())
    assert enriched2["prompt"] == "Goose appears"
