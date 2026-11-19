"""Kept starts (hfvg/kept_starts.py, POST /api/episodes/{id}/kept-start): the start request the new
video wizard keeps for an episode that waits on its page (for its first frames or a Universe lock)
lives in the studio, so the page's Start card works on any device. Only the episode's owner or an
admin keeps it and sees it; the start clears it. The table comes from an additive migration."""

from __future__ import annotations

import io
import json
import sqlite3

from hfvg import kept_starts
from tests.test_cast_api import EP, IDEA, OTHER_EP, REFUSED, _client, _running, studio  # noqa: F401

CARD = "nostalgia-time-travel"
WIZARD = {"episode_id": EP, "dry_run": True, "enable_story_shaping": True, "archetype_id": CARD, "idea": IDEA,
          "target_duration": 30}


def _uploaded(client, ep: str = EP) -> None:
    draft = client.post("/api/episodes/draft-from-idea", json={"idea": IDEA, "target_duration": 30}).json()
    res = client.post("/api/episodes/upload", data={"episode_id": ep}, files={
        "beatmap_file": ("BEATMAP.md", io.BytesIO(draft["beatmap"].encode()), "text/markdown")})
    assert res.status_code == 200, res.text


def _view(client, ep: str = EP) -> dict:
    res = client.get(f"/api/episodes/{ep}/universe")
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------------------------------- the store


def test_a_kept_start_is_read_back_replaced_and_cleared(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "kept.db"))
    assert kept_starts.get("ep10kept") is None
    kept = kept_starts.keep("ep10kept", {**WIZARD, "episode_id": "ep10kept", "dry_run": False}, "u-owner")
    assert kept["request"] == {**WIZARD, "episode_id": "ep10kept", "dry_run": False}
    assert kept["kept_by"] == "u-owner" and kept["kept_at"]
    assert kept_starts.get("ep10kept") == kept
    # another start replaces it; only the wizard's fields are kept, and a run is live only when it says so
    again = kept_starts.keep("ep10kept", {"dry_run": "false", "budget": 9, "shots_subset": ["A01"]}, "admin")
    assert again["request"] == {"episode_id": "ep10kept", "dry_run": True, "enable_story_shaping": True}
    assert kept_starts.get("ep11kept") is None
    assert kept_starts.clear("ep10kept", "u-owner") is True
    assert kept_starts.get("ep10kept") is None and kept_starts.clear("ep10kept", "u-owner") is False
    with sqlite3.connect(tmp_path / "kept.db") as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM cast_audit_log WHERE episode_id = 'ep10kept' "
                                              "ORDER BY id")]
    assert actions == ["kept_start_saved", "kept_start_saved", "kept_start_cleared"]


async def test_the_migration_adds_only_its_table_and_changes_nothing_that_was_there(tmp_path):
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import (
        KEPT_STARTS_MIGRATION,
        apply_kept_starts_sync,
        migrate_cast_library,
        migrate_concept_frames,
        migrate_props_library,
        migrate_universe,
        migrate_universe_board,
        migrate_universe_story,
        run_all_migrations,
    )
    from hfvg.studio_db import create_episode, init_studio_db
    db = str(tmp_path / "existing.db")
    await init_studio_db(db)
    ledger = BudgetLedger(db)
    await ledger.init_db()
    for migrate in (migrate_cast_library, migrate_props_library, migrate_universe, migrate_universe_board,
                    migrate_universe_story, migrate_concept_frames):
        await migrate(db)
    await create_episode(db, "ep07", user_id="u-owner")
    await ledger.init_episode_budget("ep07")

    def snapshot():
        with sqlite3.connect(db) as conn:
            schema = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN "
                                                       "('table', 'index') AND name NOT LIKE 'sqlite_%'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "budget_lines", "series", "concept_frame_sets")}
        return schema, data

    schema_before, data_before = snapshot()
    await run_all_migrations(db)
    await run_all_migrations(db)
    schema_after, data_after = snapshot()
    assert {k: v for k, v in schema_after.items() if k in schema_before} == schema_before
    assert {n for n in set(schema_after) - set(schema_before) if not n.startswith("idx_")} == {"episode_kept_starts"}
    assert data_after == data_before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                            (KEPT_STARTS_MIGRATION,)).fetchone()[0] == 1
        assert apply_kept_starts_sync(conn) is False


