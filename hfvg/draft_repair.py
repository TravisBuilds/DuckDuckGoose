"""Checks and repair prompts for LLM drafts (POST /api/episodes/draft-from-idea).

A drafted BEATMAP.md must parse (hfvg.episode_parser) and list the shots of the drafted shots.json,
in the same order and with the same lengths. When it does not, the draft route sends the exact
problem back to the model with the table format and an example row; a response that left out files
is asked again for just those files.
"""

import json
import logging
from typing import Any

from hfvg.episode_parser import MODEL_CELL_FORMAT, SCREEN_CELL_FORMAT, BeatmapFormatError, parse_beatmap_text

REQUIRED_FILES = ("beatmap_md", "shots_json", "refs_json", "prompt_kit_json")
MAX_BEATMAP_REPAIRS = 2
MAX_FILE_RETRIES = 2
TABLE_HEADER = "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |"
TABLE_SEPARATOR = "|---|---|---|---|---|---|---|---|---|---|---|"
EXAMPLE_ROW = "| 1 | S01 | 6 (6) | Kitchen | Morning | A | A kettle starts to steam | Opening image | - | - | K |"
PROMPT_EXAMPLE_ROW = ("| 1 | S01 | 6 (6) | [location] | [time] | A | [action description] | [narrative purpose] "
                      "| - | - | K |")
TABLE_RULES = (
    f"Screen (gen): {SCREEN_CELL_FORMAT}; the first number is the shot's seconds on screen, the "
    "second its generated seconds (write the same number twice)",
    f"Model: {MODEL_CELL_FORMAT}",
    "Every row has exactly the 11 cells of the header; never put a '|' inside a cell",
)
_LOG_ROW_CHARS = 300
_LOG_CELL_CHARS = 24

log = logging.getLogger(__name__)


def format_help() -> str:
    """One sentence on the shot-table format, for error messages shown to the user."""
    return (f"Each shot row needs the 11 cells of {TABLE_HEADER} with Screen (gen) written "
            f"{SCREEN_CELL_FORMAT} and Model {MODEL_CELL_FORMAT}, for example {EXAMPLE_ROW}")


def table_rules_text() -> str:
    return "\n".join(f"- {rule}" for rule in TABLE_RULES)


def missing_files(result: Any) -> list[str]:
    """Required draft keys the model left out (all of them when the response is not a JSON object)."""
    if not isinstance(result, dict):
        return list(REQUIRED_FILES)
    return [key for key in REQUIRED_FILES if not result.get(key)]


def fill_refs(result: dict) -> bool:
    """Drafts never attach reference images (every per_shot list is empty), so a missing refs_json
    is rebuilt from the drafted shots instead of asking the model again. True when it was rebuilt."""
    if result.get("refs_json"):
        return False
    shots_data = result.get("shots_json")
    if isinstance(shots_data, str):
        try:
            shots_data = json.loads(shots_data)
        except ValueError:
            return False
    shots = _planned(shots_data)
    if not shots:
        return False
    result["refs_json"] = {"per_shot": {str(s.get("id")): [] for s in shots}}
    return True


def _planned(shots_data: Any) -> list[dict] | None:
    shots = shots_data.get("shots") if isinstance(shots_data, dict) else None
    if not isinstance(shots, list) or not all(isinstance(s, dict) for s in shots):
        return None
    return shots


def _row_of(content: str, shot_id: str) -> str:
    for line in content.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.lstrip().startswith("|") and shot_id in cells:
            return line.strip()
    return ""


def check_beatmap(content: Any, shots_data: Any) -> tuple[list[dict] | None, str | None, str]:
    """(parsed shots, problem, offending row). problem is None when the table is usable: every row
    parses and the table lists the shots.json shots in order with the same lengths. shots.json
    itself is validated later (hfvg.shot_data.parse_shots); without a shot list only parsing is checked."""
    if not isinstance(content, str) or not content.strip():
        return None, "beatmap_md is empty", ""
    try:
        parsed = parse_beatmap_text(content)
    except BeatmapFormatError as e:
        return None, str(e), e.row
    except ValueError as e:
        return None, str(e), ""
    if not parsed:
        return None, f"BEATMAP.md has no shot table (header {TABLE_HEADER} followed by one row per shot)", ""
    planned = _planned(shots_data)
    if planned is None:
        return parsed, None, ""
    table_ids = [p["shot_id"] for p in parsed]
    planned_ids = [str(s.get("id")) for s in planned]
    if table_ids != planned_ids:
        return None, (f"the BEATMAP table lists shots {table_ids} but shots_json has {planned_ids}; "
                      "write one row per shots_json shot, same ids, same order"), ""
    for row, shot in zip(parsed, planned):
        try:
            seconds = float(shot.get("duration"))
        except (TypeError, ValueError):
            return parsed, None, ""  # parse_shots reports the bad duration
        if abs(row["screen_time"] - seconds) > 1e-6:
            n = f"{seconds:g}"
            return None, (f"BEATMAP shot {row['shot_id']}: Screen (gen) says {row['screen_time']:g} s but "
                          f"shots_json gives the shot {n} s; write '{n} ({n})'"), _row_of(content, row["shot_id"])
    return parsed, None, ""


def repair_prompt(problem: str, beatmap: str, shots_data: Any) -> str:
    planned = _planned(shots_data) or []
    lengths = ", ".join(f"{s.get('id')} {s.get('duration')} s" for s in planned)
    keep = f"- Keep exactly these shots, in this order and with these lengths: {lengths}\n" if lengths else ""
    return (f"This BEATMAP.md cannot be used: {problem}\n\n"
            "Rewrite BEATMAP.md so its shot table follows these rules exactly:\n"
            f"- Header: {TABLE_HEADER}\n- Separator: {TABLE_SEPARATOR}\n{table_rules_text()}\n"
            f"- Example row: {EXAMPLE_ROW}\n{keep}"
            'Return a JSON object with one key, "beatmap_md", holding the whole corrected BEATMAP.md.\n\n'
            f"BEATMAP.md:\n{beatmap}")


def missing_files_prompt(missing: list[str]) -> str:
    keys = ", ".join(f'"{k}"' for k in missing)
    return (f"Your response did not include {keys}. Reply with a JSON object holding exactly these keys "
            "for the same video, consistent with the files you already wrote.")


def loggable_row(row: str) -> str:
    """The offending row with its free-text cells (action, purpose: where the idea's words land)
    replaced by their length, so the log shows the row's shape and cells but not the idea."""
    if not row:
        return ""
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    shown = [c if len(c) <= _LOG_CELL_CHARS else f"<{len(c)} chars>" for c in cells]
    return ("| " + " | ".join(shown) + " |")[:_LOG_ROW_CHARS]


def log_problem(stage: str, *, episode_id: str, card: str | None, target_s: int, attempt: int,
                problem: str, row: str = "") -> None:
    """Log a draft problem with what is needed to reproduce it: never the idea text or credentials."""
    log.warning("draft-from-idea %s: episode=%s card=%s length=%ss attempt=%d problem=%s row=%s",
                stage, episode_id, card or "freeform", target_s, attempt, problem[:_LOG_ROW_CHARS],
                loggable_row(row))
