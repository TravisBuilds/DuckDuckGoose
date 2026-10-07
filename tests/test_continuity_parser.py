"""Tests for continuity parser and prompt kit."""

import pytest
from pathlib import Path
import tempfile
import json

from hfvg.continuity_parser import (
    load_prompt_kit,
    build_prompt_with_continuity,
    get_character_refs,
)


def test_prompt_kit_resolves_characters(tmp_path):
    """
    Test: Prompt kit resolves character codes to descriptions.
    
    M-R3a will remove char_descriptions.append() - this test must fail.
    """
    # Create a prompt_kit.json
    episode_path = tmp_path / "ep99"
    episode_path.mkdir()
    
    prompt_kit = {
        "style": "Painterly 3D animation",
        "aspect_ratio": "9:16",
        "characters": {
            "M1": {"description": "Large brown moose with wide antlers"},
            "B": {"description": "Young birch moose, slender build"},
        },
        "sets": {}
    }
    
    prompt_kit_path = episode_path / "prompt_kit.json"
    prompt_kit_path.write_text(json.dumps(prompt_kit))
    
    # Load it
    loaded = load_prompt_kit(episode_path)
    assert loaded is not None
    
    # Build prompt with character codes
    shot = {
        "shot_id": "A01",
        "characters": ["M1", "B"],
        "action": "Walking through the forest",
        "room": "forest",
        "tod": "morning",
    }
    
    prompt = build_prompt_with_continuity(shot, {}, prompt_kit=loaded)
    
    # Must contain character descriptions, not bare codes
    assert "Large brown moose with wide antlers" in prompt
    assert "Young birch moose, slender build" in prompt
    assert "M1" not in prompt  # Code should not appear bare
    assert "B" not in prompt or " B " not in prompt  # Code should not appear bare


def test_prompt_kit_live_refuses_unresolved(tmp_path):
    """
    Test: Live mode with prompt_kit refuses unresolved character codes.
    
    M-R3b will disable the check - this test must fail.
    """
    episode_path = tmp_path / "ep99"
    episode_path.mkdir()
    
    # Prompt kit with only M1, missing M2
    prompt_kit = {
        "style": "Painterly 3D animation",
        "aspect_ratio": "9:16",
        "characters": {
            "M1": {"description": "Large brown moose"},
        },
        "sets": {}
    }
    
    prompt_kit_path = episode_path / "prompt_kit.json"
    prompt_kit_path.write_text(json.dumps(prompt_kit))
    
    loaded = load_prompt_kit(episode_path)
    
    # Shot with unresolved M2
    shot = {
        "shot_id": "A02",
        "characters": ["M1", "M2"],  # M2 is not in prompt_kit
        "action": "Two moose at the desk",
        "room": "lobby",
        "tod": "afternoon",
    }
    
    # Should raise ValueError about unresolved M2
    with pytest.raises(ValueError, match="Unresolved character codes"):
        build_prompt_with_continuity(shot, {}, prompt_kit=loaded)


def test_get_character_refs():
    """
    Test: get_character_refs extracts ref paths from prompt_kit.
    """
    with tempfile.TemporaryDirectory() as tmp:
        episode_path = Path(tmp) / "ep99"
        episode_path.mkdir()
        refs_dir = episode_path / "refs"
        refs_dir.mkdir()
        
        # Create ref files
        (refs_dir / "M1.png").write_text("fake image")
        (refs_dir / "B.jpg").write_text("fake image")
        
        prompt_kit = {
            "characters": {
                "M1": {"description": "Moose", "ref": "M1.png"},
                "B": {"description": "Birch", "ref": "B.jpg"},
                "W": {"description": "Willow"},  # No ref
            }
        }
        
        shot = {
            "characters": ["M1", "B", "W"],
        }
        
        refs = get_character_refs(shot, prompt_kit, episode_path)
        
        # Should return existing refs for M1 and B, skip W
        assert len(refs) == 2
        assert any("M1.png" in r for r in refs)
        assert any("B.jpg" in r for r in refs)


def test_aspect_ratio_in_prompt():
    """
    Test: 9:16 aspect_ratio adds vertical framing text to prompt.
    """
    prompt_kit = {
        "style": "Painterly 3D animation",
        "aspect_ratio": "9:16",
        "characters": {},
        "sets": {}
    }
    
    shot = {
        "shot_id": "A01",
        "characters": [],
        "action": "Scene description",
    }
    
    prompt = build_prompt_with_continuity(shot, {}, prompt_kit=prompt_kit)
    
    # Should include vertical 9:16 framing text
    assert "vertical 9:16 framing" in prompt or "9:16" in prompt
