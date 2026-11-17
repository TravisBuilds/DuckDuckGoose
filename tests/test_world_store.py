"""Universe store (hfvg/world_store.py): the universe migration on an existing studio DB, profiles
(a missing row reads as empty), saved voices, the creation references, mood board versions (redo
with feedback, approval), the bible, what episodes use, the characters still to lock, the list
summaries and the draft context."""

from __future__ import annotations

import json
import sqlite3

import pytest

from hfvg import cast_store, paths, prop_store, world_store
from hfvg.cast_store import CastConflict, CastError, CastNotFound
from tests.cast_helpers import attestation, complete_sheet, image_bytes, new_character, new_place, new_prop, upload

UNIVERSE_TABLES = {"series_profiles", "character_voices", "universe_notes", "universe_photos", "universe_boards",
                   "universe_board_images", "universe_board_requests", "universe_board_jobs"}


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.delenv("CAST_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("CAST_MIN_IMAGE_PX", raising=False)
    return tmp_path / "private-cast"


def _universe(name="Lantern Bay", owner="u-owner", **kwargs) -> str:
    return world_store.create_universe(name, owner, actor=owner, **kwargs)["series"]["series_id"]


def _lib(series_id: str) -> tuple[str, str]:
    return ("series", series_id)


def _locked(series_id: str, name: str = "Pip") -> dict:
    character = new_character(_lib(series_id), name)
    complete_sheet(character)
    cast_store.approve_version(character["character_id"], 1, "t")
    return cast_store.get_character(character["character_id"])


def _shot(sid: str, **extra) -> dict:
    return {"id": sid, "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": f"A quiet pier at dawn, shot {sid}.", "ref_keys": [], **extra}


def _write_shots(episode_id: str, shots) -> None:
    folder = paths.episode_dir(episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    text = shots if isinstance(shots, str) else json.dumps({"episode_id": episode_id, "shots": shots})
    (folder / "shots.json").write_text(text, encoding="utf-8")


def _audit(prefix: str) -> list[dict]:
    with cast_store.connect() as conn:
        rows = conn.execute("SELECT * FROM cast_audit_log WHERE action LIKE ? ORDER BY id", (f"{prefix}%",)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------- migration


async def test_migration_adds_the_universe_tables_and_changes_nothing_that_was_there(tmp_path, monkeypatch):
    """An existing studio DB with episodes, ledgers, users, a series and cast and props rows gains
    the universe tables. Every table that was there keeps its schema and rows; the second run
    does nothing."""
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import (
        UNIVERSE_BOARD_MIGRATION, UNIVERSE_MIGRATION, apply_universe_board_sync, apply_universe_sync,
        migrate_cast_library, migrate_props_library, run_all_migrations)
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    db = str(tmp_path / "existing.db")
    await init_studio_db(db)
    ledger = BudgetLedger(db)
    await ledger.init_db()
    await UserManager(db).init_db()
    await migrate_cast_library(db)
    await migrate_props_library(db)
    await create_episode(db, "ep07")
    await ledger.init_episode_budget("ep07")
    await ledger.record_job("ep07", "still-ep07-A01-v1", "A01", "seedream", "final", "L3_final_stills", 50_000)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO series (series_id, name, owner_user_id, created_at, updated_at) "
                     "VALUES ('sr_0123456789ab', 'Harbour Tales', 'u1', 'now', 'now')")
        conn.execute("INSERT INTO cast_characters (character_id, library_kind, library_id, slug, name, role, "
                     "description, created_by, created_at, updated_at) VALUES ('ch_0123456789ab', 'series', "
                     "'sr_0123456789ab', 'pip', 'Pip', '', '', 'u1', 'now', 'now')")
        conn.execute("INSERT INTO prop_items (prop_id, library_kind, library_id, kind, slug, name, description, "
                     "created_by, created_at, updated_at) VALUES ('pr_0123456789ab', 'series', 'sr_0123456789ab', "
                     "'place', 'pier', 'Pier', '', 'u1', 'now', 'now')")

    def snapshot():
        with sqlite3.connect(db) as conn:
            schema = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type IN "
                                                       "('table', 'index') AND name NOT LIKE 'sqlite_%'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "budget_lines", "job_ledger", "series", "cast_characters", "cast_versions",
                              "prop_items", "cast_audit_log")}
        return schema, data

    schema_before, data_before = snapshot()
    assert not UNIVERSE_TABLES & set(schema_before)
    await run_all_migrations(db)
    schema_once, data_once = snapshot()
    await run_all_migrations(db)
    schema_after, data_after = snapshot()
    assert data_once == data_after == data_before
    assert {k: v for k, v in schema_after.items() if k in schema_before} == schema_before
    assert {name for name in set(schema_after) - set(schema_before) if not name.startswith("idx_")} == UNIVERSE_TABLES
    assert schema_once == schema_after
    with sqlite3.connect(db) as conn:
        for version in (UNIVERSE_MIGRATION, UNIVERSE_BOARD_MIGRATION):
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                                (version,)).fetchone()[0] == 1
        assert apply_universe_sync(conn) is False and apply_universe_board_sync(conn) is False
    # the series that was there reads with the empty profile
    monkeypatch.setenv("DATABASE_PATH", db)
    profile = world_store.get_profile("sr_0123456789ab")
    assert (profile["logline"], profile["bible"], profile["narrator_voice"], profile["created_at"]) == ("", "", None,
                                                                                                         None)


