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
    return parse_beatmap_text(Path(beatmap_path).read_text(encoding="utf-8"))


def parse_beatmap_text(content: str) -> list[dict[str, Any]]:
    """parse_beatmap for BEATMAP.md content already in memory (draft checks)."""
    # A shot table with a header row (| # | ID | Screen ... | Model |) is read by its header and every
    # row is checked; only documents without one fall back to the legacy row regex.
    shots = _parse_header_tables(content)
    if not shots:
        return _legacy_rows(content)
    seen = {s["shot_id"] for s in shots}
    stray = [s["shot_id"] for s in _legacy_rows(content) if s["shot_id"] not in seen]
    if stray:
        raise BeatmapFormatError(
            f"BEATMAP shot {stray[0]}: row is outside the shot table; keep every shot row directly under "
            "the table header (no blank lines or text between rows)", stray[0])
    return shots


# Format: | # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
# (rows that start with | number | shot_id | and end with | K or S |)
_LEGACY_ROW = re.compile(
    r'\|\s*(\d+)\s*\|\s*([A-Z]{1,3}\d+[a-zA-Z]*)\s*\|\s*([\d.]+)\s*\(([\d.]+)\)\s*(⇥?)\s*\|([^|]*)\|([^|]*)\|'
    r'([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|([^|]*)\|\s*([KS])')


def _legacy_rows(content: str) -> list[dict[str, Any]]:
    return [
        _build_shot(
            int(match.group(1)), match.group(2).strip(), float(match.group(3)), float(match.group(4)),
            '⇥' in match.group(5), match.group(6).strip(), match.group(7).strip(), match.group(8).strip(),
            match.group(9).strip(), match.group(10).strip(), match.group(13).strip(),
        )
        for match in _LEGACY_ROW.finditer(content)
    ]


def _build_shot(shot_num: int, shot_id: str, screen_time: float, gen_time: float, has_end_frame: bool,
                room: str, tod: str, chars_raw: str, action: str, purpose: str, model: str) -> dict[str, Any]:
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
    
    return shot


_MODEL_CODES = {"k": "K", "kling": "K", "s": "S", "seedance": "S"}
# Screen (gen) cell: "6 (6)", "6.0 (6.0)", "5.5 (6.0) ⇥"; a bare "6" or "6s" / "6s (6s)" reads as
# screen = gen. Anything else (ranges, words, "~6", "6 sec") is refused.
_SECONDS = r"(\d+(?:\.\d+)?)\s*(?:s(?![a-z]))?"
_SCREEN_RE = re.compile(rf"^{_SECONDS}\s*(?:\(\s*{_SECONDS}\s*\))?\s*(⇥?)\s*$", re.IGNORECASE)
SCREEN_CELL_FORMAT = "'<seconds> (<seconds>)' with no units, e.g. '6 (6)' or '6.0 (6.0)'"
MODEL_CELL_FORMAT = "K (Kling) or S (Seedance)"
_SHOWN_CELL_CHARS = 40
_SHOT_COLUMNS = ("#", "id", "screen", "room", "tod", "chars", "action", "purpose", "model")


class BeatmapFormatError(ValueError):
    """A BEATMAP shot row the parser refuses; the message says what the cell must look like."""

    def __init__(self, message: str, shot_id: str | None = None, row: str = ""):
        super().__init__(message)
        self.shot_id = shot_id
        self.row = row


def parse_screen_cell(screen: str) -> tuple[float, float, bool] | None:
    """(screen_time, gen_time, has_end_frame) of a Screen (gen) cell, None when it does not read as one."""
    m = _SCREEN_RE.match(screen.strip())
    if not m:
        return None
    screen_s = float(m.group(1))
    return screen_s, float(m.group(2)) if m.group(2) else screen_s, bool(m.group(3))


def _show(cell: str) -> str:
    return repr(cell if len(cell) <= _SHOWN_CELL_CHARS else cell[:_SHOWN_CELL_CHARS - 3] + "...")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _parse_header_tables(content: str) -> list[dict[str, Any]]:
    """Shot tables located by their header (| # | ID | Screen ... | Model |), any column order/extra
    columns. Rows marked REUSE in the screen cell are reused locked clips (hfvg.edit_plan), not
    generated, and are skipped. Any other row whose cells do not read (see SCREEN_CELL_FORMAT and
    MODEL_CELL_FORMAT) is refused with BeatmapFormatError: a shot is never silently dropped."""
    shots: list[dict[str, Any]] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("|"):
            head = [c.lower() for c in _cells(line)]
            def col(prefix: str) -> int | None:
                for j, h in enumerate(head):
                    if h == prefix or h.startswith(prefix):
                        return j
                return None
            idx = {k: col(k) for k in _SHOT_COLUMNS}
            if all(v is not None for v in idx.values()):
                i += 2  # skip the |---| separator
                while i < len(lines) and lines[i].lstrip().startswith("|"):
                    row = lines[i].strip()
                    cells = _cells(row)
                    i += 1
                    if len(cells) < len(head):
                        raise BeatmapFormatError(
                            f"BEATMAP row has {len(cells)} cells, header has {len(head)}: {cells[:2]}", row=row)
                    sid, screen = cells[idx["id"]], cells[idx["screen"]]
                    if "REUSE" in screen.upper():
                        continue
                    if any(cells[len(head):]):
                        raise BeatmapFormatError(
                            f"BEATMAP shot {sid}: row has {len(cells)} cells, header has {len(head)}; a '|' "
                            "inside a cell splits it (write the text without '|')", sid, row)
                    timing = parse_screen_cell(screen)
                    if timing is None:
                        raise BeatmapFormatError(
                            f"BEATMAP shot {sid}: Screen (gen) cell {_show(screen)} must read {SCREEN_CELL_FORMAT}", sid, row)
                    model_cell = cells[idx["model"]]
                    model = _MODEL_CODES.get(model_cell.strip().lower())
                    if not model:
                        raise BeatmapFormatError(
                            f"BEATMAP shot {sid}: Model cell {_show(model_cell)} must be {MODEL_CELL_FORMAT}", sid, row)
                    number = cells[idx["#"]]
                    if not number.isdigit():
                        raise BeatmapFormatError(
                            f"BEATMAP shot {sid}: # cell {_show(number)} must be the shot number (1, 2, 3 ...)", sid, row)
                    shots.append(_build_shot(
                        int(number), sid, *timing,
                        cells[idx["room"]], cells[idx["tod"]], cells[idx["chars"]], cells[idx["action"]],
                        cells[idx["purpose"]], model,
                    ))
                continue
        i += 1
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
