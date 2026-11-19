"""Cast library store (hfvg/cast_store.py): reference uploads (type, real decode, minimum size, size
limit, sha256 dedupe, private storage), the rights attestation, versions (outfits), the sheet lock
and its immutability, series libraries, the audit log and the migration on an existing studio DB."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3

import pytest

from hfvg import cast_store
from tests.cast_helpers import attestation, complete_sheet, image_bytes, new_character, upload


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.delenv("CAST_UPLOAD_MAX_BYTES", raising=False)
    monkeypatch.delenv("CAST_MIN_IMAGE_PX", raising=False)
    return tmp_path / "private-cast"


def _audit_actions(character_id: str) -> list[str]:
    return [row["action"] for row in reversed(cast_store.list_audit(character_id=character_id))]


# ------------------------------------------------------------------------------------- migration


async def test_migration_up_on_an_existing_studio_db(tmp_path):
    """An existing studio DB (episodes, ledgers, users, rows in them) gains the cast tables; nothing
    that was there changes, and running the migrations again is a no-op."""
    from hfvg.budget import BudgetLedger
    from hfvg.migrations import CAST_LIBRARY_MIGRATION, apply_cast_library_sync, run_all_migrations
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    db = str(tmp_path / "existing.db")
    await init_studio_db(db)
    ledger = BudgetLedger(db)
    await ledger.init_db()
    await UserManager(db).init_db()
    await create_episode(db, "ep07")
    await ledger.init_episode_budget("ep07")
    await ledger.record_job("ep07", "still-ep07-A01-v1", "A01", "seedream", "final", "L3_final_stills", 50_000)

    def snapshot():
        with sqlite3.connect(db) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            data = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
                    for t in ("episodes", "budget_lines", "job_ledger")}
        return tables, data

    tables_before, data_before = snapshot()
    assert "cast_characters" not in tables_before
    await run_all_migrations(db)
    await run_all_migrations(db)
    tables_after, data_after = snapshot()
    assert data_after == data_before
    assert {"series", "episode_series", "cast_characters", "cast_versions", "cast_images", "cast_sheet_jobs",
            "cast_audit_log", "schema_migrations"} <= tables_after - tables_before
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                            (CAST_LIBRARY_MIGRATION,)).fetchone()[0] == 1
        columns = {r[1] for r in conn.execute("PRAGMA table_info(cast_versions)")}
        assert {"locked", "locked_at", "locked_by", "lock_hash", "locked_description", "derived_from_version",
                "sheet_stage"} <= columns
        assert apply_cast_library_sync(conn) is False


def test_the_worker_side_opens_the_cast_tables_on_a_db_without_them(tmp_path, monkeypatch):
    """Processes without the API's startup (the worker) get the tables on first use."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "fresh.db"))
    character = new_character()
    assert cast_store.get_character(character["character_id"])["name"] == "Pip"


# ------------------------------------------------------------------------------------- uploads


def test_upload_validates_stores_privately_and_audits(cast_dir):
    character = new_character()
    data = image_bytes("JPEG", seed=3)
    image = cast_store.add_upload(character["character_id"], 1, data, filename="pip-front.jpg",
                                  attestation=attestation(note="drawn by me"), actor="user-1", episode_id="ep41cast")
    sha = hashlib.sha256(data).hexdigest()
    assert (image["sha256"], image["ext"], image["mime"], image["role"]) == (sha, "jpg", "image/jpeg", "upload")
    assert image["is_primary"] is True and image["duplicate"] is False
    assert image["attestation"]["rights"] == "original" and image["attestation"]["note"] == "drawn by me"
    stored = cast_store.image_path(image)
    assert stored.read_bytes() == data
    assert stored.is_relative_to(cast_dir) and "public" not in stored.parts
    assert stored.stat().st_mode & 0o777 == 0o640
    assert cast_store.thumb_file(sha).is_file()
    audit = cast_store.list_audit(character_id=character["character_id"])[0]
    details = json.loads(audit["details"])
    assert (audit["action"], audit["actor"], audit["episode_id"], audit["image_id"]) == (
        "image_upload", "user-1", "ep41cast", image["image_id"])
    assert details["sha256"] == sha and details["attestation"]["rights"] == "original"


