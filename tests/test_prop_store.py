"""Props and places library store (hfvg/prop_store.py): items with a fixed kind, photos with the cast
library's upload checks, rights answers and private storage, image files shared with the cast (kept
until nothing uses them), series libraries, the audit log and the migration on an existing studio DB."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3

import pytest

from hfvg import cast_store, prop_store
from hfvg.cast_store import CastConflict, CastError, CastNotFound, UploadTooLarge
from tests.cast_helpers import attestation, image_bytes, new_character, new_place, new_prop, prop_photo

EP = "ep41cast"
LIB = ("episode", EP)


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.delenv("CAST_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("CAST_MIN_IMAGE_PX", raising=False)
    return tmp_path / "private-cast"


def _prop_audit() -> list[dict]:
    with cast_store.connect() as conn:
        rows = conn.execute("SELECT * FROM cast_audit_log WHERE action LIKE 'prop%' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def _gif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (640, 960), (10, 20, 30)).save(buf, "GIF")
    return buf.getvalue()


# ------------------------------------------------------------------------------------- migration


async def test_migration_up_on_an_existing_studio_db(tmp_path):
    """An existing studio DB (episodes, ledgers, users and the cast tables) gains the props tables;
    nothing that was there changes, and running the migrations again is a no-op."""
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import (
        CAST_LIBRARY_MIGRATION, PROPS_LIBRARY_MIGRATION, UNIVERSE_MIGRATION, apply_props_library_sync,
        migrate_cast_library, run_all_migrations)
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    db = str(tmp_path / "existing.db")
    await init_studio_db(db)
    ledger = BudgetLedger(db)
    await ledger.init_db()
    await UserManager(db).init_db()
    await migrate_cast_library(db)
    await create_episode(db, "ep07")
    await ledger.init_episode_budget("ep07")
    await ledger.record_job("ep07", "still-ep07-A01-v1", "A01", "seedream", "final", "L3_final_stills", 50_000)

    def snapshot():
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "budget_lines", "job_ledger", "cast_characters", "cast_images")}
        return tables, data

    tables_before, data_before = snapshot()
    assert "prop_items" not in tables_before
    await run_all_migrations(db)
    await run_all_migrations(db)
    tables_after, data_after = snapshot()
    assert data_after == data_before
    # run_all_migrations also adds the universe tables (tests/test_world_store.py checks them)
    assert tables_after - tables_before == {"prop_items", "prop_images", "shot_plan_edits", "series_profiles",
                                            "character_voices", "universe_notes", "universe_photos",
                                            "universe_boards", "universe_board_images"}
    with sqlite3.connect(db) as conn:
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        assert versions == sorted([CAST_LIBRARY_MIGRATION, PROPS_LIBRARY_MIGRATION, UNIVERSE_MIGRATION])
        assert apply_props_library_sync(conn) is False


def test_a_db_with_only_the_cast_tables_gets_the_props_tables_on_first_use(tmp_path, monkeypatch):
    """The worker (no API startup) opens a DB that has the cast library but not the props library."""
    from hfvg.migrations import apply_cast_library_sync

    db = tmp_path / "cast-only.db"
    conn = sqlite3.connect(db, isolation_level=None)
    apply_cast_library_sync(conn)
    conn.close()
    monkeypatch.setenv("DATABASE_PATH", str(db))
    item = new_prop()
    assert prop_store.get_item(item["prop_id"])["name"] == "Red umbrella"


# ------------------------------------------------------------------------------------- items


def test_a_prop_starts_from_a_photo_in_one_step(cast_dir):
    data = image_bytes("JPEG", seed=3)
    item = prop_store.create_item_with_upload(LIB, "prop", "Red umbrella", "a red umbrella with a wooden handle",
                                              data, filename="umbrella.jpg", attestation=attestation(note="my photo"),
                                              actor="user-1", episode_id=EP)
    assert (item["kind"], item["slug"], item["name"], item["library_kind"], item["library_id"]) == (
        "prop", "red-umbrella", "Red umbrella", "episode", EP)
    [photo] = item["photos"]
    sha = hashlib.sha256(data).hexdigest()
    assert (photo["sha256"], photo["ext"], photo["is_primary"]) == (sha, "jpg", True)
    assert photo["attestation"]["rights"] == "original" and photo["attestation"]["note"] == "my photo"
    stored = prop_store.photo_path(photo)
    assert stored.read_bytes() == data and stored.is_relative_to(cast_dir)
    assert stored.stat().st_mode & 0o777 == 0o640
    assert cast_store.thumb_file(sha).is_file()
    actions = [(a["action"], a["actor"], a["episode_id"]) for a in _prop_audit()]
    assert actions == [("prop_create", "user-1", EP), ("prop_photo_upload", "user-1", EP)]
    assert json.loads(_prop_audit()[1]["details"])["attestation"]["rights"] == "original"


@pytest.mark.parametrize("data,message", [
    (b"definitely not a picture", "not a readable PNG, JPG or WebP"),
    (_gif(), "Only PNG, JPG and WebP"),
    (image_bytes(size=(300, 900)), "at least 512 px"),
    (b"", "empty"),
])
def test_a_refused_photo_leaves_no_prop_behind(data, message):
    with pytest.raises(CastError, match=message):
        prop_store.create_item_with_upload(LIB, "place", "Harbour cabin", "", data, filename="x.png",
                                           attestation=attestation(), actor="tester")
    assert prop_store.list_items(LIB) == []
    assert _prop_audit() == []


def test_the_upload_size_limit_applies_to_prop_photos(monkeypatch):
    item = new_prop()
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", "2000")
    with pytest.raises(UploadTooLarge):
        prop_photo(item, seed=1)
    assert prop_store.get_item(item["prop_id"])["photos"] == []


@pytest.mark.parametrize("kind", ["thing", "", None, "Prop "])
def test_every_item_is_a_prop_or_a_place(kind):
    if kind == "Prop ":
        assert prop_store.create_item(LIB, kind, "Lantern")["kind"] == "prop"
        return
    with pytest.raises(CastError, match="a prop .* or a place"):
        prop_store.create_item(LIB, kind, "Lantern")


def test_names_and_descriptions_are_checked():
    with pytest.raises(CastError, match="Name is required"):
        prop_store.create_item(LIB, "prop", "   ")
    with pytest.raises(CastError, match="keep it under 300"):
        prop_store.create_item(LIB, "prop", "Lantern", "x" * 301)
    with pytest.raises(CastError, match="must be lower-case"):
        prop_store.create_item(LIB, "prop", "Lantern", slug="Bad Id")


def test_ids_are_unique_across_props_and_places():
    place = new_place(name="Harbour cabin")
    prop = new_prop(name="Harbour cabin")
    assert (place["slug"], prop["slug"]) == ("harbour-cabin", "harbour-cabin-2")
    with pytest.raises(CastConflict, match="already exists"):
        prop_store.create_item(LIB, "prop", "Cabin key", slug="harbour-cabin")
    other = prop_store.create_item(("episode", "ep42other"), "prop", "Harbour cabin")
    assert other["slug"] == "harbour-cabin"  # another library has its own ids


def test_edits_keep_the_kind_and_are_audited():
    item = new_prop()
    edited = prop_store.update_item(item["prop_id"], name="Red umbrella", description="  a red umbrella,\n folded ",
                                    actor="user-1", episode_id=EP)
    assert (edited["kind"], edited["description"]) == ("prop", "a red umbrella, folded")
    assert _prop_audit()[-1]["action"] == "prop_update"
    with pytest.raises(CastNotFound):
        prop_store.update_item("pr_000000000000", name="x")


def test_places_are_listed_before_props():
    new_prop(name="Lantern")
    new_place(name="Harbour cabin")
    new_prop(name="Red umbrella")
    assert [(i["kind"], i["slug"]) for i in prop_store.list_items(LIB)] == [
        ("place", "harbour-cabin"), ("prop", "lantern"), ("prop", "red-umbrella")]
    assert prop_store.find_item(LIB, "lantern")["name"] == "Lantern"
    assert prop_store.find_item(LIB, "missing") is None


# ------------------------------------------------------------------------------------- photos


def test_photos_are_deduplicated_and_the_first_is_the_main_one():
    item = new_prop()
    first = prop_photo(item, seed=1)
    again = prop_photo(item, seed=1)
    second = prop_photo(item, seed=2)
    assert first["is_primary"] and not second["is_primary"]
    assert again["image_id"] == first["image_id"] and again["duplicate"] is True and first["duplicate"] is False
    assert [p["image_id"] for p in prop_store.get_item(item["prop_id"])["photos"]] == [
        first["image_id"], second["image_id"]]
    prop_store.set_primary(item["prop_id"], second["image_id"], "user-1")
    assert [p["image_id"] for p in prop_store.reference_photos(prop_store.get_item(item["prop_id"]))] == [
        second["image_id"], first["image_id"]]
    with pytest.raises(CastNotFound):
        prop_store.set_primary(new_place()["prop_id"], first["image_id"])


def test_each_item_keeps_at_most_eight_photos():
    item = new_prop()
    for seed in range(prop_store.MAX_PHOTOS_PER_ITEM):
        prop_photo(item, seed=seed)
    with pytest.raises(CastConflict, match="already has 8 photos"):
        prop_photo(item, seed=99)
    assert prop_photo(item, seed=3)["duplicate"] is True  # the same bytes again are not a new photo


def test_deleting_the_main_photo_makes_the_next_one_main_and_removes_the_file():
    item = new_prop()
    first, second = prop_photo(item, seed=1), prop_photo(item, seed=2)
    path = prop_store.photo_path(first)
    assert prop_store.delete_photo(first["image_id"], "user-1", prop_id=item["prop_id"]) == {
        "deleted": first["image_id"], "file_removed": True}
    assert not path.exists() and not cast_store.thumb_file(first["sha256"]).exists()
    [left] = prop_store.get_item(item["prop_id"])["photos"]
    assert left["image_id"] == second["image_id"] and left["is_primary"] is True
    with pytest.raises(CastNotFound):
        prop_store.delete_photo(second["image_id"], prop_id=new_place()["prop_id"])


def test_a_file_shared_by_a_cast_picture_and_a_prop_photo_stays_until_neither_uses_it():
    data = image_bytes(seed=7)
    pip = new_character(LIB, name="Pip")
    picture = cast_store.add_upload(pip["character_id"], 1, data, filename="pip.png", attestation=attestation(),
                                    actor="tester")
    item = new_prop()
    photo = prop_store.add_photo(item["prop_id"], data, filename="umbrella.png", attestation=attestation(),
                                 actor="tester")
    path = cast_store.image_path(picture)
    assert prop_store.photo_path(photo) == path
    assert cast_store.delete_image(picture["image_id"], "tester")["file_removed"] is False
    assert path.is_file() and cast_store.thumb_file(photo["sha256"]).is_file()
    assert prop_store.delete_photo(photo["image_id"], "tester")["file_removed"] is True
    assert not path.exists()

    # the other way round: deleting a prop keeps the picture a character still uses
    picture = cast_store.add_upload(pip["character_id"], 1, data, filename="pip.png", attestation=attestation(),
                                    actor="tester")
    prop_store.add_photo(item["prop_id"], data, filename="umbrella.png", attestation=attestation(), actor="tester")
    assert prop_store.delete_item(item["prop_id"], "tester") == {"deleted": item["prop_id"], "photos_removed": 1}
    assert cast_store.image_path(picture).is_file()
    with pytest.raises(CastNotFound):
        prop_store.get_item(item["prop_id"])


def test_photo_library_names_the_library_of_the_photo():
    item = new_place(("series", "sr_000000000001"))
    photo = prop_photo(item, seed=4)
    assert prop_store.photo_library(photo["image_id"]) == ("series", "sr_000000000001")
    assert prop_store.photo_library("pi_000000000000") is None
    assert prop_store.item_library(item["prop_id"]) == ("series", "sr_000000000001")


# ------------------------------------------------------------------------------------- series


def test_an_episode_joining_a_series_brings_its_props_and_places():
    series = cast_store.create_series("Harbour tales", "user-1")
    new_prop(name="Red umbrella")
    new_place(name="Harbour cabin")
    linked = cast_store.link_episode(EP, series["series_id"], "user-1")
    assert linked["moved_props"] == ["harbour-cabin", "red-umbrella"] and linked["moved_characters"] == []
    library = cast_store.library_for_episode(EP)
    assert library == ("series", series["series_id"])
    assert {i["slug"] for i in prop_store.list_items(library)} == {"red-umbrella", "harbour-cabin"}
    assert prop_store.list_items(LIB) == []
    unlinked = cast_store.link_episode(EP, None, "user-1")
    assert unlinked["moved_props"] == [] and len(prop_store.list_items(library)) == 2  # the series keeps them


def test_a_prop_id_the_series_already_uses_refuses_the_link_and_moves_nothing():
    series = cast_store.create_series("Harbour tales", "user-1")
    new_prop(("series", series["series_id"]), name="Red umbrella")
    new_character(LIB, name="Pip")
    new_prop(name="Red umbrella")
    with pytest.raises(CastConflict, match="props or places with the ids \\['red-umbrella'\\]"):
        cast_store.link_episode(EP, series["series_id"], "user-1")
    assert cast_store.library_for_episode(EP) == LIB
    assert [c["slug"] for c in cast_store.list_characters(LIB)] == ["pip"]
    assert [i["slug"] for i in prop_store.list_items(LIB)] == ["red-umbrella"]