def test_a_db_without_the_universe_tables_gets_them_on_first_use(tmp_path, monkeypatch):
    """The worker opens a DB that has the cast and props libraries but not the universe tables."""
    from hfvg.migrations import UNIVERSE_MIGRATION, apply_cast_library_sync, apply_props_library_sync

    db = tmp_path / "older.db"
    conn = sqlite3.connect(db, isolation_level=None)
    apply_cast_library_sync(conn)
    apply_props_library_sync(conn)
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    sid = _universe()
    assert world_store.get_profile(sid)["series_id"] == sid
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert UNIVERSE_TABLES <= tables
        assert conn.execute("SELECT version FROM schema_migrations WHERE version = ?",
                            (UNIVERSE_MIGRATION,)).fetchone()


def test_a_db_with_the_first_universe_tables_gets_the_board_request_tables_on_first_use(tmp_path, monkeypatch):
    """A DB that has the universe tables without the live board request tables (an earlier build)
    gains them on first use; its boards are kept."""
    from hfvg.migrations import (
        UNIVERSE_BOARD_MIGRATION, apply_cast_library_sync, apply_props_library_sync, apply_universe_sync)

    db = tmp_path / "first-universe.db"
    conn = sqlite3.connect(db, isolation_level=None)
    apply_cast_library_sync(conn)
    apply_props_library_sync(conn)
    apply_universe_sync(conn)
    conn.execute("INSERT INTO series (series_id, name, owner_user_id, created_at, updated_at) "
                 "VALUES ('sr_0123456789ab', 'Harbour Tales', 'u1', 'now', 'now')")
    conn.execute("INSERT INTO universe_boards (board_id, series_id, version, status, prompt, image_count, "
                 "estimate_usd_micros, approved_total_usd_micros, dry_run, created_at, updated_at) VALUES "
                 "('mb_0123456789ab', 'sr_0123456789ab', 1, 'ready', 'p', 4, 0, 0, 1, 'now', 'now')")
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    assert world_store.making_board_requests("sr_0123456789ab") == []
    assert world_store.open_board_request("sr_0123456789ab") is None
    assert [b["board_id"] for b in world_store.list_boards("sr_0123456789ab")] == ["mb_0123456789ab"]
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"universe_board_requests", "universe_board_jobs"} <= tables
        assert conn.execute("SELECT version FROM schema_migrations WHERE version = ?",
                            (UNIVERSE_BOARD_MIGRATION,)).fetchone()


# ------------------------------------------------------------------------------------- profile


def test_a_series_without_a_profile_row_reads_as_the_empty_profile():
    series = cast_store.create_series("Harbour Tales", "u1")
    profile = world_store.get_profile(series["series_id"])
    assert profile == {"series_id": series["series_id"], "logline": "", "bible": "", "style_notes": "",
                       "preset_id": None, "cover_image_id": None, "narrator_voice": None, "bible_board_id": None,
                       "bible_generated_at": None, "updated_by": None, "created_at": None, "updated_at": None}
    with pytest.raises(CastNotFound):
        world_store.get_profile("sr_000000000000")


def test_create_universe_records_owner_and_profile_and_refuses_bad_input_before_writing():
    out = world_store.create_universe("  Lantern   Bay ", "u-owner", logline="A harbour town  where lanterns talk",
                                      preset_id="creature-legend", actor="u-owner")
    assert (out["series"]["name"], out["series"]["owner_user_id"]) == ("Lantern Bay", "u-owner")
    assert (out["profile"]["logline"], out["profile"]["preset_id"]) == ("A harbour town where lanterns talk",
                                                                        "creature-legend")
    assert [a["action"] for a in _audit("universe_create")] == ["universe_create"]
    before = len(cast_store.list_series())
    with pytest.raises(CastError, match="Unknown video type"):
        world_store.create_universe("Second", "u-owner", preset_id="not-a-card")
    with pytest.raises(CastError, match="Logline"):
        world_store.create_universe("Third", "u-owner", logline="x" * (world_store.LOGLINE_MAX + 1))
    assert len(cast_store.list_series()) == before


