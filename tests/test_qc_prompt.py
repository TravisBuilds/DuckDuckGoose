"""Tests for prompt validation."""

import pytest
from hfvg.qc.prompt_validation import (
    validate_prompt,
    suggest_ambient_motion,
    check_ambient_motion_named,
    PromptValidationError,
)


def test_reject_nothing_else_moves():
    """Test rejection of 'nothing else moves' endings."""
    bad_prompts = [
        "A duck stands at the desk, nothing else moves.",
        "The moose walks through, everything else is still.",
        "Camera locked on the door, all else static.",
        "Only the duck moves.",
        "The scene is frozen, rest of the scene is still",
    ]
    
    for prompt in bad_prompts:
        with pytest.raises(PromptValidationError, match="forbidden static clause"):
            validate_prompt(prompt)


def test_reject_nothing_moves_in_middle():
    """Test rejection of 'nothing moves' in middle of prompt."""
    prompt = "The duck stands still, nothing moves in the background."
    
    with pytest.raises(PromptValidationError, match="nothing moves"):
        validate_prompt(prompt)


def test_accept_valid_prompts():
    """Test acceptance of valid prompts with named ambient motion."""
    valid_prompts = [
        "A duck stands at the desk as snowfall drifts through the doorway.",
        "The moose walks through the door, breath steam visible, ears flicking.",
        "Locked camera on the pool, steam rising, ripples spreading.",
        "The duck watches calmly, tuque pom bobbing, aurora glowing softly behind.",
    ]
    
    for prompt in valid_prompts:
        result = validate_prompt(prompt)
        assert result == prompt  # Should return unchanged


def test_check_ambient_motion_named():
    """Test ambient motion detection in prompts."""
    # Prompts with named ambient motion
    assert check_ambient_motion_named("Snowfall drifts in the doorway") is True
    assert check_ambient_motion_named("Steam rises from the pool") is True
    assert check_ambient_motion_named("The duck's pom bobs") is True
    assert check_ambient_motion_named("Aurora glowing softly") is True
    
    # Prompts without named ambient motion
    assert check_ambient_motion_named("A duck stands still") is False
    assert check_ambient_motion_named("The moose walks") is False


def test_suggest_ambient_motion_outdoor_snow():
    """Test ambient motion suggestions for outdoor snow scenes."""
    context = {
        "tags": ["exterior", "snow"],
        "room": "Way in: steps",
        "tod": "Afternoon, light snow",
    }
    
    suggestions = suggest_ambient_motion(context)
    
    # Should suggest snow-related motion
    assert any("snow" in s.lower() for s in suggestions)


def test_suggest_ambient_motion_pool():
    """Test ambient motion suggestions for pool scenes."""
    context = {
        "tags": ["pool", "steam"],
        "room": "Warm pool",
        "tod": "Golden hour",
    }
    
    suggestions = suggest_ambient_motion(context)
    
    # Should suggest water/steam motion
    assert any("steam" in s.lower() or "ripple" in s.lower() for s in suggestions)


def test_suggest_ambient_motion_aurora():
    """Test ambient motion suggestions for aurora night scenes."""
    context = {
        "tags": ["night", "aurora"],
        "room": "Sleeping deck",
        "tod": "Aurora night",
    }
    
    suggestions = suggest_ambient_motion(context)
    
    # Should suggest aurora/night-related motion
    assert any("aurora" in s.lower() or "glow" in s.lower() for s in suggestions)


def test_suggest_ambient_motion_animals():
    """Test ambient motion suggestions for animal scenes."""
    context = {
        "tags": ["moose", "duck"],
        "room": "Lobby",
        "tod": "Afternoon",
    }
    
    suggestions = suggest_ambient_motion(context)
    
    # Should suggest animal-related motion
    assert any(
        any(keyword in s.lower() for keyword in ["breath", "ear", "pom", "tail"])
        for s in suggestions
    )
