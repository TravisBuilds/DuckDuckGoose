"""Continuity notes parser for character descriptions, sets, and lighting.

This module supports two formats:
1. prompt_kit.json (preferred): Explicit per-episode configuration
2. CONTINUITY.md (legacy): Parsed from markdown headings

Live mode REQUIRES prompt_kit.json. Dry mode falls back to CONTINUITY.md.
"""

from pathlib import Path
from typing import Any
import json


def load_prompt_kit(episode_path: str | Path) -> dict[str, Any] | None:
    """
    Load prompt_kit.json for an episode.
    
    Format:
    {
      "style": "<series style line>",
      "aspect_ratio": "9:16",
      "characters": {
        "<CODE>": {
          "description": "<text>",
          "ref": "<optional filename in episode refs/>"
        }
      },
      "sets": {
        "<SET_ID>": "<text>"
      }
    }
    
    Args:
        episode_path: Path to episode directory (e.g., data/episodes/ep04)
    
    Returns:
        Parsed prompt kit dict, or None if file doesn't exist
    """
    episode_path = Path(episode_path)
    prompt_kit_path = episode_path / "prompt_kit.json"
    
    if not prompt_kit_path.exists():
        return None
    
    try:
        with open(prompt_kit_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        raise ValueError(f"Failed to load prompt_kit.json: {e}")


def parse_continuity(continuity_path: str | Path) -> dict[str, Any]:
    """
    Parse CONTINUITY.md into structured data.
    
    Expected format (simplified):
    
    ## Characters
    - CharCode: Description
    
    ## Sets
    - SetName: Location description
    
    ## Lighting
    - TimeOfDay: Lighting description
    
    ## Style
    Series visual style line
    
    Returns:
        {
            "characters": {"CharCode": "description", ...},
            "sets": {"SetName": "description", ...},
            "lighting": {"TimeOfDay": "description", ...},
            "style": "visual style line"
        }
    """
    if not Path(continuity_path).exists():
        return {
            "characters": {},
            "sets": {},
            "lighting": {},
            "style": ""
        }
    
    content = Path(continuity_path).read_text()
    
    result = {
        "characters": {},
        "sets": {},
        "lighting": {},
        "style": ""
    }
    
    current_section = None
    
    for line in content.split("\n"):
        line = line.strip()
        
        # Section headers
        if line.startswith("## Characters"):
            current_section = "characters"
        elif line.startswith("## Sets"):
            current_section = "sets"
        elif line.startswith("## Lighting") or line.startswith("## Time of Day"):
            current_section = "lighting"
        elif line.startswith("## Style"):
            current_section = "style"
        # Parse entries
        elif line.startswith("- ") and current_section in ["characters", "sets", "lighting"]:
            # Format: - Key: Description
            if ":" in line:
                key_part, desc_part = line[2:].split(":", 1)
                key = key_part.strip()
                desc = desc_part.strip()
                result[current_section][key] = desc
        elif line and current_section == "style":
            # Style is a paragraph after ## Style header
            if not line.startswith("##") and not line.startswith("-"):
                if result["style"]:
                    result["style"] += " " + line
                else:
                    result["style"] = line
    
    return result


def build_prompt_with_continuity(
    shot: dict[str, Any],
    continuity: dict[str, Any],
    prompt_kit: dict[str, Any] | None = None,
) -> str:
    """
    Build generation prompt from shot + continuity notes + prompt_kit.
    
    Priority: prompt_kit (if available) > continuity (legacy fallback)
    
    Combines:
    - Series style line
    - Character descriptions (resolved from codes)
    - Shot action and camera from beatmap
    - Set/location
    - Time-of-day/lighting
    - Aspect ratio (from prompt_kit)
    
    Args:
        shot: Shot dict from beatmap parser
        continuity: Continuity dict from parse_continuity (legacy fallback)
        prompt_kit: prompt_kit.json dict (preferred source)
    
    Returns:
        Complete prompt string for generation
        
    Raises:
        ValueError: In live mode if character code is unresolved
    """
    # Use prompt_kit if available, otherwise fall back to continuity
    source = prompt_kit if prompt_kit else continuity
    
    parts = []
    
    # Add series style first (sets the visual tone)
    style = source.get("style", "")
    if style:
        parts.append(style)
    
    # Add aspect_ratio text if specified in prompt_kit
    if prompt_kit and prompt_kit.get("aspect_ratio") == "9:16":
        parts.append("vertical 9:16 framing")
    
    # Add characters with descriptions
    characters = shot.get("characters", [])
    if characters:
        char_descriptions = []
        unresolved_codes = []
        
        for char_code in characters:
            if prompt_kit and "characters" in prompt_kit:
                # prompt_kit format: {"characters": {"CODE": {"description": "...", "ref": "..."}}}
                char_info = prompt_kit["characters"].get(char_code)
                if char_info and "description" in char_info:
                    char_descriptions.append(char_info["description"])
                else:
                    unresolved_codes.append(char_code)
            elif char_code in source.get("characters", {}):
                # continuity format: {"characters": {"CODE": "description"}}
                char_descriptions.append(source["characters"][char_code])
            else:
                unresolved_codes.append(char_code)
        
        # In live mode with prompt_kit, refuse unresolved codes
        if prompt_kit and unresolved_codes:
            raise ValueError(
                f"Unresolved character codes in prompt_kit: {unresolved_codes}. "
                f"All character codes in the beat map must be defined in prompt_kit.json characters section."
            )
        
        # Add resolved descriptions
        if char_descriptions:
            parts.append(", ".join(char_descriptions))
        
        # In dry mode, fall back to bare codes if needed
        if unresolved_codes and not prompt_kit:
            char_descriptions.extend(unresolved_codes)
    
    # Add shot action (the main description)
    action = shot.get("action", "")
    if action:
        parts.append(action)
    
    # Add camera if present
    camera = shot.get("camera", "")
    if camera:
        parts.append(camera)
    
    # Add set/location with description
    room = shot.get("room", "")
    if room:
        if prompt_kit and "sets" in prompt_kit and room in prompt_kit["sets"]:
            # prompt_kit sets: {"SET_ID": "description"}
            parts.append(f"in {prompt_kit['sets'][room]}")
        elif room in source.get("sets", {}):
            # continuity sets: {"SET_ID": "description"}
            parts.append(f"in {source['sets'][room]}")
        else:
            parts.append(f"in {room}")
    
    # Add time-of-day/lighting
    tod = shot.get("tod", "")
    if tod:
        if tod in source.get("lighting", {}):
            parts.append(f"({source['lighting'][tod]})")
        else:
            parts.append(f"({tod})")
    
    # Fallback if no data
    if not parts:
        shot_id = shot.get("shot_id", "unknown")
        parts.append(f"Scene {shot_id}")
    
    return " ".join(parts).strip()


def get_character_refs(
    shot: dict[str, Any],
    prompt_kit: dict[str, Any],
    episode_path: str | Path,
) -> list[str]:
    """
    Get character reference file paths from prompt_kit.
    
    Args:
        shot: Shot dict with characters list
        prompt_kit: prompt_kit.json dict
        episode_path: Path to episode directory
    
    Returns:
        List of absolute paths to character reference images (max 3)
    """
    refs = []
    characters = shot.get("characters", [])
    
    for char_code in characters[:3]:  # Limit to 3 refs
        char_info = prompt_kit.get("characters", {}).get(char_code)
        if char_info and "ref" in char_info:
            ref_filename = char_info["ref"]
            ref_path = Path(episode_path) / "refs" / ref_filename
            if ref_path.exists():
                refs.append(str(ref_path.absolute()))
    
    return refs
