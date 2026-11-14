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
    data = image_bytes("PNG")
    with pytest.raises(cast_store.CastError, match="damaged or incomplete|not a readable"):
        cast_store.inspect_image(data[: len(data) // 2])
    broken = bytearray(image_bytes("JPEG"))
    broken[400:900] = b"\x00" * 500
    with pytest.raises(cast_store.CastError):
        cast_store.inspect_image(bytes(broken[:-200]))
    with pytest.raises(cast_store.CastError, match="empty"):
        cast_store.inspect_image(b"")


def test_too_small_images_are_refused_and_the_minimum_is_configurable(monkeypatch):
    with pytest.raises(cast_store.CastError, match="at least 512 px"):
        cast_store.inspect_image(image_bytes(size=(511, 900)))
    assert cast_store.inspect_image(image_bytes(size=(512, 512)))["width"] == 512
    monkeypatch.setenv("CAST_MIN_IMAGE_PX", "1024")
    with pytest.raises(cast_store.CastError, match="at least 1024 px"):
        cast_store.inspect_image(image_bytes(size=(1000, 1400)))


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
    assert full["default_version"] == 1  # the newest LOCKED version until v2 is approved
    # v2's options are made from v1's locked master, never from scene stills
    from hfvg.cast_sheets import _derived_master
    assert _derived_master(full, next(v for v in full["versions"] if v["version"] == 2))["image_id"] == \
        sheet[cast_store.MASTER_ROLE]["image_id"]
    complete_sheet(cast_store.get_character(cid), 2, seed=300)
    cast_store.approve_version(cid, 2, "t")
    assert cast_store.get_character(cid)["default_version"] == 2


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
    for role in (*cast_store.TURNAROUND_ROLES, cast_store.HEAD_ROLE):
        cast_store.add_generated_image(cid, 1, image_bytes(seed=hash(role) % 1000), role=role, job_id=role,
                                       placeholder=False)
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
