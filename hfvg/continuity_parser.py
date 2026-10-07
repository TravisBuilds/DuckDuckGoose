"""Continuity notes parser for character descriptions, sets, and lighting."""

from pathlib import Path
from typing import Any


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
) -> str:
    """
    Build generation prompt from shot + continuity notes.
    
    Combines:
    - Shot action and camera from beatmap
    - Character descriptions from continuity
    - Set/location from continuity (matching room in beatmap)
    - Time-of-day/lighting from continuity
    - Series style line from continuity
    
    Args:
        shot: Shot dict from beatmap parser
        continuity: Continuity dict from parse_continuity
    
    Returns:
        Complete prompt string for generation
    """
    parts = []
    
    # Add series style first (sets the visual tone)
    if continuity.get("style"):
        parts.append(continuity["style"])
    
    # Add characters with descriptions from continuity
    characters = shot.get("characters", [])
    if characters:
        char_descriptions = []
        for char_code in characters:
            if char_code in continuity.get("characters", {}):
                char_descriptions.append(continuity["characters"][char_code])
            else:
                char_descriptions.append(char_code)  # Fallback to code
        if char_descriptions:
            parts.append(", ".join(char_descriptions))
    
    # Add shot action (the main description)
    action = shot.get("action", "")
    if action:
        parts.append(action)
    
    # Add set/location with description from continuity
    room = shot.get("room", "")
    if room and room in continuity.get("sets", {}):
        parts.append(f"in {continuity['sets'][room]}")
    elif room:
        parts.append(f"in {room}")
    
    # Add time-of-day/lighting
    tod = shot.get("tod", "")
    if tod and tod in continuity.get("lighting", {}):
        parts.append(f"({continuity['lighting'][tod]})")
    elif tod:
        parts.append(f"({tod})")
    
    # Fallback if no data
    if not parts:
        shot_id = shot.get("shot_id", "unknown")
        parts.append(f"Scene {shot_id}")
    
    return " ".join(parts).strip()
