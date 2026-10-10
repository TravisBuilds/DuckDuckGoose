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
    
    M-R3b will disable the check - this test must fail with AssertionError.
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
    # M-R3b will disable this check, causing no exception
    with pytest.raises(ValueError) as exc_info:
        build_prompt_with_continuity(shot, {}, prompt_kit=loaded)
    
    # Explicitly assert the exception message (M-R3b must fail with AssertionError)
    assert "Unresolved character codes" in str(exc_info.value), \
        f"Expected 'Unresolved character codes' in error, got: {exc_info.value}"


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
    Test: 9:16 aspect_ratio should NOT duplicate vertical framing text.
    
    The style should already contain aspect ratio info. Adding it again
    causes duplication like "vertical 9:16 frame vertical 9:16 framing".
    """
    prompt_kit = {
        "style": "Painterly 3D animation, vertical 9:16 frame",
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
    
    # Should not duplicate vertical/9:16 text
    assert prompt.count("vertical") <= 1, f"Duplicated 'vertical' in prompt: {prompt}"
    assert prompt.count("9:16") <= 1, f"Duplicated '9:16' in prompt: {prompt}"


def test_multi_letter_character_codes():
    """
    Test: Beat-map parser handles multi-letter codes like AG, B, W, M1.
    
    Regex must be [A-Z]+\d* to match both multi-letter codes and digits.
    """
    from hfvg.episode_parser import parse_beatmap
    import tempfile
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
        # Simple beatmap with various character code formats
        f.write("""
| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|----|--------------|----|-----|-------|--------|---------|----|----|-------|
| 1 | A01 | 5.0 (7.0) | lobby | day | B W | Two characters | Test | fade | cut | K |
| 2 | A02 | 5.0 (7.0) | lobby | day | M1 M2 | Moose variants | Test | cut | fade | S |
| 3 | A03 | 5.0 (7.0) | lobby | day | AG B | Multi-letter code | Test | cut | fade | K |
""")
        f.flush()
        beatmap_path = f.name
    
    shots = parse_beatmap(beatmap_path)
    
    # Check shot 1: B, W (single letters)
    assert shots[0]["characters"] == ["B", "W"]
    
    # Check shot 2: M1, M2 (letter + digit)
    assert shots[1]["characters"] == ["M1", "M2"]
    
    # Check shot 3: AG, B (multi-letter + single letter)
    assert shots[2]["characters"] == ["AG", "B"], \
        f"Expected ['AG', 'B'], got {shots[2]['characters']}"
    
    import os
    os.unlink(beatmap_path)
