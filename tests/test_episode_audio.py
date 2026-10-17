"""Test episode audio activities signature."""

import pytest
import inspect
from hfvg.activities.audio import generate_voiceover, generate_sfx, generate_music


def test_audio_activities_signature():
    """Test that audio activities have episode_id as first parameter."""
    # Check generate_voiceover signature
    sig = inspect.signature(generate_voiceover)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_voiceover first param should be episode_id, got {params[0]}"
    assert params[1] == "script", f"generate_voiceover second param should be script, got {params[1]}"
    assert params[2] == "voice_id", f"generate_voiceover third param should be voice_id, got {params[2]}"
    
    # Check generate_sfx signature
    sig = inspect.signature(generate_sfx)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_sfx first param should be episode_id, got {params[0]}"
    assert params[1] == "description", f"generate_sfx second param should be description, got {params[1]}"
    
    # Check generate_music signature
    sig = inspect.signature(generate_music)
    params = list(sig.parameters.keys())
    assert params[0] == "episode_id", f"generate_music first param should be episode_id, got {params[0]}"
    assert params[1] == "style", f"generate_music second param should be style, got {params[1]}"
    assert params[2] == "duration", f"generate_music third param should be duration, got {params[2]}"
