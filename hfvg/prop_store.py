"""Props and places library: the things and settings that come back in a story's shots (a red
umbrella, the harbour cabin), each with a name, a short description and reference photos.

Rows live in the studio DB (tables from the props library migration, hfvg.migrations). Photos share
the cast library's private image store and its rules (hfvg.cast_store): the same upload checks
(``inspect_image``), the same rights answers (``validate_attestation``), files written once under
``CAST_STORAGE_DIR`` and never served statically, and the same audit log (``cast_audit_log``).

Libraries are the cast's: the episodes of a series share the series library; an episode without a
series has its own, and it moves into the series with the cast (``cast_store.link_episode``). Shots
name props and places by ``slug``, unique per library across both kinds; an item's kind is set when
it is made.

Sync sqlite3, like the cast store: the planner runs inside activities and the API calls these
through a worker thread.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from hfvg import cast_store
from hfvg.cast_prompt import SLUG_RE, slugify
from hfvg.cast_store import CastConflict, CastError, CastNotFound, _clean, _new_id, _now, _tx, audit, connect

KINDS = ("prop", "place")
MAX_ITEMS_PER_LIBRARY = 60
MAX_PHOTOS_PER_ITEM = 8
NAME_MAX = cast_store.NAME_MAX
DESCRIPTION_MAX = 300


def _kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in KINDS:
        raise CastError("Choose what this is: a prop (a thing in the shot) or a place (where the shot happens)")
    return kind


def _series_id(library: tuple[str, str]) -> str | None:
    return library[1] if library[0] == "series" else None


def _item_row(conn: sqlite3.Connection, prop_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM prop_items WHERE prop_id = ?", (prop_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Prop or place {prop_id} not found")
    return row


def _unique_slug(conn: sqlite3.Connection, library: tuple[str, str], wanted: str) -> str:
    taken = {r[0] for r in conn.execute("SELECT slug FROM prop_items WHERE library_kind = ? AND library_id = ?",
                                        library)}
    if wanted not in taken:
        return wanted
    for n in range(2, 100):
        candidate = f"{wanted[:29]}-{n}"
        if candidate not in taken:
            return candidate
    raise CastConflict("Too many props and places with this name")


def _fields(kind: Any, name: Any, description: Any, slug: str | None) -> dict:
    fields = {"kind": _kind(kind), "name": _clean(name, "Name", NAME_MAX, required=True),
              "description": _clean(description, "Description", DESCRIPTION_MAX), "slug": None}
    if slug is not None:
        fields["slug"] = str(slug).strip().lower()
        if not SLUG_RE.match(fields["slug"]):
            raise CastError(f"Id {fields['slug']!r} must be lower-case letters, digits and dashes (max 32)")
    return fields


def _insert_item(conn: sqlite3.Connection, library: tuple[str, str], fields: dict, actor: str | None,
                 episode_id: str | None) -> str:
    """The item row, inside the caller's transaction. Returns the prop id."""
    count = conn.execute("SELECT COUNT(*) FROM prop_items WHERE library_kind = ? AND library_id = ?",
                         library).fetchone()[0]
    if count >= MAX_ITEMS_PER_LIBRARY:
        raise CastConflict(f"This library already has {count} props and places (the limit is {MAX_ITEMS_PER_LIBRARY})")
    slug = fields["slug"]
    if slug is not None:
        if conn.execute("SELECT 1 FROM prop_items WHERE library_kind = ? AND library_id = ? AND slug = ?",
                        (*library, slug)).fetchone():
            raise CastConflict(f"A prop or place with the id {slug!r} already exists in this library")
    else:
        slug = _unique_slug(conn, library, slugify(fields["name"]))
    prop_id = _new_id("pr")
    now = _now()
    conn.execute("INSERT INTO prop_items (prop_id, library_kind, library_id, kind, slug, name, description, "
                 "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (prop_id, *library, fields["kind"], slug, fields["name"], fields["description"], actor, now, now))
    audit(conn, actor, "prop_create", series_id=_series_id(library), episode_id=episode_id,
          details={"prop_id": prop_id, "kind": fields["kind"], "slug": slug, "name": fields["name"]})
    return prop_id


def create_item(library: tuple[str, str], kind: str, name: str, description: str = "", *, slug: str | None = None,
                actor: str | None = None, episode_id: str | None = None) -> dict:
    """A new prop or place in the library. An explicit slug that is taken is refused; a slug made
    from the name gets a number when taken."""
    fields = _fields(kind, name, description, slug)
    with connect() as conn, _tx(conn):
        prop_id = _insert_item(conn, library, fields, actor, episode_id)
    return get_item(prop_id)


def create_item_with_upload(library: tuple[str, str], kind: str, name: str, description: str, data: bytes, *,
                            filename: str | None, attestation: dict, actor: str | None,
                            episode_id: str | None = None) -> dict:
    """A new prop or place that starts from a photo, in one step. The photo gets the checks of an
    upload first; the item and its photo (primary) are recorded in one transaction, so a refused
    photo leaves no item and a refused item no photo record."""
    fields = _fields(kind, name, description, None)
    info = cast_store.inspect_image(data)
    with connect() as conn:
        with _tx(conn):
            prop_id = _insert_item(conn, library, fields, actor, episode_id)
            image_id, _ = _store_photo(conn, prop_id, data, info, actor=actor, filename=filename,
                                       attestation=attestation, primary=True)
            audit(conn, actor, "prop_photo_upload", series_id=_series_id(library), episode_id=episode_id,
                  image_id=image_id, details=_upload_details(prop_id, info, filename, attestation))
    cast_store.ensure_thumb(info["sha256"], info["ext"])
    return get_item(prop_id)


def update_item(prop_id: str, *, name: str | None = None, description: str | None = None,
                actor: str | None = None, episode_id: str | None = None) -> dict:
    """Edit the name or description. A run already planned keeps the words it was planned with."""
    changes: dict[str, str] = {}
    if name is not None:
        changes["name"] = _clean(name, "Name", NAME_MAX, required=True)
    if description is not None:
        changes["description"] = _clean(description, "Description", DESCRIPTION_MAX)
    with connect() as conn, _tx(conn):
        row = _item_row(conn, prop_id)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE prop_items SET {sets}, updated_at = ? WHERE prop_id = ?",
                         (*changes.values(), _now(), prop_id))
            audit(conn, actor, "prop_update", series_id=_series_id((row["library_kind"], row["library_id"])),
                  episode_id=episode_id, details={"prop_id": prop_id, **changes})
    return get_item(prop_id)