@pytest.mark.parametrize("fmt,ext", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp")])
def test_png_jpeg_and_webp_are_accepted(fmt, ext):
    info = cast_store.inspect_image(image_bytes(fmt))
    assert (info["format"], info["ext"], info["width"], info["height"]) == (fmt, ext, 640, 960)


def test_other_types_are_refused():
    from PIL import Image
    gif = io.BytesIO()
    Image.new("RGB", (640, 960), (10, 10, 10)).save(gif, "GIF")
    bmp = io.BytesIO()
    Image.new("RGB", (640, 960), (10, 10, 10)).save(bmp, "BMP")
    for data in (gif.getvalue(), bmp.getvalue()):
        with pytest.raises(cast_store.CastError, match="Only PNG, JPG and WebP"):
            cast_store.inspect_image(data)
    for data in (b"%PDF-1.4 not an image", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"GIF89a" + b"\x00" * 64):
        with pytest.raises(cast_store.CastError, match="not a readable PNG, JPG or WebP"):
            cast_store.inspect_image(data)


def test_a_renamed_file_is_judged_by_its_content():
    """A GIF named .png is still a GIF: the decoder decides, not the name or the content type."""
    from PIL import Image
    gif = io.BytesIO()
    Image.new("RGB", (640, 960)).save(gif, "GIF")
    character = new_character()
    with pytest.raises(cast_store.CastError, match="Only PNG, JPG and WebP"):
        cast_store.add_upload(character["character_id"], 1, gif.getvalue(), filename="pip.png",
                              attestation=attestation(), actor="t")


def test_corrupt_and_truncated_images_are_refused():
    png = image_bytes("PNG")
    for data in (png[: len(png) // 2], png[:-100]):
        with pytest.raises(cast_store.CastError, match="damaged or incomplete"):
            cast_store.inspect_image(data)
    bad_crc = bytearray(png)
    middle = len(bad_crc) // 2
    bad_crc[middle:middle + 200] = b"\xff" * 200
    with pytest.raises(cast_store.CastError, match="damaged or incomplete"):
        cast_store.inspect_image(bytes(bad_crc))
    jpeg = image_bytes("JPEG")
    with pytest.raises(cast_store.CastError, match="damaged or incomplete"):
        cast_store.inspect_image(jpeg[: len(jpeg) // 3])
    with pytest.raises(cast_store.CastError, match="not a readable"):
        cast_store.inspect_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    with pytest.raises(cast_store.CastError, match="empty"):
        cast_store.inspect_image(b"")


def test_too_small_images_are_refused_and_the_minimum_is_configurable(monkeypatch):
    with pytest.raises(cast_store.CastError, match="at least 128 px"):
        cast_store.inspect_image(image_bytes(size=(127, 900)))
    assert cast_store.inspect_image(image_bytes(size=(128, 128)))["width"] == 128
    monkeypatch.setenv("CAST_MIN_IMAGE_PX", "1024")
    with pytest.raises(cast_store.CastError, match="at least 1024 px"):
        cast_store.inspect_image(image_bytes(size=(1000, 1400)))


def test_a_too_small_image_is_refused_with_what_to_do_next():
    with pytest.raises(cast_store.CastError) as refused:
        cast_store.inspect_image(image_bytes(size=(100, 900)))
    assert str(refused.value) == "The image is 100x900 px; each side must be at least 128 px: choose a bigger one"


def test_small_pictures_are_accepted_with_a_plain_note(monkeypatch):
    for size in ((128, 900), (255, 255), (256, 400), (511, 2000), (300, 300)):
        assert cast_store.inspect_image(image_bytes(size=size))["width"] == size[0]
    assert cast_store.size_note(128, 900) == cast_store.VERY_SMALL_IMAGE_NOTE
    assert cast_store.size_note(900, 255) == cast_store.VERY_SMALL_IMAGE_NOTE
    assert cast_store.size_note(256, 400) == "Small image, we'll sharpen it up."
    assert cast_store.size_note(511, 2000) == "Small image, we'll sharpen it up."
    assert cast_store.size_note(512, 512) is None and cast_store.size_note(4000, 3000) is None
    assert cast_store.size_note(None, 300) is None and cast_store.size_note(True, 300) is None
    assert cast_store.size_limits() == {"min_image_px": 128, "small_image_px": 256, "sharpen_below_px": 512}
    # the note follows the size the normalizer upscales to
    monkeypatch.setenv("REF_MIN_SHORT_SIDE", "768")
    assert cast_store.size_note(600, 900) == "Small image, we'll sharpen it up."
    assert cast_store.size_limits()["sharpen_below_px"] == 768


def test_oversize_images_are_refused_with_413(monkeypatch):
    data = image_bytes(size=(900, 900))
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) - 1))
    with pytest.raises(cast_store.UploadTooLarge) as err:
        cast_store.inspect_image(data)
    assert err.value.status == 413
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", "lots")
    with pytest.raises(cast_store.CastError, match="whole number"):
        cast_store.max_upload_bytes()


def test_animated_images_are_refused():
    from PIL import Image
    frames = [Image.new("RGB", (640, 960), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, "WEBP", save_all=True, append_images=frames[1:], duration=100)
    with pytest.raises(cast_store.CastError, match="Animated"):
        cast_store.inspect_image(buf.getvalue())


def test_the_same_picture_twice_is_stored_once():
    character = new_character()
    first = upload(character, seed=5)
    again = upload(character, seed=5)
    assert again["image_id"] == first["image_id"] and again["duplicate"] is True
    assert len(cast_store.get_version(character["character_id"], 1)["images"]) == 1
    assert _audit_actions(character["character_id"])[-1] == "image_upload_duplicate"


def test_uploads_per_version_are_limited(monkeypatch):
    monkeypatch.setattr(cast_store, "MAX_UPLOADS_PER_VERSION", 2)
    character = new_character()
    upload(character, seed=1)
    upload(character, seed=2)
    with pytest.raises(cast_store.CastConflict, match="limit is 2"):
        upload(character, seed=3)


def test_primary_upload_and_delete():
    character = new_character()
    a, b = upload(character, seed=1), upload(character, seed=2)
    assert a["is_primary"] and not cast_store.get_image(b["image_id"])["is_primary"]
    cast_store.set_primary(character["character_id"], 1, b["image_id"], "t")
    assert cast_store.get_image(b["image_id"])["is_primary"] and not cast_store.get_image(a["image_id"])["is_primary"]
    result = cast_store.delete_image(b["image_id"], "t", character_id=character["character_id"])
    assert result["file_removed"] is True and not cast_store.image_path(b).exists()
    assert cast_store.get_image(a["image_id"])["is_primary"] is True
    with pytest.raises(cast_store.CastNotFound):
        cast_store.get_image(b["image_id"])


# ------------------------------------------------------------------------------------- attestation


def test_an_upload_needs_the_rights_attestation():
    for rights in ("", None, "found it online", "public"):
        with pytest.raises(cast_store.CastError, match="original"):
            cast_store.validate_attestation(rights, False, False)
    assert cast_store.validate_attestation("owned", False, False)["rights"] == "owned"


def test_real_people_need_the_likeness_consent_attestation():
    with pytest.raises(cast_store.CastError, match="consent"):
        cast_store.validate_attestation("owned", True, False)
    ok = cast_store.validate_attestation("owned", "true", "true", "the actor signed a release")
    assert ok["depicts_real_person"] is True and ok[cast_store.LIKENESS_CONSENT] is True
    assert cast_store.LIKENESS_CONSENT == "likeness_consent"  # the existing gate's name
    with pytest.raises(cast_store.CastError, match="true or false"):
        cast_store.validate_attestation("owned", "maybe", False)


# ------------------------------------------------------------------------------------- characters, versions


def test_characters_get_unique_ids_and_text_limits():
    a = new_character(name="Moss Fern")
    b = new_character(name="Moss Fern")
    assert (a["slug"], b["slug"]) == ("moss-fern", "moss-fern-2")
    with pytest.raises(cast_store.CastConflict):
        new_character(name="Other", slug="moss-fern")
    with pytest.raises(cast_store.CastError, match="Name"):
        new_character(name="   ")
    with pytest.raises(cast_store.CastError, match="Description"):
        new_character(name="Long", description="x" * (cast_store.DESCRIPTION_MAX + 1))
    with pytest.raises(cast_store.CastError, match="lower-case"):
        new_character(name="Bad", slug="Bad Id!")


def _row_counts() -> dict:
    with cast_store.connect() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("cast_characters", "cast_versions", "cast_images", "cast_audit_log")}


def _stored_files(cast_dir) -> list:
    return sorted(p for p in (cast_dir / "images").rglob("*") if p.is_file()) if (cast_dir / "images").exists() else []


def test_a_character_starts_from_a_reference_picture_in_one_step(cast_dir):
    data = image_bytes("JPEG", seed=5)
    attested = attestation(note="drawn by me")
    character = cast_store.create_character_with_upload(
        ("episode", "ep41cast"), "Moss Fern", "lead", "a small round robot with a teal shell", data,
        filename="moss-front.jpg", attestation=attested, actor="user-1", episode_id="ep41cast")
    assert (character["name"], character["slug"], character["role"], character["library_id"]) == (
        "Moss Fern", "moss-fern", "lead", "ep41cast")
    [version] = character["versions"]
    [image] = version["images"]
    sha = hashlib.sha256(data).hexdigest()
    assert (version["version"], version["locked"], image["version"], image["role"], image["source"]) == (
        1, False, 1, cast_store.UPLOAD_ROLE, "upload")
    assert (image["is_primary"], image["sha256"], image["original_filename"]) == (True, sha, "moss-front.jpg")
    assert image["attestation"] == attested
    assert cast_store.image_path(image).read_bytes() == data and cast_store.thumb_file(sha).is_file()
    assert _audit_actions(character["character_id"]) == ["character_create", "image_upload"]
    upload_row = cast_store.list_audit(character_id=character["character_id"])[0]
    assert (upload_row["actor"], upload_row["episode_id"], upload_row["image_id"]) == ("user-1", "ep41cast", image["image_id"])
    assert json.loads(upload_row["details"]) == {
        "version": 1, "sha256": sha, "bytes": len(data), "width": 640, "height": 960, "format": "JPEG",
        "filename": "moss-front.jpg", "attestation": attested}
    # the same name again gets its own id, as without a picture
    again = cast_store.create_character_with_upload(("episode", "ep41cast"), "Moss Fern", "", "", image_bytes(seed=6),
                                                    filename=None, attestation=attestation(), actor="user-1")
    assert again["slug"] == "moss-fern-2"


@pytest.mark.parametrize("name,description,data,error", [
    ("   ", "", image_bytes(), "Name is required"),
    ("Pip", "x" * (cast_store.DESCRIPTION_MAX + 1), image_bytes(), "Description"),
    ("Pip", "", image_bytes(size=(100, 900)), "each side must be at least 128 px"),
    ("Pip", "", b"just some notes, not a picture", "not a readable PNG, JPG or WebP image"),
])
def test_a_refused_character_from_a_picture_leaves_nothing(cast_dir, name, description, data, error):
    before = _row_counts()
    with pytest.raises(cast_store.CastError, match=error):
        cast_store.create_character_with_upload(("episode", "ep41cast"), name, "", description, data,
                                                filename="pip.png", attestation=attestation(), actor="t")
    assert _row_counts() == before and _stored_files(cast_dir) == []


def test_a_full_cast_refuses_a_character_from_a_picture_and_stores_no_picture(cast_dir, monkeypatch):
    monkeypatch.setattr(cast_store, "MAX_CHARACTERS_PER_LIBRARY", 1)
    new_character()
    before = _row_counts()
    with pytest.raises(cast_store.CastConflict, match="the limit is 1"):
        cast_store.create_character_with_upload(("episode", "ep41cast"), "Moss", "", "", image_bytes(seed=2),
                                                filename="moss.png", attestation=attestation(), actor="t")
    assert _row_counts() == before and _stored_files(cast_dir) == []


def test_a_picture_that_cannot_be_recorded_takes_its_character_with_it(cast_dir, monkeypatch):
    """One transaction: when recording the picture fails, the character and its version are not kept."""
    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(cast_store, "_store_image", broken)
    before = _row_counts()
    with pytest.raises(sqlite3.OperationalError):
        cast_store.create_character_with_upload(("episode", "ep41cast"), "Pip", "", "", image_bytes(seed=3),
                                                filename="pip.png", attestation=attestation(), actor="t")
    assert _row_counts() == before
    assert cast_store.list_characters(("episode", "ep41cast")) == []


def test_a_new_outfit_derives_only_from_a_locked_master():
    character = new_character()
    cid = character["character_id"]
    with pytest.raises(cast_store.CastConflict, match="not locked"):
        cast_store.create_version(cid, label="Raincoat", outfit="yellow raincoat", derive_from_version=1)
    sheet = complete_sheet(character)
    cast_store.approve_version(cid, 1, "t")
    v2 = cast_store.create_version(cid, label="Raincoat", outfit="yellow raincoat", derive_from_version=1, actor="t")
    assert (v2["version"], v2["derived_from_version"], v2["locked"], v2["outfit"]) == (2, 1, False, "yellow raincoat")
    assert "yellow raincoat" in v2["clause"]
    full = cast_store.get_character(cid)
    assert full["default_version"] == 1
    # v2's options are made from v1's locked master, never from scene stills
    from hfvg.cast_sheets import _derived_master
    assert _derived_master(full, next(v for v in full["versions"] if v["version"] == 2))["image_id"] == \
        sheet[cast_store.MASTER_ROLE]["image_id"]
    complete_sheet(cast_store.get_character(cid), 2, seed=300)
    cast_store.approve_version(cid, 2, "t")
    # locking an outfit leaves shots that did not pick one on the main look
    assert cast_store.get_character(cid)["default_version"] == 1


# ------------------------------------------------------------------------------------- lock


def test_approve_needs_the_complete_sheet_and_no_placeholders():
    character = new_character()
    cid = character["character_id"]
    with pytest.raises(cast_store.CastConflict, match="not complete"):
        cast_store.approve_version(cid, 1, "t")
    cast_store.add_generated_image(cid, 1, image_bytes(seed=9), role=cast_store.MASTER_ROLE, job_id="j1",
                                   placeholder=False)
    with pytest.raises(cast_store.CastConflict, match="front view"):
        cast_store.approve_version(cid, 1, "t")
    other = new_character(name="Moss")
    complete_sheet(other, placeholder=True)
    with pytest.raises(cast_store.CastConflict, match="placeholder"):
        cast_store.approve_version(other["character_id"], 1, "t")
    locked = cast_store.approve_version(other["character_id"], 1, "t", allow_placeholder=True)
    assert locked["locked"] is True


def test_a_locked_version_cannot_be_changed():
    character = new_character()
    cid = character["character_id"]
    up = upload(character, seed=1)
    sheet = complete_sheet(character)
    locked = cast_store.approve_version(cid, 1, "approver")
    assert locked["locked"] and locked["locked_by"] == "approver" and len(locked["lock_hash"]) == 64
    assert locked["locked_description"].startswith("Pip: a small round robot")
    refused = [
        lambda: upload(character, seed=2),
        lambda: cast_store.add_generated_image(cid, 1, image_bytes(seed=50), role=cast_store.HEAD_ROLE,
                                               job_id="j9", placeholder=False),
        lambda: cast_store.delete_image(sheet[cast_store.MASTER_ROLE]["image_id"], "t"),
        lambda: cast_store.delete_image(up["image_id"], "t"),
        lambda: cast_store.set_primary(cid, 1, up["image_id"], "t"),
        lambda: cast_store.pick_master(cid, 1, sheet[cast_store.MASTER_ROLE]["image_id"], "t"),
        lambda: cast_store.reject_sheet(cid, 1, "all", "t"),
        lambda: cast_store.update_version(cid, 1, outfit="a new hat"),
        lambda: cast_store.approve_version(cid, 1, "t"),
        lambda: cast_store.delete_character(cid, "t"),
    ]
    for attempt in refused:
        with pytest.raises(cast_store.CastConflict):
            attempt()
    assert cast_store.verify_lock(cid, 1) == []
    after = cast_store.get_version(cid, 1)
    assert after["lock_hash"] == locked["lock_hash"] and cast_store.image_path(sheet["master"]).is_file()


def test_renaming_keeps_the_locked_description_and_hash():
    character = new_character()
    cid = character["character_id"]
    complete_sheet(character)
    locked = cast_store.approve_version(cid, 1, "t")
    cast_store.update_character(cid, name="Pip the Second", description="now completely different")
    version = cast_store.get_version(cid, 1)
    assert version["clause"] == locked["locked_description"] and "different" not in version["clause"]
    assert cast_store.verify_lock(cid, 1) == []


def test_tampering_with_a_locked_sheet_is_detected(cast_dir):
    character = new_character()
    cid = character["character_id"]
    sheet = complete_sheet(character)
    cast_store.approve_version(cid, 1, "t")
    with cast_store.connect() as conn:
        conn.execute("UPDATE cast_versions SET outfit = 'swapped' WHERE character_id = ?", (cid,))
    assert any("lock hash" in p for p in cast_store.verify_lock(cid, 1))
    with cast_store.connect() as conn:
        conn.execute("UPDATE cast_versions SET outfit = '' WHERE character_id = ?", (cid,))
    assert cast_store.verify_lock(cid, 1) == []
    path = cast_store.image_path(sheet[cast_store.HEAD_ROLE])
    path.write_bytes(image_bytes(size=(960, 640), seed=999))
    assert any("changed on disk" in p for p in cast_store.verify_lock(cid, 1))
    path.unlink()
    assert any("missing on disk" in p for p in cast_store.verify_lock(cid, 1))


def test_only_a_forced_delete_with_a_reason_removes_a_locked_character():
    character = new_character()
    cid = character["character_id"]
    complete_sheet(character)
    cast_store.approve_version(cid, 1, "t")
    with pytest.raises(cast_store.CastError, match="reason"):
        cast_store.delete_character(cid, "admin", force=True, reason=" ")
    out = cast_store.delete_character(cid, "admin", force=True, reason="duplicate character")
    assert out["images_removed"] == 6
    assert _audit_actions(cid)[-1] == "character_delete_forced"
    with pytest.raises(cast_store.CastNotFound):
        cast_store.get_character(cid)


def test_sheet_stages_follow_pick_and_reject():
    character = new_character()
    cid = character["character_id"]
    options = [cast_store.add_generated_image(cid, 1, image_bytes(seed=s), role=cast_store.OPTION_ROLE,
                                              job_id=f"o{s}", placeholder=False) for s in (1, 2, 3)]
    assert cast_store.get_version(cid, 1)["sheet_stage"] == "options_ready"
    assert cast_store.pick_master(cid, 1, options[1]["image_id"], "t")["sheet_stage"] == "master_picked"
    # each view needs bytes of its own: a version keeps one image per sha256, so a view with the
    # bytes of an option or of another view is stored as that image, not as the view
    for seed, role in enumerate((*cast_store.TURNAROUND_ROLES, cast_store.HEAD_ROLE), start=10):
        cast_store.add_generated_image(cid, 1, image_bytes(seed=seed), role=role, job_id=role, placeholder=False)
    assert cast_store.get_version(cid, 1)["sheet_stage"] == "turnaround_ready"
    # picking another master discards the turnaround built from the old one
    assert cast_store.pick_master(cid, 1, options[0]["image_id"], "t")["sheet_stage"] == "master_picked"
    assert cast_store.reject_sheet(cid, 1, "all", "t")["sheet_stage"] == "none"


# ------------------------------------------------------------------------------------- series


def test_an_episode_joins_a_series_and_its_characters_move_with_it():
    series = cast_store.create_series("Harbor Tales", "user-1", "user-1")
    own = new_character(("episode", "ep41cast"), name="Pip")
    assert cast_store.library_for_episode("ep41cast") == ("episode", "ep41cast")
    moved = cast_store.link_episode("ep41cast", series["series_id"], "user-1")
    assert moved["moved_characters"] == ["pip"]
    assert cast_store.library_for_episode("ep41cast") == ("series", series["series_id"])
    cast_store.link_episode("ep42cast", series["series_id"], "user-1")
    assert [c["character_id"] for c in cast_store.list_characters(cast_store.library_for_episode("ep42cast"))] == \
        [own["character_id"]]
    new_character(("episode", "ep43cast"), name="Pip")
    with pytest.raises(cast_store.CastConflict, match="pip"):
        cast_store.link_episode("ep43cast", series["series_id"], "user-1")
    out = cast_store.link_episode("ep41cast", None, "user-1")
    assert out["series_id"] is None and cast_store.library_for_episode("ep41cast") == ("episode", "ep41cast")
    assert cast_store.get_series(series["series_id"])["episodes"] == ["ep42cast"]
