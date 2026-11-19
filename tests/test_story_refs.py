"""Story first (hfvg/story_refs.py, docs/WORLDS.md): the universe story migration on a DB from an
earlier build, the story, the reference checklist read from it (the deterministic test reader and
the model's list cleaned), photos uploaded onto its items, progress, making the list again, adding
and removing items, and the story and list going into the mood board and the bible. A Universe
without a story keeps working as before. Every picture is generated (flat shapes); every name is
original."""

from __future__ import annotations

import sqlite3

import pytest

from hfvg import cast_store, prop_store, story_refs, world_store
from hfvg.cast_store import CastConflict, CastError, CastNotFound
from tests.cast_helpers import attestation, image_bytes

STORY = ("Mira mends nets in the harbour town. One night she finds a brass lantern in the old lighthouse. "
         "She carries the lantern to the night market, where Tobin sells kites with a paper moon.")


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.delenv("CAST_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("CAST_MIN_IMAGE_PX", raising=False)
    return tmp_path / "private-cast"


def _universe(name="Lantern Bay", owner="u-owner") -> str:
    return world_store.create_universe(name, owner, actor=owner)["series"]["series_id"]


def _photo(sid: str, seed: int = 0, item_id: str | None = None, size=(640, 960)) -> dict:
    return world_store.add_reference_photo(sid, image_bytes(size=size, seed=seed), filename=f"ref{seed}.png",
                                           attestation=attestation(), actor="u-owner", item_id=item_id)


def _make_list(sid: str) -> dict:
    inputs = story_refs.checklist_inputs(sid)
    items = story_refs.parse_checklist(story_refs.fake_checklist(inputs))
    return story_refs.apply_checklist(sid, items, story=inputs["story_sha"], actor="u-owner")


def _by_name(sid: str) -> dict[str, dict]:
    return {i["name"]: i for i in story_refs.checklist(sid)["items"]}


def _audit(prefix: str) -> list[dict]:
    with cast_store.connect() as conn:
        rows = conn.execute("SELECT * FROM cast_audit_log WHERE action LIKE ? ORDER BY id", (f"{prefix}%",)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------- migration


def test_a_db_from_before_stories_gets_the_story_tables_and_its_universe_keeps_working(tmp_path, monkeypatch):
    """A DB with the universe and board tables (an earlier build) gains the story tables on first
    use. Its notes, photos and boards stay; its Universe reads as made before stories (no story, an
    empty list) and its mood board is still made from the notes alone."""
    from hfvg.migrations import (
        UNIVERSE_STORY_MIGRATION,
        apply_cast_library_sync,
        apply_props_library_sync,
        apply_universe_board_sync,
        apply_universe_sync,
    )

    db = tmp_path / "before-stories.db"
    conn = sqlite3.connect(db, isolation_level=None)
    for apply in (apply_cast_library_sync, apply_props_library_sync, apply_universe_sync, apply_universe_board_sync):
        apply(conn)
    conn.execute("INSERT INTO series (series_id, name, owner_user_id, created_at, updated_at) "
                 "VALUES ('sr_0123456789ab', 'Harbour Tales', 'u1', 'now', 'now')")
    conn.execute("INSERT INTO universe_notes (note_id, series_id, topic, body, created_by, created_at, updated_at) "
                 "VALUES ('un_0123456789ab', 'sr_0123456789ab', 'premise', 'Lanterns that talk', 'u1', 'now', 'now')")
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    sid = "sr_0123456789ab"
    story = story_refs.get_story(sid)
    assert (story["body"], story["written"], story["list_made"], story["list_current"]) == ("", False, False, False)
    assert story_refs.checklist(sid) == {"items": [], "progress": {"ready": 0, "required": 0, "optional": 0,
                                                                   "optional_ready": 0, "photos": 0}}
    assert [n["body"] for n in world_store.list_notes(sid)] == ["Lanterns that talk"]
    plan = world_store.plan_board(sid)
    assert "Premise: Lanterns that talk" in plan["prompt"] and "Story:" not in plan["prompt"]
    assert plan["sources"] == {"notes": ["un_0123456789ab"], "photos": []}
    assert world_store.summaries([sid])[sid]["story_written"] is False
    with sqlite3.connect(db) as check:
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"universe_stories", "universe_checklist_items", "universe_checklist_photos"} <= tables
        assert check.execute("SELECT version FROM schema_migrations WHERE version = ?",
                             (UNIVERSE_STORY_MIGRATION,)).fetchone()


# ------------------------------------------------------------------------------------- the story


def test_the_story_keeps_its_paragraphs_and_the_list_knows_when_the_story_changed():
    sid = _universe()
    saved = story_refs.save_story(sid, "  Mira finds a lantern.  \r\n\r\n\r\n\r\nTobin wants it.  ", actor="u-owner")
    assert (saved["body"], saved["written"], saved["list_made"]) == ("Mira finds a lantern.\n\nTobin wants it.", True,
                                                                      False)
    with pytest.raises(CastError, match="keep it under"):
        story_refs.save_story(sid, "x" * (story_refs.STORY_MAX + 1))
    with pytest.raises(CastNotFound):
        story_refs.save_story("sr_000000000000", "A story")
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    assert story_refs.get_story(sid)["list_current"] is True
    story_refs.save_story(sid, STORY + " The tide comes in.")
    after = story_refs.get_story(sid)
    assert (after["list_made"], after["list_current"]) == (True, False)
    assert world_store.summaries([sid])[sid]["story_written"] is True
    assert [a["action"] for a in _audit("universe_story")] == ["universe_story_save"] * 3


def test_the_list_waits_for_a_few_lines_of_story():
    sid = _universe()
    with pytest.raises(CastConflict, match="Write a few lines of the story first"):
        story_refs.checklist_inputs(sid)
    story_refs.save_story(sid, "Mira")
    with pytest.raises(CastConflict, match="Write a few lines of the story first"):
        story_refs.checklist_inputs(sid)


# ------------------------------------------------------------------------------------- reading the story


def test_the_test_reader_lists_each_character_place_and_prop_of_the_story():
    listed = story_refs.fake_checklist({"story": STORY})["items"]
    assert [(i["kind"], i["name"], i["optional"]) for i in listed] == [
        ("character", "Mira", False), ("character", "Tobin", False), ("place", "Harbour town", False),
        ("place", "Old lighthouse", False), ("place", "Night market", False), ("prop", "Brass lantern", False),
        ("prop", "Paper moon", True)]
    assert story_refs.fake_checklist({"story": STORY}) == story_refs.fake_checklist({"story": STORY})


def test_the_model_request_carries_the_story_the_names_and_the_content_policy():
    sid = _universe()
    world_store.update_profile(sid, logline="A harbour of talking lanterns", actor="u-owner")
    cast_store.create_character(("series", sid), "Mira", actor="u-owner", role="lead",
                                description="a net mender with a green coat")
    story_refs.save_story(sid, STORY)
    inputs = story_refs.checklist_inputs(sid)
    assert inputs["story_sha"] == story_refs.story_sha(STORY) and inputs["existing"]["characters"] == ["Mira"]
    system, user = story_refs.checklist_messages(inputs)
    assert "CONTENT POLICY" in system["content"] and '"items"' in system["content"]
    assert STORY in user["content"] and "Logline: A harbour of talking lanterns" in user["content"]
    assert "Already in the Universe" in user["content"] and "Mira" in user["content"]


def test_the_models_list_is_cleaned_before_it_is_kept():
    raw = {"items": [
        {"kind": "Location", "name": "  Night   market ", "why": "Where the kites are sold", "ideal_photos": 9,
         "min_photos": 5},
        {"kind": "character", "name": "Mira", "ideal_photos": 2, "min_photos": -1, "optional": "yes"},
        {"kind": "character", "name": "MIRA", "why": "duplicate"},
        {"kind": "vehicle", "name": "Tram"},
        {"kind": "prop", "name": ""},
        "not an item",
        {"kind": "prop", "name": "Kite", "ideal_photos": True, "show": "x" * 400, "optional": True},
    ]}
    cleaned = story_refs.parse_checklist(raw)
    assert [(i["kind"], i["name"], i["ideal_photos"], i["min_photos"], i["optional"]) for i in cleaned] == [
        ("place", "Night market", 6, 5, False), ("character", "Mira", 2, 0, False), ("prop", "Kite", 2, 1, True)]
    assert cleaned[1]["show"] == story_refs.DEFAULTS["character"]["show"]
    assert len(cleaned[2]["show"]) == story_refs.SHOW_MAX and cleaned[2]["show"].endswith("…")
    many = {"items": [{"kind": "prop", "name": f"Thing {n}"} for n in range(40)]}
    assert len(story_refs.parse_checklist(many)) == story_refs.MAX_ITEMS
    assert story_refs.parse_checklist(None) == story_refs.parse_checklist({"items": "no"}) == []


# ------------------------------------------------------------------------------------- the checklist


def test_photos_upload_onto_items_and_the_list_counts_what_is_ready():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    made = _make_list(sid)
    assert made["added"] == ["Mira", "Tobin", "Harbour town", "Old lighthouse", "Night market", "Brass lantern",
                             "Paper moon"]
    assert made["progress"] == {"ready": 0, "required": 6, "optional": 1, "optional_ready": 0, "photos": 0}
    items = _by_name(sid)
    mira = items["Mira"]
    assert (mira["kind"], mira["ideal_photos"], mira["min_photos"], mira["source"], mira["ready"]) == (
        "character", 3, 1, "story", False)
    assert mira["show"] == story_refs.DEFAULTS["character"]["show"] and "Mira is in the story" in mira["why"]
    first = _photo(sid, 1, mira["item_id"])
    assert first["item_id"] == mira["item_id"] and first["duplicate"] is False
    small = _photo(sid, 2, items["Paper moon"]["item_id"], size=(300, 200))
    assert (small["width"], small["height"]) == (300, 200)
    _photo(sid, 3, items["Night market"]["item_id"])
    _photo(sid, 4, items["Brass lantern"]["item_id"])
    _photo(sid, 5, items["Tobin"]["item_id"])
    progress = story_refs.checklist(sid)["progress"]
    assert progress == {"ready": 4, "required": 6, "optional": 1, "optional_ready": 1, "photos": 5}
    assert _by_name(sid)["Mira"]["photos"] == [first["image_id"]]
    # the same picture again onto another item is one reference photo on both
    again = _photo(sid, 1, items["Harbour town"]["item_id"])
    assert (again["image_id"], again["duplicate"]) == (first["image_id"], True)
    assert _by_name(sid)["Harbour town"]["ready"] is True
    assert len(world_store.list_reference_photos(sid)) == 5
    # every reference photo is still a Universe reference: the mood board is made from them all
    assert len(world_store.plan_board(sid)["photo_refs"]) == 5


def test_an_item_takes_a_limited_number_of_photos_and_only_its_own_universe():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    mira = _by_name(sid)["Mira"]["item_id"]
    for seed in range(story_refs.MAX_PHOTOS_PER_ITEM):
        _photo(sid, 10 + seed, mira)
    with pytest.raises(CastConflict, match="the limit is 6"):
        _photo(sid, 99, mira)
    other = _universe("Other Bay")
    with pytest.raises(CastNotFound, match="not on the reference list"):
        _photo(other, 98, mira)
    assert world_store.list_reference_photos(other) == []
    assert len(world_store.list_reference_photos(sid)) == story_refs.MAX_PHOTOS_PER_ITEM


def test_deleting_a_photo_takes_it_off_its_item():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    mira = _by_name(sid)["Mira"]
    photo = _photo(sid, 1, mira["item_id"])
    assert _by_name(sid)["Mira"]["ready"] is True
    world_store.delete_reference_photo(sid, photo["image_id"], "u-owner")
    assert (_by_name(sid)["Mira"]["ready"], _by_name(sid)["Mira"]["photos"]) == (False, [])
    with cast_store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM universe_checklist_photos").fetchone()[0] == 0


def test_the_owner_adds_and_removes_items_and_removed_items_keep_their_photos():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    cloak = story_refs.add_item(sid, "prop", "  Rain   cloak ", why="Mira wears it in every scene", ideal_photos=2,
                                min_photos=1, actor="u-owner")
    assert (cloak["name"], cloak["source"], cloak["show"], cloak["optional"]) == (
        "Rain cloak", "owner", story_refs.DEFAULTS["prop"]["show"], False)
    with pytest.raises(CastConflict, match="on the list already"):
        story_refs.add_item(sid, "prop", "RAIN CLOAK")
    with pytest.raises(CastError, match="a character, a place or a prop"):
        story_refs.add_item(sid, "vehicle", "Tram")
    with pytest.raises(CastError, match="Name is required"):
        story_refs.add_item(sid, "prop", "  ")
    tobin = _by_name(sid)["Tobin"]
    photo = _photo(sid, 3, tobin["item_id"])
    assert story_refs.remove_item(sid, tobin["item_id"], "u-owner") == {"removed": tobin["item_id"]}
    assert "Tobin" not in _by_name(sid)
    assert [p["image_id"] for p in world_store.list_reference_photos(sid)] == [photo["image_id"]]
    with pytest.raises(CastNotFound):
        story_refs.remove_item(sid, tobin["item_id"])
    with pytest.raises(CastNotFound):
        _photo(sid, 4, tobin["item_id"])
    # the list has a limit
    for n in range(story_refs.MAX_ITEMS - len(_by_name(sid))):
        story_refs.add_item(sid, "prop", f"Shell {n}")
    with pytest.raises(CastConflict, match="the limit is 16"):
        story_refs.add_item(sid, "prop", "One more shell")
    actions = [a["action"] for a in _audit("universe_checklist")]
    assert actions[0] == "universe_checklist_make" and "universe_checklist_remove" in actions


def test_making_the_list_again_keeps_photos_and_owner_items_and_drops_what_the_story_lost():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    story_refs.add_item(sid, "prop", "Rain cloak")
    _photo(sid, 1, _by_name(sid)["Night market"]["item_id"])
    story_refs.save_story(sid, "Mira mends nets in the harbour town. She finds a brass lantern in the old lighthouse "
                               "and keeps it.")
    again = _make_list(sid)
    assert (again["added"], again["removed"]) == ([], ["Tobin", "Paper moon"])
    assert [i["name"] for i in again["items"]] == ["Mira", "Harbour town", "Old lighthouse", "Night market",
                                                   "Brass lantern", "Rain cloak"]
    # a story item on the new list takes its new why and photo counts
    inputs = story_refs.checklist_inputs(sid)
    redo = story_refs.apply_checklist(sid, story_refs.parse_checklist({"items": [
        {"kind": "character", "name": "mira", "why": "She is in every scene", "ideal_photos": 4, "min_photos": 2},
        {"kind": "place", "name": "Sea cave", "why": "The last scene"}]}), story=inputs["story_sha"])
    assert redo["added"] == ["Sea cave"]
    assert set(redo["removed"]) == {"Harbour town", "Old lighthouse", "Brass lantern"}
    mira = _by_name(sid)["Mira"]
    assert (mira["why"], mira["ideal_photos"], mira["min_photos"]) == ("She is in every scene", 4, 2)
    # a list read from a story that changed meanwhile is not kept
    story_refs.save_story(sid, STORY)
    with pytest.raises(CastConflict, match="The story changed"):
        story_refs.apply_checklist(sid, [], story=inputs["story_sha"])


# ------------------------------------------------------------------------------------- the board and the bible


def test_a_written_story_opens_the_mood_board_and_goes_into_its_prompt():
    sid = _universe()
    with pytest.raises(CastConflict, match="Add a reference first"):
        world_store.plan_board(sid)
    story_refs.save_story(sid, STORY)
    plan = world_store.plan_board(sid)
    assert f"Story: {STORY}" in plan["prompt"] and plan["sources"]["story"] == story_refs.story_sha(STORY)
    _make_list(sid)
    prompt = world_store.plan_board(sid)["prompt"]
    assert "Characters to show: Mira, Tobin" in prompt
    assert "Places to show: Harbour town, Old lighthouse, Night market" in prompt
    assert "Props to show: Brass lantern, Paper moon" in prompt
    assert len(prompt) <= world_store.BOARD_PROMPT_MAX


def test_the_bible_is_written_from_the_story_and_names_everything_on_the_list():
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    plan = world_store.plan_board(sid)
    board = world_store.create_board(sid, plan, estimate_usd_micros=4_000, approved_total_usd_micros=4_000,
                                     dry_run=True, images=world_store.placeholder_board_images(sid, []),
                                     placeholder=True, requested_by="u-owner")
    world_store.approve_board(sid, board["board_id"], "u-owner")
    inputs = world_store.bible_inputs(sid)
    assert inputs["story"] == STORY and [i["name"] for i in inputs["checklist"]][:2] == ["Mira", "Tobin"]
    user = world_store.bible_messages(inputs)[1]["content"]
    assert f"Story (from the creator):\n{STORY}" in user and "character Mira (Mira is in the story" in user
    bible = world_store.fake_bible(inputs)
    assert [c["name"] for c in bible["characters"]] == ["Mira", "Tobin"]
    assert [p["name"] for p in bible["places"]] == ["Harbour town", "Old lighthouse", "Night market"]
    assert [p["name"] for p in bible["props"]] == ["Brass lantern", "Paper moon"]
    assert bible["bible"].startswith("Mira mends nets")
    applied = world_store.apply_bible(sid, world_store.parse_bible(bible), board_id=inputs["board_id"],
                                      actor="u-owner")
    assert applied["created"] == {"characters": ["Mira", "Tobin"],
                                  "places": ["Harbour town", "Old lighthouse", "Night market"],
                                  "props": ["Brass lantern", "Paper moon"]}
    assert {c["name"] for c in cast_store.list_characters(("series", sid))} == {"Mira", "Tobin"}


def test_after_the_bible_the_photos_on_the_list_go_to_what_it_made():
    """Mira's two photos become her first version's references and the lantern's photo the prop's,
    with the rights they were uploaded with; names the bible did not make get nothing."""
    sid = _universe()
    story_refs.save_story(sid, STORY)
    _make_list(sid)
    items = _by_name(sid)
    face, outfit = _photo(sid, 1, items["Mira"]["item_id"]), _photo(sid, 2, items["Mira"]["item_id"])
    _photo(sid, 3, items["Brass lantern"]["item_id"])
    _photo(sid, 4, items["Tobin"]["item_id"])
    cast_store.create_character(("series", sid), "Mira", actor="u-owner", description="a net mender")
    mira = next(c for c in cast_store.list_characters(("series", sid)) if c["name"] == "Mira")
    lantern = prop_store.create_item(("series", sid), "prop", "Brass lantern", actor="u-owner",
                                     description="a brass lantern")
    attached = story_refs.attach_to_library(sid, {"characters": ["Mira"], "places": [], "props": ["Brass lantern"]},
                                            actor="u-owner")
    assert attached == {"Mira": 2, "Brass lantern": 1}
    uploads = cast_store.get_character(mira["character_id"])["versions"][0]["images"]
    assert sorted(i["sha256"] for i in uploads) == sorted([face["sha256"], outfit["sha256"]])
    assert all(i["attestation"]["rights"] == "original" for i in uploads)
    assert [p["sha256"] for p in prop_store.get_item(lantern["prop_id"])["photos"]] == [
        world_store.list_reference_photos(sid)[2]["sha256"]]
    assert story_refs.attach_to_library(sid, {"characters": ["Nobody"]}) == {}
