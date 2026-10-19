"""Episode parser for BRIEF/BEATMAP document format.

Parses episode 3-style BEATMAP.md into shot plans for workflow execution.
"""

import re
from typing import Any
from pathlib import Path


def parse_beatmap(beatmap_path: str | Path) -> list[dict[str, Any]]:
    """
    Parse BEATMAP.md into shot list with metadata.
    
    Returns list of shots with:
    - shot_id: e.g., "A01", "B02"
    - scene_id: e.g., "A", "B", "O"
    - screen_time: float (seconds)
    - gen_time: float (seconds to generate)
    - room: string
    - tod: time of day
    - characters: list of character codes
    - action: description
    - purpose: narrative purpose
    - model: "K" (Kling) or "S" (Seedance)
    - tags: list of tags for routing (wet, mud, pool, soak, bare, underwater, etc.)
    - is_handoff: bool (marked with ★)
    - has_end_frame: bool (marked with ⇥)
    - duck_role: "host", "absent", or "present"
    """
    content = Path(beatmap_path).read_text()
    
    shots = []
    
    # Find all table rows with shot data
    # Format: | # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
    # Need to match rows that start with | number | shot_id | and end with | K or S |
    pattern = r'\|\s*(\d+)\s*\|\s*([A-Z]\d+[a-zA-Z]*)\s*\|\s*([\d.]+)\s*\(([\d.]+)\)\s*(⇥?)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*([KS])'
    
    for match in re.finditer(pattern, content, re.MULTILINE):
        shot_num = int(match.group(1))
        shot_id = match.group(2).strip()
        screen_time = float(match.group(3))
        gen_time = float(match.group(4))
        has_end_frame = '⇥' in match.group(5)
        room = match.group(6).strip()
        tod = match.group(7).strip()
        chars_raw = match.group(8).strip()
        action = match.group(9).strip()
        purpose = match.group(10).strip()
        # Skip groups 11 and 12 (In and Out transitions)
        model = match.group(13).strip()
        
        # Extract scene ID (letter prefix)
        scene_id = shot_id[0]
        
        # Parse characters
        # Format examples: "H Y K", "D(T)", "K(tuque) D(B)", "–"
        characters = []
        duck_role = "absent"
        
        if chars_raw != '–':
            # Match character codes: multi-letter + optional digit (e.g., B, W, M1, M2, AG)
            char_codes = re.findall(r'([A-Z]+\d*)(?:\([^)]*\))?', chars_raw)
            for code in char_codes:
                characters.append(code)
                if code == 'D':
                    duck_role = "host"  # Duck present means host role
        
        # Detect tags for model routing
        tags = []
        action_lower = action.lower()
        purpose_lower = purpose.lower()
        room_lower = room.lower()
        combined = f"{action_lower} {purpose_lower} {room_lower}"
        
        # Wet/water tags (route to Kling)
        if any(keyword in combined for keyword in [
            'water', 'wet', 'soak', 'swim', 'dive', 'underwater', 'splash',
            'pool', 'spring', 'surface', 'steam'
        ]):
            if 'underwater' in combined:
                tags.append('underwater')
            if 'soak' in combined or 'spring' in combined:
                tags.append('soak')
            if 'wet' in combined or 'swim' in combined:
                tags.append('wet')
        
        # Mud (route to Kling)
        if 'mud' in combined:
            tags.append('mud')
        
        # Hand-offs (marked with ★)
        is_handoff = '★' in action or 'Hand-off' in action
        
        shot = {
            "shot_num": shot_num,
            "shot_id": shot_id,
            "scene_id": scene_id,
            "screen_time": screen_time,
            "gen_time": gen_time,
            "room": room,
            "tod": tod,
            "characters": characters,
            "action": action,
            "purpose": purpose,
            "model": model,  # K or S
            "tags": tags,
            "is_handoff": is_handoff,
            "has_end_frame": has_end_frame,
            "duck_role": duck_role,
        }
        
        shots.append(shot)
    
    return shots


def build_prompt_from_shot(shot: dict[str, Any]) -> str:
    """
    Build a generation prompt from beatmap shot metadata.
    
    Args:
        shot: Shot dict with fields like action, characters, duck_role, room, tod, etc.
    
    Returns:
        A descriptive prompt string for image/video generation
    """
    parts = []
    
    # Add duck role if present
    duck_role = shot.get("duck_role")
    if duck_role:
        parts.append(f"{duck_role} duck")
    
    # Add characters
    characters = shot.get("characters", [])
    if characters:
        chars_str = ", ".join(characters)
        if duck_role:
            parts.append(f"with {chars_str}")
        else:
            parts.append(chars_str)
    
    # Add action
    action = shot.get("action", "")
    if action:
        parts.append(action)
    
    # Add location context
    room = shot.get("room", "")
    tod = shot.get("tod", "")
    if room:
        location = f"in {room}"
        if tod:
            location += f" ({tod})"
        parts.append(location)
    
    # Fallback if no data
    if not parts:
        shot_id = shot.get("shot_id", "unknown")
        parts.append(f"Scene {shot_id}")
    
    return " ".join(parts).strip()


def get_model_routing(shot: dict[str, Any]) -> dict[str, Any]:
    """
    Determine model routing per PIPELINE-LESSONS.md rules.
    
    Returns:
        {
            "model": "kling_3.0" or "seedance_2.5",
            "resolution": "1080p", "720p", or "480p",
            "draft": bool,
            "reason": str
        }
    """
    # Model K in BEATMAP = Kling 3.0 pro
    # Model S in BEATMAP = Seedance 2.5
    
    if shot["model"] == "K":
        # Kling: wet/water/landscape/single-character
        # Always 1080p native, draft=final
        return {
            "model": "kling_3.0",
            "resolution": "1080p",
            "draft": False,
            "reason": "Kling for wet/landscape (BEATMAP K, draft=final)",
        }
    
    elif shot["model"] == "S":
        # Seedance: dry multi-character or hand-offs
        # Draft: 480p, Final: 720p (NOT 1080p unless hero)
        # From BEATMAP: only A05 and F02 are finalized at 1080p
        is_hero = shot["shot_id"] in ["A05", "F02"]
        
        return {
            "model": "seedance_2.5",
            "resolution": "1080p" if is_hero else "720p",
            "draft": True,  # Always draft first, finalize after approval
            "reason": f"Seedance for dry multi-char (BEATMAP S, hero={is_hero})",
        }
    
    # Fallback
    return {
        "model": "kling_3.0",
        "resolution": "1080p",
        "draft": False,
        "reason": "Default routing",
    }


def shots_by_scene(shots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group shots by scene_id."""
    scenes = {}
    for shot in shots:
        scene_id = shot["scene_id"]
        if scene_id not in scenes:
            scenes[scene_id] = []
        scenes[scene_id].append(shot)
    return scenes
