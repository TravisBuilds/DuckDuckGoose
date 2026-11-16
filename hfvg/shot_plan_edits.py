"""History of shot plan edits (docs/PROPS.md): one row per changed field of a shot (a prompt, or
the shot's cast, props or place), with who changed it, the old and the new value and the run the
change went into. Stored in the studio database next to the props library (migration
``PROPS_LIBRARY_MIGRATION``); rows are only added."""

from __future__ import annotations

import json
from typing import Any

from hfvg.cast_store import _now, _tx, connect

TEXT_FIELDS = ("prompt", "end_prompt", "clip_prompt")
PICK_FIELDS = ("cast", "props", "place")
FIELDS = TEXT_FIELDS + PICK_FIELDS
VALUE_MAX = 20_000


def _encode(field: str, value: Any) -> str | None:
    if value is None:
        return None
    if field in TEXT_FIELDS:
        return str(value)[:VALUE_MAX]
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:VALUE_MAX]


def _decode(field: str, raw: str | None) -> Any:
    if raw is None or field in TEXT_FIELDS:
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def record(episode_id: str, edits: list[dict[str, Any]], *, actor: str | None, actor_name: str | None,
           run_id: str | None = None, note: str | None = None) -> int:
    """Store ``edits`` ([{"shot_id", "field", "old", "new"}]) in one transaction; the row count."""
    for edit in edits:
        if edit["field"] not in FIELDS:
            raise ValueError(f"unknown shot plan field {edit['field']!r}")
    now = _now()
    with connect() as conn, _tx(conn):
        for edit in edits:
            conn.execute(
                "INSERT INTO shot_plan_edits (episode_id, shot_id, field, old_value, new_value, actor, actor_name, "
                "run_id, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (episode_id, edit["shot_id"], edit["field"], _encode(edit["field"], edit.get("old")),
                 _encode(edit["field"], edit.get("new")), actor, actor_name, run_id, note or None, now))
    return len(edits)


def list_edits(episode_id: str, limit: int = 500) -> list[dict[str, Any]]:
    """The episode's edits, newest first."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, shot_id, field, old_value, new_value, actor, actor_name, run_id, note, created_at "
            "FROM shot_plan_edits WHERE episode_id = ? ORDER BY id DESC LIMIT ?", (episode_id, limit)).fetchall()
    return [{"id": r["id"], "shot_id": r["shot_id"], "field": r["field"],
             "old": _decode(r["field"], r["old_value"]), "new": _decode(r["field"], r["new_value"]),
             "actor": r["actor"], "actor_name": r["actor_name"], "run_id": r["run_id"], "note": r["note"],
             "at": r["created_at"]} for r in rows]