def test_update_profile_keeps_bible_paragraphs_and_checks_limits():
    sid = _universe()
    profile = world_store.update_profile(sid, bible="First  paragraph.\r\n\r\n\r\n\r\nSecond one.   \n",
                                         style_notes="Warm dusk light.", actor="u-owner")
    assert profile["bible"] == "First  paragraph.\n\nSecond one."
    assert (profile["style_notes"], profile["logline"], profile["updated_by"]) == ("Warm dusk light.", "", "u-owner")
    with pytest.raises(CastError, match="Story bible"):
        world_store.update_profile(sid, bible="b" * (world_store.BIBLE_MAX + 1))
    with pytest.raises(CastError, match="must be text"):
        world_store.update_profile(sid, bible=["not", "text"])
    assert world_store.update_profile(sid, preset_id=None)["preset_id"] is None
    details = json.loads(_audit("universe_profile_update")[0]["details"])
    assert details["bible"] == len("First  paragraph.\n\nSecond one.")


def test_the_cover_must_be_a_picture_of_this_universe():
    sid, other = _universe(), _universe("Other")
    pip = new_character(_lib(sid))
    image = upload(pip)
    stranger = upload(new_character(_lib(other), "Moss"), seed=4)
    assert world_store.update_profile(sid, cover_image_id=image["image_id"])["cover_image_id"] == image["image_id"]
    with pytest.raises(CastNotFound):
        world_store.update_profile(sid, cover_image_id=stranger["image_id"])
    with pytest.raises(CastNotFound):
        world_store.update_profile(sid, cover_image_id="ci_000000000000")
    assert world_store.update_profile(sid, cover_image_id="")["cover_image_id"] is None


# ------------------------------------------------------------------------------------- voices


def test_a_character_voice_is_saved_replaced_and_cleared_without_any_provider():
    sid = _universe()
    pip = new_character(_lib(sid))
    saved = world_store.set_character_voice(pip["character_id"], voice_id="dryrun-voice-maple", voice_name="Maple",
                                            description="warm, bright", preview_url="data:audio/wav;base64,UklG",
                                            actor="u-owner")
    assert (saved["voice_id"], saved["name"], saved["description"], saved["chosen_by"]) == (
        "dryrun-voice-maple", "Maple", "warm, bright", "u-owner")
    world_store.set_character_voice(pip["character_id"], voice_id="dryrun-voice-cedar", voice_name="Cedar",
                                    preview_url="https://example.invalid/cedar.mp3")
    assert world_store.character_voices([pip["character_id"]])[pip["character_id"]]["name"] == "Cedar"
    assert world_store.set_character_voice(pip["character_id"], voice_id=None) is None
    assert world_store.character_voices([pip["character_id"]]) == {}
    assert [a["action"] for a in _audit("character_voice")] == ["character_voice_save", "character_voice_save",
                                                                  "character_voice_clear"]


@pytest.mark.parametrize("fields, message", [
    ({"voice_id": ""}, "Pick a voice"),
    ({"voice_id": "bad id with spaces"}, "Pick a voice"),
    ({"voice_id": "v1", "preview_url": "javascript:alert(1)"}, "preview"),
    ({"voice_id": "v1", "preview_url": "http://example.invalid/a.mp3"}, "preview"),
    ({"voice_id": "v1", "preview_url": "data:text/html;base64,AAAA"}, "preview"),
    ({"voice_id": "v1", "preview_url": "data:audio/wav;base64," + "A" * world_store.PREVIEW_URL_MAX}, "preview"),
    ({"voice_id": "v1", "voice_name": "n" * (world_store.VOICE_NAME_MAX + 1)}, "Voice name"),
])
def test_a_voice_that_is_not_a_pick_is_refused(fields, message):
    pip = new_character(_lib(_universe()))
    with pytest.raises(CastError, match=message):
        world_store.set_character_voice(pip["character_id"], **fields)
    assert world_store.character_voices([pip["character_id"]]) == {}


def test_the_narrator_voice_lives_on_the_profile_and_a_deleted_character_takes_its_voice_along():
    sid = _universe()
    narrator = world_store.set_narrator_voice(sid, voice_id="dryrun-voice-pebble", voice_name="Pebble",
                                              description="calm storyteller", actor="u-owner")
    assert narrator == {"voice_id": "dryrun-voice-pebble", "name": "Pebble", "description": "calm storyteller",
                        "preview_url": None}
    assert world_store.get_profile(sid)["narrator_voice"] == narrator
    assert world_store.set_narrator_voice(sid, voice_id=None) is None
    assert world_store.get_profile(sid)["narrator_voice"] is None
    pip = new_character(_lib(sid))
    world_store.set_character_voice(pip["character_id"], voice_id="dryrun-voice-maple", voice_name="Maple")
    cast_store.delete_character(pip["character_id"], "t")
    with cast_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM character_voices").fetchone()[0] == 0