def delete_item(prop_id: str, actor: str | None = None, *, episode_id: str | None = None) -> dict:
    """Delete a prop or place and its photos (their files too, unless something else uses them)."""
    with connect() as conn:
        row = _item_row(conn, prop_id)
        photos = conn.execute("SELECT sha256, ext FROM prop_images WHERE prop_id = ?", (prop_id,)).fetchall()
        with _tx(conn):
            conn.execute("DELETE FROM prop_images WHERE prop_id = ?", (prop_id,))
            conn.execute("DELETE FROM prop_items WHERE prop_id = ?", (prop_id,))
            audit(conn, actor, "prop_delete", series_id=_series_id((row["library_kind"], row["library_id"])),
                  episode_id=episode_id, details={"prop_id": prop_id, "kind": row["kind"], "slug": row["slug"],
                                                  "name": row["name"], "photos": len(photos)})
        for photo in photos:
            cast_store._remove_unreferenced_file(conn, photo["sha256"], photo["ext"])
    return {"deleted": prop_id, "photos_removed": len(photos)}


def list_items(library: tuple[str, str]) -> list[dict]:
    """The library's props and places: places first, then props, each in the order they were added."""
    with connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT prop_id FROM prop_items WHERE library_kind = ? AND library_id = ? "
            "ORDER BY kind = 'prop', created_at, slug", library)]
    return [get_item(prop_id) for prop_id in ids]


def get_item(prop_id: str) -> dict:
    with connect() as conn:
        row = _item_row(conn, prop_id)
        photos = conn.execute("SELECT * FROM prop_images WHERE prop_id = ? AND status = 'active' "
                              "ORDER BY is_primary DESC, created_at, image_id", (prop_id,)).fetchall()
    return {**dict(row), "photos": [_photo_view(dict(p)) for p in photos]}


def find_item(library: tuple[str, str], slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT prop_id FROM prop_items WHERE library_kind = ? AND library_id = ? AND slug = ?",
                           (*library, slug)).fetchone()
    return get_item(row[0]) if row else None


def item_library(prop_id: str) -> tuple[str, str] | None:
    with connect() as conn:
        row = conn.execute("SELECT library_kind, library_id FROM prop_items WHERE prop_id = ?", (prop_id,)).fetchone()
    return (row[0], row[1]) if row else None


# ------------------------------------------------------------------------------------- photos

def _photo_view(row: dict) -> dict:
    row["is_primary"] = bool(row["is_primary"])
    if row.get("attestation"):
        try:
            row["attestation"] = json.loads(row["attestation"])
        except ValueError:
            pass
    return row


def _upload_details(prop_id: str, info: dict, filename: str | None, attestation: dict) -> dict:
    return {"prop_id": prop_id, "sha256": info["sha256"], "bytes": info["bytes"], "width": info["width"],
            "height": info["height"], "format": info["format"], "filename": (filename or "")[:200],
            "attestation": attestation}


def _store_photo(conn: sqlite3.Connection, prop_id: str, data: bytes, info: dict, *, actor: str | None,
                 filename: str | None, attestation: dict, primary: bool) -> tuple[str, bool]:
    """(image_id, created). The same bytes for the same item return the existing row."""
    existing = conn.execute("SELECT image_id FROM prop_images WHERE prop_id = ? AND sha256 = ?",
                            (prop_id, info["sha256"])).fetchone()
    if existing:
        return existing["image_id"], False
    cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
    image_id = _new_id("pi")
    conn.execute(
        "INSERT INTO prop_images (image_id, prop_id, status, sha256, ext, mime, width, height, bytes, is_primary, "
        "original_filename, attestation, created_by, created_at) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (image_id, prop_id, info["sha256"], info["ext"], info["mime"], info["width"], info["height"], info["bytes"],
         int(primary), (filename or "")[:200] or None, json.dumps(attestation, sort_keys=True), actor, _now()))
    return image_id, True


