"""Kept starts (docs/CREATION-FLOW-FIXES.md): the start request the new video wizard keeps for an
episode that waits on its page before it starts, while its owner picks the shots' first frames or
locks the looks of a Universe's characters. The episode page's Start card sends it (POST
/api/episodes), on whichever device the owner opens the page. One row per episode, replaced when the
wizard keeps another and cleared once the episode starts.

Only the wizard's part of a start is kept (dry run or live, story shaping, card, idea, length): the
start itself runs every check again. Table from hfvg.migrations.KEPT_STARTS_MIGRATION; sync sqlite3
like the cast store (the API calls these through a worker thread).
"""

from __future__ import annotations

import json
from typing import Any

from hfvg.cast_store import _now, _tx, audit, connect

FIELDS = ("dry_run", "enable_story_shaping", "archetype_id", "idea", "target_duration")


def _request(episode_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """The start request as POST /api/episodes takes it: a run is live only when it says so."""
    out: dict[str, Any] = {"episode_id": episode_id, "dry_run": raw.get("dry_run") is not False,
                           "enable_story_shaping": raw.get("enable_story_shaping") is not False}
    if isinstance(raw.get("archetype_id"), str) and raw["archetype_id"]:
        out["archetype_id"] = raw["archetype_id"]
    if isinstance(raw.get("idea"), str) and raw["idea"]:
        out["idea"] = raw["idea"]
    if isinstance(raw.get("target_duration"), int) and not isinstance(raw.get("target_duration"), bool):
        out["target_duration"] = raw["target_duration"]
    return out


def _view(row) -> dict[str, Any]:
    try:
        raw = json.loads(row["request"])
    except ValueError:
        raw = {}
    return {"request": _request(row["episode_id"], raw if isinstance(raw, dict) else {}),
            "kept_by": row["kept_by"], "kept_at": row["updated_at"]}


def keep(episode_id: str, request: dict[str, Any], actor: str | None) -> dict[str, Any]:
    """Keep the start request for the episode (replacing one kept before)."""
    body = _request(episode_id, {k: request.get(k) for k in FIELDS})
    text = json.dumps({k: v for k, v in body.items() if k != "episode_id"}, sort_keys=True)
    now = _now()
    with connect() as conn:
        with _tx(conn):
            conn.execute(
                "INSERT INTO episode_kept_starts (episode_id, request, kept_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(episode_id) DO UPDATE SET request = excluded.request, "
                "kept_by = excluded.kept_by, updated_at = excluded.updated_at",
                (episode_id, text, actor, now, now))
            audit(conn, actor, "kept_start_saved", episode_id=episode_id,
                  details={"dry_run": body["dry_run"]})
        row = conn.execute("SELECT * FROM episode_kept_starts WHERE episode_id = ?", (episode_id,)).fetchone()
    return _view(row)


def get(episode_id: str) -> dict[str, Any] | None:
    """The start kept for the episode, or None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM episode_kept_starts WHERE episode_id = ?", (episode_id,)).fetchone()
    return _view(row) if row else None


def clear(episode_id: str, actor: str | None) -> bool:
    """Forget the start kept for the episode (it started); True when there was one."""
    with connect() as conn:
        with _tx(conn):
            gone = conn.execute("DELETE FROM episode_kept_starts WHERE episode_id = ?", (episode_id,)).rowcount
            if gone:
                audit(conn, actor, "kept_start_cleared", episode_id=episode_id)
    return bool(gone)