# ------------------------------------------------------------------------------------- references


def test_notes_are_added_edited_and_deleted_by_topic():
    sid = _universe()
    note = world_store.add_note(sid, "Characters", "Pip: a small round robot\nMoss: a shy moth", actor="u-owner")
    assert (note["topic"], note["body"]) == ("characters", "Pip: a small round robot\nMoss: a shy moth")
    assert world_store.update_note(sid, note["note_id"], "Pip: a teal robot")["body"] == "Pip: a teal robot"
    with pytest.raises(CastError, match="premise, characters, places or tone"):
        world_store.add_note(sid, "plot", "x")
    with pytest.raises(CastError, match="Write the note"):
        world_store.add_note(sid, "tone", "   ")
    with pytest.raises(CastNotFound):
        world_store.update_note(_universe("Other"), note["note_id"], "taken over")
    assert world_store.delete_note(sid, note["note_id"]) == {"deleted": note["note_id"]}
    assert world_store.list_notes(sid) == []


def test_the_number_of_notes_is_limited(monkeypatch):
    monkeypatch.setattr(world_store, "MAX_NOTES", 2)
    sid = _universe()
    world_store.add_note(sid, "premise", "one")
    world_store.add_note(sid, "tone", "two")
    with pytest.raises(CastConflict, match="limit is 2"):
        world_store.add_note(sid, "places", "three")


def test_reference_photos_get_the_upload_checks_are_deduplicated_and_their_files_go_when_unused(cast_dir):
    sid = _universe()
    data = image_bytes(seed=11)
    photo = world_store.add_reference_photo(sid, data, filename="harbour.png", attestation=attestation(),
                                            caption="the harbour at dusk", actor="u-owner")
    assert (photo["caption"], photo["duplicate"], photo["attestation"]["rights"]) == ("the harbour at dusk", False,
                                                                                      "original")
    stored = cast_store.image_file(photo["sha256"], photo["ext"])
    assert stored.read_bytes() == data and stored.is_relative_to(cast_dir)
    again = world_store.add_reference_photo(sid, data, filename="again.png", attestation=attestation())
    assert (again["image_id"], again["duplicate"]) == (photo["image_id"], True)
    assert len(world_store.list_reference_photos(sid)) == 1
    with pytest.raises(cast_store.CastError):
        world_store.add_reference_photo(sid, image_bytes(size=(300, 300)), filename="small.png",
                                        attestation=attestation())
    world_store.update_profile(sid, cover_image_id=photo["image_id"])
    assert world_store.delete_reference_photo(sid, photo["image_id"])["file_removed"] is True
    assert not stored.exists()
    assert world_store.get_profile(sid)["cover_image_id"] is None


def test_a_reference_photo_file_stays_while_a_character_uses_the_same_picture(cast_dir):
    sid = _universe()
    pip = new_character(_lib(sid))
    image = upload(pip, seed=12)
    photo = world_store.add_reference_photo(sid, image_bytes(seed=12), filename="same.png", attestation=attestation())
    assert photo["sha256"] == image["sha256"]
    assert world_store.delete_reference_photo(sid, photo["image_id"])["file_removed"] is False
    assert cast_store.image_file(image["sha256"], image["ext"]).is_file()


def test_reference_photos_are_limited(monkeypatch):
    monkeypatch.setattr(world_store, "MAX_REFERENCE_PHOTOS", 1)
    sid = _universe()
    world_store.add_reference_photo(sid, image_bytes(seed=1), filename="a.png", attestation=attestation())
    with pytest.raises(CastConflict, match="limit is 1"):
        world_store.add_reference_photo(sid, image_bytes(seed=2), filename="b.png", attestation=attestation())


# ------------------------------------------------------------------------------------- mood boards


def _board(sid: str, feedback: str = "") -> dict:
    plan = world_store.plan_board(sid, feedback)
    images = world_store.placeholder_board_images(f"{sid}:{plan['version']}", plan["photo_files"])
    return world_store.create_board(sid, plan, estimate_usd_micros=4_000, approved_total_usd_micros=4_000,
                                    dry_run=True, images=images, placeholder=True, requested_by="u-owner")


def test_a_board_needs_a_reference_and_a_decision_before_the_next_one():
    sid = _universe()
    with pytest.raises(CastConflict, match="Add a reference first"):
        world_store.plan_board(sid)
    world_store.add_note(sid, "premise", "Lanterns that talk")
    with pytest.raises(CastConflict, match="no mood board to redo"):
        world_store.plan_board(sid, "brighter")
    plan = world_store.plan_board(sid)
    assert (plan["version"], plan["image_count"], plan["answers_feedback"], plan["replaces_board_id"]) == (
        1, world_store.BOARD_IMAGES, "", None)
    assert "Premise: Lanterns that talk" in plan["prompt"]
    first = _board(sid)
    assert (first["version"], first["status"], len(first["images"]), first["dry_run"]) == (1, "ready", 4, True)
    assert all(i["placeholder"] for i in first["images"])
    with pytest.raises(CastConflict, match="Approve this mood board, or say what to change"):
        world_store.plan_board(sid)