def add_photo(prop_id: str, data: bytes, *, filename: str | None, attestation: dict, actor: str | None,
              primary: bool | None = None, episode_id: str | None = None) -> dict:
    """Store a checked reference photo for a prop or place (the same bytes twice are one photo)."""
    info = cast_store.inspect_image(data)
    with connect() as conn:
        with _tx(conn):
            row = _item_row(conn, prop_id)
            count, primaries = conn.execute("SELECT COUNT(*), SUM(is_primary) FROM prop_images WHERE prop_id = ? "
                                            "AND status = 'active'", (prop_id,)).fetchone()
            existing = conn.execute("SELECT image_id FROM prop_images WHERE prop_id = ? AND sha256 = ?",
                                    (prop_id, info["sha256"])).fetchone()
            if not existing and count >= MAX_PHOTOS_PER_ITEM:
                raise CastConflict(f"{row['name']} already has {count} photos (the limit is {MAX_PHOTOS_PER_ITEM}); "
                                   "delete one first")
            image_id, created = _store_photo(conn, prop_id, data, info, actor=actor, filename=filename,
                                             attestation=attestation, primary=False)
            if primary or (not (primaries or 0) and not existing):
                conn.execute("UPDATE prop_images SET is_primary = (image_id = ?) WHERE prop_id = ?", (image_id, prop_id))
            audit(conn, actor, "prop_photo_upload" if created else "prop_photo_upload_duplicate",
                  series_id=_series_id((row["library_kind"], row["library_id"])), episode_id=episode_id,
                  image_id=image_id, details=_upload_details(prop_id, info, filename, attestation))
    cast_store.ensure_thumb(info["sha256"], info["ext"])
    return {**get_photo(image_id), "duplicate": not created}


def get_photo(image_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM prop_images WHERE image_id = ?", (image_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Image {image_id} not found")
    return _photo_view(dict(row))


def photo_library(image_id: str) -> tuple[str, str] | None:
    """The library ((kind, id)) of the photo's prop or place; None when there is no such photo."""
    with connect() as conn:
        row = conn.execute("SELECT p.library_kind, p.library_id FROM prop_images i JOIN prop_items p "
                           "ON p.prop_id = i.prop_id WHERE i.image_id = ?", (image_id,)).fetchone()
    return (row[0], row[1]) if row else None


def set_primary(prop_id: str, image_id: str, actor: str | None = None, episode_id: str | None = None) -> dict:
    with connect() as conn, _tx(conn):
        row = _item_row(conn, prop_id)
        if not conn.execute("SELECT 1 FROM prop_images WHERE image_id = ? AND prop_id = ? AND status = 'active'",
                            (image_id, prop_id)).fetchone():
            raise CastNotFound("That photo is not part of this prop or place")
        conn.execute("UPDATE prop_images SET is_primary = (image_id = ?) WHERE prop_id = ?", (image_id, prop_id))
        audit(conn, actor, "prop_photo_primary", series_id=_series_id((row["library_kind"], row["library_id"])),
              episode_id=episode_id, image_id=image_id, details={"prop_id": prop_id})
    return get_photo(image_id)


def delete_photo(image_id: str, actor: str | None = None, *, prop_id: str | None = None,
                 episode_id: str | None = None) -> dict:
    """Delete a photo (its file too, unless something else uses it); the next photo becomes the main one."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM prop_images WHERE image_id = ?", (image_id,)).fetchone()
        if not row or (prop_id and row["prop_id"] != prop_id):
            raise CastNotFound(f"Image {image_id} not found")
        item = _item_row(conn, row["prop_id"])
        with _tx(conn):
            conn.execute("DELETE FROM prop_images WHERE image_id = ?", (image_id,))
            if row["is_primary"]:
                nxt = conn.execute("SELECT image_id FROM prop_images WHERE prop_id = ? AND status = 'active' "
                                   "ORDER BY created_at, image_id LIMIT 1", (row["prop_id"],)).fetchone()
                if nxt:
                    conn.execute("UPDATE prop_images SET is_primary = 1 WHERE image_id = ?", (nxt[0],))
            audit(conn, actor, "prop_photo_delete", series_id=_series_id((item["library_kind"], item["library_id"])),
                  episode_id=episode_id, image_id=image_id, details={"prop_id": row["prop_id"], "sha256": row["sha256"]})
        removed = cast_store._remove_unreferenced_file(conn, row["sha256"], row["ext"])
    return {"deleted": image_id, "file_removed": removed}


def photo_path(photo: dict):
    return cast_store.image_file(photo["sha256"], photo["ext"])


def reference_photos(item: dict) -> list[dict]:
    """The item's photos the planner sends, main photo first (every photo is a creator upload)."""
    return [p for p in item.get("photos", []) if p.get("status") == "active"]
