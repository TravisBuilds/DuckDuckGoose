"""Who spent the money: attribute each paid job to a person (docs/ACTUAL-COSTS.md).

job_ledger.user_id is the episode's owner when the job was submitted. Everyone who signs in with
the shared ADMIN_SECRET gets the same user id ("admin"), so on its own it cannot say who ran an
episode. Attribution, first match wins:

1. assigned     - an admin's explicit choice for the episode (episode_spend_owner)
2. run_starter  - the signed-in person who started the run the job belongs to (episode_runs: the
                  latest run started at or before the job)
3. episode_owner - the episode's owner (episodes.user_id)
4. job_owner    - the owner recorded on the job row
Ids that name nobody (the shared admin login, the legacy/shared and system ids, no id) never win;
a job with no person is reported as unattributed with the reason.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field

import aiosqlite

from hfvg.actual_costs import ensure_cost_schema, now_iso

SHARED_ADMIN_USER_ID = "admin"  # user id of every ADMIN_SECRET login (api/main.py login)


def _non_person_ids() -> frozenset[str]:
    from hfvg.users import LEGACY_USER_ID, SYSTEM_USER_ID
    return frozenset({SHARED_ADMIN_USER_ID, LEGACY_USER_ID, SYSTEM_USER_ID, ""})


def is_person(user_id: str | None) -> bool:
    return bool(user_id) and user_id not in _non_person_ids()


def _norm_ts(value: str | None) -> str:
    """Comparable UTC timestamp text: SQLite 'YYYY-MM-DD HH:MM:SS' and ISO forms both sort right."""
    return (value or "").replace(" ", "T")[:19]


async def record_run_start(db_path: str, episode_id: str, run_id: str, started_by: str | None,
                           started_by_role: str | None, via: str) -> None:
    """Record who started a run (idempotent per episode and run id)."""
    async with aiosqlite.connect(db_path) as db:
        await ensure_cost_schema(db)
        await db.execute(
            "INSERT OR IGNORE INTO episode_runs (episode_id, run_id, started_at, started_by, started_by_role, via) "
            "VALUES (?, ?, ?, ?, ?, ?)", (episode_id, run_id or "", now_iso(), started_by, started_by_role, via))
        await db.commit()


async def set_spend_owner(db_path: str, episode_id: str, user_id: str, set_by: str | None,
                          note: str | None = None) -> dict:
    async with aiosqlite.connect(db_path) as db:
        await ensure_cost_schema(db)
        stamp = now_iso()
        await db.execute(
            "INSERT INTO episode_spend_owner (episode_id, user_id, set_by, set_at, note) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(episode_id) DO UPDATE SET user_id = excluded.user_id, set_by = excluded.set_by, "
            "set_at = excluded.set_at, note = excluded.note", (episode_id, user_id, set_by, stamp, note))
        await db.commit()
    return {"episode_id": episode_id, "user_id": user_id, "set_by": set_by, "set_at": stamp, "note": note}


async def clear_spend_owner(db_path: str, episode_id: str) -> bool:
    async with aiosqlite.connect(db_path) as db:
        await ensure_cost_schema(db)
        cur = await db.execute("DELETE FROM episode_spend_owner WHERE episode_id = ?", (episode_id,))
        await db.commit()
        return cur.rowcount == 1


@dataclass
class Attribution:
    assigned: dict[str, str] = field(default_factory=dict)
    runs: dict[str, list[tuple[str, str | None, str | None]]] = field(default_factory=dict)  # (start, by, role)
    owners: dict[str, str | None] = field(default_factory=dict)

    def run_for(self, episode_id: str, at: str | None) -> tuple[str, str | None, str | None] | None:
        runs = self.runs.get(episode_id)
        if not runs:
            return None
        i = bisect_right([r[0] for r in runs], _norm_ts(at)) - 1
        return runs[i] if i >= 0 else None

    def attribute(self, episode_id: str, job_user_id: str | None = None,
                  at: str | None = None) -> tuple[str | None, str]:
        """(person user id or None, source). Source is one of assigned / run_starter /
        episode_owner / job_owner, or unattributed_shared_login / unattributed when nobody is known."""
        assigned = self.assigned.get(episode_id)
        if is_person(assigned):
            return assigned, "assigned"
        run = self.run_for(episode_id, at) if at else (self.runs.get(episode_id) or [None])[-1]
        if run and is_person(run[1]):
            return run[1], "run_starter"
        owner = self.owners.get(episode_id)
        if is_person(owner):
            return owner, "episode_owner"
        if is_person(job_user_id):
            return job_user_id, "job_owner"
        shared = (run is not None and (run[1] is None or run[1] == SHARED_ADMIN_USER_ID)) \
            or owner == SHARED_ADMIN_USER_ID or job_user_id == SHARED_ADMIN_USER_ID
        return None, "unattributed_shared_login" if shared else "unattributed"


async def load_attribution(db: aiosqlite.Connection) -> Attribution:
    await ensure_cost_schema(db)
    attr = Attribution()
    async with db.execute("SELECT episode_id, user_id FROM episode_spend_owner") as cur:
        async for episode_id, user_id in cur:
            attr.assigned[episode_id] = user_id
    async with db.execute(
        "SELECT episode_id, started_at, started_by, started_by_role FROM episode_runs ORDER BY started_at"
    ) as cur:
        async for episode_id, started_at, started_by, role in cur:
            attr.runs.setdefault(episode_id, []).append((_norm_ts(started_at), started_by, role))
    async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='episodes'") as cur:
        has_episodes = await cur.fetchone() is not None
    if has_episodes:
        async with db.execute("SELECT episode_id, user_id FROM episodes") as cur:
            async for episode_id, user_id in cur:
                attr.owners[episode_id] = user_id
    return attr