def test_a_redo_answers_the_feedback_and_only_the_latest_board_can_be_approved():
    sid = _universe()
    world_store.add_note(sid, "tone", "Cosy and a little spooky")
    first = _board(sid)
    second = _board(sid, "  more  fog, fewer lanterns ")
    boards = world_store.list_boards(sid, with_prompt=True)
    assert [(b["version"], b["status"]) for b in boards] == [(1, "redo_requested"), (2, "ready")]
    assert boards[0]["redo_feedback"] == "more fog, fewer lanterns"
    assert boards[1]["answers_feedback"] == "more fog, fewer lanterns"
    assert "Change from the last board: more fog, fewer lanterns" in boards[1]["prompt"]
    assert "prompt" not in world_store.list_boards(sid)[0]
    with pytest.raises(CastConflict, match="Only the latest"):
        world_store.approve_board(sid, first["board_id"])
    with pytest.raises(CastNotFound):
        world_store.approve_board(_universe("Other"), second["board_id"])
    approved = world_store.approve_board(sid, second["board_id"], "u-owner")
    assert (approved["status"], approved["decided_by"]) == ("approved", "u-owner")
    with pytest.raises(CastConflict, match="approved already"):
        world_store.approve_board(sid, second["board_id"])
    with pytest.raises(CastConflict, match="next step is the Universe bible"):
        world_store.plan_board(sid, "one more")
    assert [a["action"] for a in _audit("universe_board")] == ["universe_board_create", "universe_board_create",
                                                                 "universe_board_approve"]


def test_a_board_made_meanwhile_refuses_the_older_plan():
    sid = _universe()
    world_store.add_note(sid, "premise", "Lanterns that talk")
    plan = world_store.plan_board(sid)
    _board(sid)
    with pytest.raises(CastConflict, match="changed meanwhile"):
        world_store.create_board(sid, plan, estimate_usd_micros=1, approved_total_usd_micros=1, dry_run=True,
                                 images=world_store.placeholder_board_images("x", []), placeholder=True,
                                 requested_by="u-owner")
    assert len(world_store.list_boards(sid)) == 1


def _live_request(sid: str, request_id: str, feedback: str = "") -> list[dict]:
    plan = world_store.plan_board(sid, feedback)
    jobs = [{"job_id": f"uboard-{request_id[3:]}-{k}", "position": k, "refs": [], "estimated_usd_micros": 100_000,
             "approved_usd_micros": 120_000} for k in range(world_store.BOARD_IMAGES)]
    world_store.start_board_request(sid, plan, jobs, request_id=request_id, workflow_id=f"universe-board-{request_id}",
                                    estimate_usd_micros=400_000, approved_total_usd_micros=480_000, model_path="m/still",
                                    requested_by="u-owner")
    return jobs