def test_the_worker_side_gets_the_table_on_a_db_without_it(tmp_path, monkeypatch):
    from hfvg.migrations import apply_cast_library_sync
    db = tmp_path / "older.db"
    conn = sqlite3.connect(db, isolation_level=None)
    apply_cast_library_sync(conn)
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    assert kept_starts.get("ep07") is None
    with sqlite3.connect(db) as check:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "episode_kept_starts" in tables


# ------------------------------------------------------------------------------------- the API


async def test_the_owner_keeps_the_start_and_finds_it_on_another_device_until_it_starts(studio):  # noqa: F811
    owner = _client(studio, "owner")
    _uploaded(owner)
    assert _view(owner)["kept_start"] is None
    res = owner.post(f"/api/episodes/{EP}/kept-start", json=WIZARD)
    assert res.status_code == 200, res.text
    assert res.json()["kept_start"]["request"] == WIZARD
    # the same owner, signed in on another device (a new session, no browser storage): the same start
    elsewhere = _client(studio, "owner")
    view = _view(elsewhere)
    assert (view["can_act"], view["kept_start"]) == (True, WIZARD)
    assert _view(_client(studio, "admin"))["kept_start"] == WIZARD
    studio.temporal_client.start_workflow.assert_not_called()

    # the Start card sends it: the run gets the wizard's card, idea and length, and the kept start is used up
    res = elsewhere.post("/api/episodes", json=view["kept_start"])
    assert res.status_code == 200, res.text
    args = studio.temporal_client.start_workflow.call_args.kwargs["args"]
    assert args[0] == EP and args[2] is True and args[7] is True
    assert (args[8]["archetype_id"], args[8]["idea"], args[8]["target_duration_s"]) == (CARD, IDEA, 30)
    assert _view(owner)["kept_start"] is None and kept_starts.get(EP) is None


async def test_only_the_owner_or_an_admin_keeps_or_sees_a_start(studio):  # noqa: F811
    owner, other, admin, anon = (_client(studio, who) for who in ("owner", "other", "admin", None))
    _uploaded(owner)
    assert anon.post(f"/api/episodes/{EP}/kept-start", json=WIZARD).status_code == 401
    res = other.post(f"/api/episodes/{EP}/kept-start", json=WIZARD)
    assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}", res.text
    assert kept_starts.get(EP) is None
    assert owner.post(f"/api/episodes/{EP}/kept-start", json=WIZARD).status_code == 200
    # another member sees who made it, that they may not act, and no start
    theirs = _view(other)
    assert (theirs["can_act"], theirs["kept_start"]) == (False, None)
    assert IDEA not in json.dumps(theirs)
    # an admin may keep one for any episode (here a free test run without the wizard's card)
    res = admin.post(f"/api/episodes/{EP}/kept-start", json={"episode_id": EP})
    assert res.status_code == 200, res.text
    assert _view(owner)["kept_start"] == {"episode_id": EP, "dry_run": True, "enable_story_shaping": True}


async def test_a_start_that_cannot_be_kept_keeps_nothing(studio):  # noqa: F811
    owner = _client(studio, "owner")
    # not uploaded yet
    res = owner.post(f"/api/episodes/{EP}/kept-start", json=WIZARD)
    assert res.status_code == 404 and "has no beatmap yet" in res.json()["detail"], res.text
    _uploaded(owner)
    for body, status, detail in (
            ({**WIZARD, "episode_id": OTHER_EP}, 400, f"This start is for episode {OTHER_EP}, not {EP}"),
            ({**WIZARD, "archetype_id": "no-such-card"}, 400, "Unknown video type 'no-such-card'"),
            ({**WIZARD, "target_duration": 5}, 422, None),
            ({**WIZARD, "idea": "x" * 2001}, 422, None),
            ({**WIZARD, "episode_id": "../ep61cast"}, 422, None)):
        res = owner.post(f"/api/episodes/{EP}/kept-start", json=body)
        assert res.status_code == status, (body, res.text)
        if detail:
            assert res.json()["detail"] == detail
    assert kept_starts.get(EP) is None
    # never while a run is going
    _running(studio)
    res = owner.post(f"/api/episodes/{EP}/kept-start", json=WIZARD)
    assert res.status_code == 409 and "has a run in progress" in res.json()["detail"], res.text
    assert kept_starts.get(EP) is None


async def test_a_start_still_starts_when_nothing_was_kept(studio):  # noqa: F811
    owner = _client(studio, "owner")
    _uploaded(owner)
    res = owner.post("/api/episodes", json={"episode_id": EP, "dry_run": True})
    assert res.status_code == 200, res.text
    studio.temporal_client.start_workflow.assert_called_once()
    assert kept_starts.get(EP) is None