def test_a_live_board_keeps_every_picture_that_was_made():
    sid = _universe()
    world_store.add_note(sid, "premise", "Lanterns that talk")
    # a request that made no picture fails with its reason
    _live_request(sid, "mr_0123456789a0")
    failed = world_store.finish_board_request("mr_0123456789a0", "The provider could not make a picture")
    assert (failed["status"], failed["error"], failed["made"], failed["board_id"]) == (
        "failed", "The provider could not make a picture", 0, None)
    assert [j["status"] for j in failed["jobs"]] == ["skipped"] * 4
    assert world_store.list_boards(sid) == [] and world_store.open_board_request(sid) == failed
    assert world_store.finish_board_request("mr_0123456789a0", "later") == failed  # an ended request stays as it is
    # one being made: no plan meanwhile; its prompt is not in the view; a job keeps its first picture
    jobs = _live_request(sid, "mr_0123456789aa")
    request = world_store.open_board_request(sid)
    assert (request["status"], request["made"], [j["status"] for j in request["jobs"]]) == (
        "making", 0, ["queued"] * 4)
    assert "prompt" not in request
    with pytest.raises(CastConflict, match=world_store.BOARD_BEING_MADE):
        world_store.plan_board(sid)
    first = world_store.store_board_job_picture(jobs[0]["job_id"], image_bytes(seed=1), placeholder=False)
    again = world_store.store_board_job_picture(jobs[0]["job_id"], image_bytes(seed=2), placeholder=False)
    assert again["sha256"] == first["sha256"] and again["status"] == "completed"
    for job in jobs[1:3]:
        world_store.store_board_job_picture(job["job_id"], image_bytes(seed=10 + job["position"]), placeholder=False)
    # it stopped after 3 of 4 pictures: the three that were paid for are the board, with the reason
    short = world_store.finish_board_request("mr_0123456789aa")
    assert (short["status"], short["error"], short["made"]) == ("done", world_store.BOARD_STOPPED, 3)
    board = world_store.list_boards(sid)[0]
    assert short["board_id"] == board["board_id"]
    assert [j["status"] for j in short["jobs"]] == ["completed"] * 3 + ["skipped"]
    assert (board["version"], board["status"], board["dry_run"], board["image_count"]) == (1, "ready", False, 3)
    assert [(i["position"], i["source"]) for i in board["images"]] == [(0, "generated"), (1, "generated"),
                                                                       (2, "generated")]
    assert world_store.open_board_request(sid) == short  # the page says why the board has fewer pictures
    with cast_store.connect() as conn:
        assert cast_store.file_in_use(conn, first["sha256"])
    # a redo that made every picture: the next version, and nothing to report
    jobs = _live_request(sid, "mr_0123456789ab", "fewer lanterns")
    for job in jobs:
        world_store.store_board_job_picture(job["job_id"], image_bytes(seed=20 + job["position"]), placeholder=False)
    done = world_store.finish_board_request("mr_0123456789ab")
    boards = world_store.list_boards(sid)
    assert (done["status"], done["error"], done["board_id"]) == ("done", None, boards[1]["board_id"])
    assert [(b["version"], b["status"], b["image_count"]) for b in boards] == [(1, "redo_requested", 3),
                                                                             (2, "ready", 4)]
    assert world_store.open_board_request(sid) is None
    assert [a["action"] for a in _audit("universe_board")] == [
        "universe_board_request", "universe_board_failed", "universe_board_request", "universe_board_create",
        "universe_board_request", "universe_board_create"]


def test_a_live_redo_answers_its_feedback_and_a_board_made_meanwhile_refuses_the_request():
    sid = _universe()
    world_store.add_note(sid, "premise", "Lanterns that talk")
    first = _board(sid)
    plan = world_store.plan_board(sid, "more fog")
    _board(sid, "warmer")  # another board was made meanwhile
    with pytest.raises(CastConflict, match="changed meanwhile"):
        world_store.start_board_request(sid, plan, [], request_id="mr_0123456789ac", workflow_id="w",
                                        estimate_usd_micros=1, approved_total_usd_micros=1, model_path="m",
                                        requested_by="u-owner")
    jobs = _live_request(sid, "mr_0123456789ad", "fewer lanterns")
    latest = world_store.list_boards(sid)[-1]
    assert world_store.open_board_request(sid)["replaces_board_id"] == latest["board_id"] != first["board_id"]
    for job in jobs:
        world_store.store_board_job_picture(job["job_id"], image_bytes(seed=30 + job["position"]), placeholder=True)
    world_store.finish_board_request("mr_0123456789ad")
    boards = world_store.list_boards(sid, with_prompt=True)
    assert [(b["version"], b["status"]) for b in boards] == [(1, "redo_requested"), (2, "redo_requested"),
                                                              (3, "ready")]
    assert (boards[1]["redo_feedback"], boards[2]["answers_feedback"]) == ("fewer lanterns", "fewer lanterns")
    assert "Change from the last board: fewer lanterns" in boards[2]["prompt"] and boards[2]["dry_run"] is True


def test_placeholder_boards_are_deterministic_and_use_the_reference_photos(tmp_path):
    photo = tmp_path / "ref.png"
    photo.write_bytes(image_bytes(seed=5, color=(200, 40, 40)))
    plain = world_store.placeholder_board_images("seed", [])
    assert plain == world_store.placeholder_board_images("seed", [])
    assert len(plain) == world_store.BOARD_IMAGES and len(set(plain)) == world_store.BOARD_IMAGES
    framed = world_store.placeholder_board_images("seed", [str(photo)])
    assert framed != plain
    info = cast_store.inspect_image(framed[0])
    assert (info["width"], info["height"]) == (world_store.BOARD_PX, world_store.BOARD_PX)


# ------------------------------------------------------------------------------------- bible


def _approved(sid: str) -> dict:
    board = _board(sid)
    return world_store.approve_board(sid, board["board_id"], "u-owner")


def test_the_bible_waits_for_an_approved_board_and_is_written_once_per_board():
    sid = _universe(logline="Lanterns that talk")
    world_store.add_note(sid, "characters", "Pip: a small teal robot with one yellow antenna\n- Moss - a shy moth")
    world_store.add_note(sid, "places", "Lantern Pier: a crooked wooden pier hung with paper lanterns")
    world_store.add_note(sid, "tone", "Cosy, a little spooky")
    with pytest.raises(CastConflict, match="Approve a mood board first"):
        world_store.bible_inputs(sid)
    board = _approved(sid)
    inputs = world_store.bible_inputs(sid)
    assert (inputs["board_id"], inputs["logline"]) == (board["board_id"], "Lanterns that talk")
    bible = world_store.parse_bible(world_store.fake_bible(inputs))
    assert [c["name"] for c in bible["characters"]] == ["Pip", "Moss"]
    assert [p["name"] for p in bible["places"]] == ["Lantern Pier"]
    out = world_store.apply_bible(sid, bible, board_id=board["board_id"], actor="u-owner")
    assert out["created"] == {"characters": ["Pip", "Moss"], "places": ["Lantern Pier"], "props": []}
    assert out["filled"] == ["bible", "style_notes"]
    names = [c["name"] for c in cast_store.list_characters(_lib(sid))]
    assert names == ["Pip", "Moss"]
    assert [i["name"] for i in prop_store.list_items(_lib(sid))] == ["Lantern Pier"]
    profile = world_store.get_profile(sid)
    assert profile["bible_board_id"] == board["board_id"] and profile["bible"].startswith("Lanterns that talk")
    with pytest.raises(CastConflict, match="made already"):
        world_store.bible_inputs(sid)
    with pytest.raises(CastConflict, match="made already"):
        world_store.apply_bible(sid, bible, board_id=board["board_id"])


def test_the_bible_keeps_what_the_universe_has_and_what_the_creator_wrote():
    sid = _universe()
    new_character(_lib(sid), "Pip", description="drawn by hand")
    world_store.update_profile(sid, bible="My own bible.")
    world_store.add_note(sid, "premise", "Lanterns")
    board = _approved(sid)
    raw = {"bible": "Model bible", "style_notes": "Model style",
           "characters": [{"name": "pip", "description": "replaced?"}, {"name": "Wren", "role": "guide",
                                                                         "description": "a tall heron"},
                          {"name": "WREN", "description": "duplicate"}, {"name": "", "description": "nameless"},
                          "not an object"],
           "places": [{"name": "Night Market", "description": "x" * 1000}],
           "props": [{"name": "Brass Key", "description": "a small brass key"}]}
    bible = world_store.parse_bible(raw)
    assert [c["name"] for c in bible["characters"]] == ["pip", "Wren"]
    assert len(bible["places"][0]["description"]) == prop_store.DESCRIPTION_MAX
    out = world_store.apply_bible(sid, bible, board_id=board["board_id"])
    assert out["created"] == {"characters": ["Wren"], "places": ["Night Market"], "props": ["Brass Key"]}
    assert out["skipped"] == ["pip"] and out["filled"] == ["style_notes"]
    pip = next(c for c in cast_store.list_characters(_lib(sid)) if c["slug"] == "pip")
    assert pip["description"] == "drawn by hand"
    assert world_store.get_profile(sid)["bible"] == "My own bible."


def test_parse_bible_tolerates_anything_the_model_returns():
    assert world_store.parse_bible(None) == {"bible": "", "style_notes": "", "characters": [], "places": [],
                                             "props": []}
    assert world_store.parse_bible({"characters": "Pip", "bible": 12})["characters"] == []
    many = {"characters": [{"name": f"Name {i}"} for i in range(30)]}
    assert len(world_store.parse_bible(many)["characters"]) == world_store.BIBLE_CHARACTERS_MAX


def test_bible_messages_carry_the_references_and_the_existing_names():
    sid = _universe(logline="Lanterns that talk")
    new_place(_lib(sid), "Old Pier")
    world_store.add_note(sid, "characters", "Pip: a teal robot")
    world_store.add_reference_photo(sid, image_bytes(seed=3), filename="a.png", attestation=attestation(),
                                    caption="fog over the water")
    _approved(sid)
    system, user = world_store.bible_messages(world_store.bible_inputs(sid))
    assert system["role"] == "system" and "No named IP" in system["content"]
    assert "Characters (from the creator): Pip: a teal robot" in user["content"]
    assert "Reference photo captions: fog over the water" in user["content"]
    assert "Already in the Universe (keep them, do not list them again): Old Pier" in user["content"]
    assert "Approved mood board" in user["content"]


# ------------------------------------------------------------------------------------- what episodes use


def test_used_in_counts_each_episode_once_per_item_and_tolerates_bad_files():
    _write_shots("ep01", [
        _shot("A01", cast=["pip", {"character": "moss", "version": 2}], place="pier", props=["lantern"]),
        _shot("A02", cast=[{"character": "pip", "version": 1}], place="pier", props=["lantern", "key"]),
    ])
    _write_shots("ep02", [_shot("B01", cast=["pip"], props=["lantern"],
                                shot_meta={"cast": [{"character": "pip", "version": 2}]})])
    _write_shots("ep03", "{not json")
    _write_shots("ep04", json.dumps({"shots": "nope"}))
    usage = world_store.used_in(["ep01", "ep02", "ep03", "ep04", "ep05-missing"])
    assert usage["characters"] == {"pip": {"episodes": 2, "versions": {1: 1, 2: 1}},
                                   "moss": {"episodes": 1, "versions": {2: 1}}}
    assert usage["items"] == {"pier": 1, "lantern": 2, "key": 1}
    assert world_store.episode_usage("ep03") == {"characters": {}, "places": [], "props": []}


def test_unlocked_cast_lists_the_universe_characters_still_to_lock_in_order():
    sid = _universe()
    _locked(sid, "Pip")
    new_character(_lib(sid), "Moss")
    new_character(_lib(sid), "Wren")
    cast_store.link_episode("ep11", sid, "t")
    _write_shots("ep11", [_shot("A01", cast=["pip", "wren"]), _shot("A02", cast=["moss", "wren"]),
                          _shot("A03", cast=["ghost"])])
    assert [(c["character"], c["name"], c["version"]) for c in world_store.unlocked_cast("ep11")] == [
        ("wren", "Wren", 1), ("moss", "Moss", 1)]
    _write_shots("ep11", [_shot("A01", cast=["pip"])])
    assert world_store.unlocked_cast("ep11") == []
    # outside a Universe nothing is required; nothing to read is nothing to block
    new_character(("episode", "ep12"), "Solo")
    _write_shots("ep12", [_shot("A01", cast=["solo"])])
    assert world_store.unlocked_cast("ep12") == []
    cast_store.link_episode("ep13", sid, "t")
    assert world_store.unlocked_cast("ep13") == []


# ------------------------------------------------------------------------------------- list summaries


def test_summaries_count_and_pick_the_cover_in_order(cast_dir):
    sid = _universe()
    assert world_store.summaries([sid])[sid]["cover"] is None
    world_store.add_note(sid, "premise", "Lanterns")
    board = _approved(sid)
    assert world_store.summaries([sid])[sid]["cover"] == {"image_id": board["images"][0]["image_id"],
                                                          "kind": "board"}
    pier = new_place(_lib(sid), "Pier")
    from tests.cast_helpers import prop_photo
    photo = prop_photo(pier)
    assert world_store.summaries([sid])[sid]["cover"] == {"image_id": photo["image_id"], "kind": "prop"}
    new_character(_lib(sid), "Moss")
    pip = _locked(sid, "Pip")
    master = next(i for i in pip["versions"][0]["images"] if i["role"] == cast_store.MASTER_ROLE)
    new_prop(_lib(sid), "Lantern")
    summary = world_store.summaries([sid])[sid]
    assert summary["cover"] == {"image_id": master["image_id"], "kind": "cast"}
    assert summary["counts"] == {"characters": 2, "locked_characters": 1, "locked_versions": 1, "places": 1,
                                 "props": 1}
    assert summary["board_status"] == "approved"
    world_store.update_profile(sid, cover_image_id=photo["image_id"])
    assert world_store.summaries([sid])[sid]["cover"] == {"image_id": photo["image_id"], "kind": "prop"}
    assert world_store.summaries([]) == {}


# ------------------------------------------------------------------------------------- draft context


def test_draft_context_names_the_universe_and_keeps_private_text_for_its_editors():
    sid = _universe(logline="Lanterns that talk")
    world_store.update_profile(sid, bible="Secret story engine.", style_notes="Ink and watercolour.")
    new_character(_lib(sid), "Pip", role="lead", description="a small teal robot")
    new_character(_lib(sid), "Moss", description="a shy moth")
    new_place(_lib(sid), "Pier", description="a crooked pier")
    new_prop(_lib(sid), "Lantern", description="a paper lantern")
    private = world_store.draft_context(sid, vo_mode="dialogue", include_private=True)
    assert "Story bible: Secret story engine." in private["context"]
    assert "Style notes: Ink and watercolour." in private["context"]
    assert "- Pip (lead): a small teal robot" in private["context"]
    assert "- Pier: a crooked pier" in private["context"] and "- Lantern: a paper lantern" in private["context"]
    assert private["speakers"] == ["Pip", "Moss"] and private["names"] == ["Pip", "Moss"]
    assert 'speaker" set to one of these Universe character names: Pip, Moss' in private["context"]
    public = world_store.draft_context(sid, vo_mode="narration", include_private=False)
    assert "Secret story engine" not in public["context"] and "Ink and watercolour" not in public["context"]
    assert "Logline: Lanterns that talk" in public["context"] and public["speakers"] == []
