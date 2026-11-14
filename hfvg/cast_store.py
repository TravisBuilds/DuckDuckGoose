"""Cast library store: series, characters, character versions (outfits) and their reference images.

Rows live in the studio DB (tables from the cast library migration, hfvg.migrations); image files
live under :func:`cast_root` (``CAST_STORAGE_DIR``, default ``<DATA_DIR>/cast``), content-addressed
by sha256 and written once (never overwritten). Nothing here is served statically: the API streams
files to authenticated studio members only.

Libraries: an episode linked to a series uses the series library (shared by every episode of the
series); an episode without a series has its own library. Character ids in shots.json are the
character's ``slug`` (unique per library).

Locked versions (approved character sheets) are immutable: no uploads, no sheet changes, no image
deletion; the lock records a content hash over the character snapshot and the sheet image hashes,
and :func:`verify_lock` re-checks it (and the files on disk) before a locked sheet is used.

Sync sqlite3: the planner runs inside activities and the API calls these through a worker thread.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from hfvg.cast_prompt import SLUG_RE, character_clause, slugify
from hfvg.genre_presets import Gate
from hfvg.paths import data_dir

ALLOWED_FORMATS = {"PNG": ("png", "image/png"), "JPEG": ("jpg", "image/jpeg"), "WEBP": ("webp", "image/webp")}
DEFAULT_MAX_BYTES = 15 * 1024 * 1024
DEFAULT_MIN_PX = 512
MAX_PIXELS = 40_000_000
MAX_UPLOADS_PER_VERSION = 12
MAX_CHARACTERS_PER_LIBRARY = 40
THUMB_PX = 384
NAME_MAX = 60
ROLE_MAX = 80
DESCRIPTION_MAX = 600
OUTFIT_MAX = 300
LABEL_MAX = 60

# Rights attestation for every upload: the creator made the image or owns the rights to use it.
RIGHTS_VALUES = ("original", "owned")
# Images of a real person need the person's consent (the wizard's likeness-consent concept).
LIKENESS_CONSENT = Gate.LIKENESS_CONSENT.value

MASTER_ROLE = "master"
OPTION_ROLE = "master_option"
UPLOAD_ROLE = "upload"
TURNAROUND_ROLES = ("turnaround_front", "turnaround_three_quarter", "turnaround_side", "turnaround_back")
HEAD_ROLE = "head_sheet"
SHEET_ROLES = (MASTER_ROLE, *TURNAROUND_ROLES, HEAD_ROLE)
ROLES = (UPLOAD_ROLE, OPTION_ROLE, *SHEET_ROLES)
# Identity refs of a locked version, most useful first (the planner keeps this order).
IDENTITY_ORDER = (MASTER_ROLE, HEAD_ROLE, "turnaround_three_quarter", "turnaround_side", "turnaround_back",
                  "turnaround_front")
SHEET_STAGES = ("none", "options_ready", "master_picked", "turnaround_ready")


class CastError(ValueError):
    """A cast library request that cannot be done (status: the HTTP status the API answers)."""
    status = 400


class CastNotFound(CastError):
    status = 404


class CastConflict(CastError):
    status = 409


class CastForbidden(CastError):
    status = 403


class UploadTooLarge(CastError):
    status = 413


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def db_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/studio.db")


def cast_root() -> Path:
    value = os.getenv("CAST_STORAGE_DIR", "").strip()
    return Path(value) if value else data_dir() / "cast"


def max_upload_bytes() -> int:
    return _env_int("CAST_UPLOAD_MAX_BYTES", DEFAULT_MAX_BYTES)


def min_image_px() -> int:
    return _env_int("CAST_MIN_IMAGE_PX", DEFAULT_MIN_PX)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise CastError(f"{name} must be a whole number, got {raw!r}") from e
    if value <= 0:
        raise CastError(f"{name} must be positive, got {value}")
    return value


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    """A connection with the cast tables present (the migration runs if this DB never had it)."""
    from hfvg.migrations import apply_cast_library_sync
    target = path or db_path()
    if target not in (":memory:",) and not target.startswith("file:"):
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30, isolation_level=None, uri=target.startswith("file:"))
    conn.row_factory = sqlite3.Row
    try:
        apply_cast_library_sync(conn)
        yield conn
    finally:
        conn.close()


@contextmanager
def _tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def audit(conn: sqlite3.Connection, actor: str | None, action: str, *, series_id: str | None = None,
          episode_id: str | None = None, character_id: str | None = None, image_id: str | None = None,
          details: dict | str | None = None) -> None:
    text = details if isinstance(details, str) or details is None else json.dumps(details, sort_keys=True)
    conn.execute(
        "INSERT INTO cast_audit_log (timestamp, actor, action, series_id, episode_id, character_id, image_id, "
        "details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (_now(), actor, action, series_id, episode_id, character_id, image_id, text))


def log_event(actor: str | None, action: str, **fields: Any) -> None:
    """One audit row on its own (events the API decides, such as a sheet request)."""
    with connect() as conn, _tx(conn):
        audit(conn, actor, action, **fields)


def list_audit(*, character_id: str | None = None, episode_id: str | None = None, limit: int = 100) -> list[dict]:
    clauses, params = [], []
    if character_id:
        clauses.append("character_id = ?")
        params.append(character_id)
    if episode_id:
        clauses.append("episode_id = ?")
        params.append(episode_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM cast_audit_log {where} ORDER BY id DESC LIMIT ?",
                            (*params, max(1, min(limit, 500)))).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------------------------- text fields

def _clean(value: Any, field: str, limit: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CastError(f"{field} must be text")
    text = " ".join(value.split())
    if required and not text:
        raise CastError(f"{field} is required")
    if len(text) > limit:
        raise CastError(f"{field} is {len(text)} characters; keep it under {limit}")
    return text


# ------------------------------------------------------------------------------------- series

def create_series(name: str, owner_user_id: str | None, actor: str | None = None) -> dict:
    name = _clean(name, "Series name", NAME_MAX, required=True)
    sid = _new_id("sr")
    now = _now()
    with connect() as conn, _tx(conn):
        conn.execute("INSERT INTO series (series_id, name, owner_user_id, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?)", (sid, name, owner_user_id, now, now))
        audit(conn, actor, "series_create", series_id=sid, details={"name": name})
    return get_series(sid)


def get_series(series_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM series WHERE series_id = ?", (series_id,)).fetchone()
        if not row:
            raise CastNotFound(f"Series {series_id} not found")
        episodes = [r[0] for r in conn.execute(
            "SELECT episode_id FROM episode_series WHERE series_id = ? ORDER BY episode_id", (series_id,))]
    return {**dict(row), "episodes": episodes}


def list_series() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT s.*, (SELECT COUNT(*) FROM episode_series e WHERE e.series_id = s.series_id) "
                            "AS episode_count FROM series s ORDER BY s.name COLLATE NOCASE").fetchall()
    return [dict(r) for r in rows]


def episode_series_id(episode_id: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT series_id FROM episode_series WHERE episode_id = ?", (episode_id,)).fetchone()
    return row[0] if row else None


def library_for_episode(episode_id: str) -> tuple[str, str]:
    """("series", series_id) when the episode belongs to a series, else ("episode", episode_id)."""
    sid = episode_series_id(episode_id)
    return ("series", sid) if sid else ("episode", episode_id)


def link_episode(episode_id: str, series_id: str | None, actor: str | None = None) -> dict:
    """Put the episode in a series (its own characters move into the series library) or take it out
    (series_id None; the series keeps its characters). Refused when a moved character's id is
    already used in the series."""
    with connect() as conn, _tx(conn):
        current = conn.execute("SELECT series_id FROM episode_series WHERE episode_id = ?", (episode_id,)).fetchone()
        if series_id is None:
            if current:
                conn.execute("DELETE FROM episode_series WHERE episode_id = ?", (episode_id,))
                audit(conn, actor, "series_unlink", series_id=current[0], episode_id=episode_id)
            return {"episode_id": episode_id, "series_id": None, "moved_characters": []}
        if not conn.execute("SELECT 1 FROM series WHERE series_id = ?", (series_id,)).fetchone():
            raise CastNotFound(f"Series {series_id} not found")
        if current and current[0] == series_id:
            return {"episode_id": episode_id, "series_id": series_id, "moved_characters": []}
        own = conn.execute("SELECT character_id, slug FROM cast_characters WHERE library_kind = 'episode' "
                           "AND library_id = ?", (episode_id,)).fetchall()
        taken = {r[0] for r in conn.execute("SELECT slug FROM cast_characters WHERE library_kind = 'series' "
                                            "AND library_id = ?", (series_id,))}
        clash = sorted(r["slug"] for r in own if r["slug"] in taken)
        if clash:
            raise CastConflict(f"The series already has characters with the ids {clash}; delete this "
                               "episode's copies (and use the series' characters) before adding it to the series")
        moved = [r["slug"] for r in own]
        conn.execute("UPDATE cast_characters SET library_kind = 'series', library_id = ?, updated_at = ? "
                     "WHERE library_kind = 'episode' AND library_id = ?", (series_id, _now(), episode_id))
        conn.execute("INSERT OR REPLACE INTO episode_series (episode_id, series_id, linked_at, linked_by) "
                     "VALUES (?, ?, ?, ?)", (episode_id, series_id, _now(), actor))
        audit(conn, actor, "series_link", series_id=series_id, episode_id=episode_id,
              details={"moved_characters": moved, "previous_series": current[0] if current else None})
    return {"episode_id": episode_id, "series_id": series_id, "moved_characters": moved}


# ------------------------------------------------------------------------------------- characters

def _character_row(conn, character_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cast_characters WHERE character_id = ?", (character_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Character {character_id} not found")
    return row


def _version_row(conn, character_id: str, version: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cast_versions WHERE character_id = ? AND version = ?",
                       (character_id, version)).fetchone()
    if not row:
        raise CastNotFound(f"Version {version} of character {character_id} not found")
    return row


def _unique_slug(conn, kind: str, library_id: str, wanted: str) -> str:
    taken = {r[0] for r in conn.execute(
        "SELECT slug FROM cast_characters WHERE library_kind = ? AND library_id = ?", (kind, library_id))}
    if wanted not in taken:
        return wanted
    for n in range(2, 100):
        candidate = f"{wanted[:29]}-{n}"
        if candidate not in taken:
            return candidate
    raise CastConflict("Too many characters with this name")


def create_character(library: tuple[str, str], name: str, role: str = "", description: str = "",
                     slug: str | None = None, actor: str | None = None, *, episode_id: str | None = None,
                     outfit: str = "") -> dict:
    """A new character (with version 1) in the library. An explicit slug that is taken is refused;
    a slug made from the name gets a number when taken."""
    kind, library_id = library
    name = _clean(name, "Name", NAME_MAX, required=True)
    role = _clean(role, "Role", ROLE_MAX)
    description = _clean(description, "Description", DESCRIPTION_MAX)
    outfit = _clean(outfit, "Outfit", OUTFIT_MAX)
    if slug is not None:
        slug = str(slug).strip().lower()
        if not SLUG_RE.match(slug):
            raise CastError(f"Character id {slug!r} must be lower-case letters, digits and dashes (max 32)")
    cid = _new_id("ch")
    now = _now()
    with connect() as conn, _tx(conn):
        count = conn.execute("SELECT COUNT(*) FROM cast_characters WHERE library_kind = ? AND library_id = ?",
                             (kind, library_id)).fetchone()[0]
        if count >= MAX_CHARACTERS_PER_LIBRARY:
            raise CastConflict(f"This cast already has {count} characters (the limit is {MAX_CHARACTERS_PER_LIBRARY})")
        if slug is not None:
            if conn.execute("SELECT 1 FROM cast_characters WHERE library_kind = ? AND library_id = ? AND slug = ?",
                            (kind, library_id, slug)).fetchone():
                raise CastConflict(f"A character with the id {slug!r} already exists in this cast")
        else:
            slug = _unique_slug(conn, kind, library_id, slugify(name))
        conn.execute("INSERT INTO cast_characters (character_id, library_kind, library_id, slug, name, role, "
                     "description, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (cid, kind, library_id, slug, name, role, description, actor, now, now))
        conn.execute("INSERT INTO cast_versions (character_id, version, label, outfit, created_by, created_at, "
                     "updated_at) VALUES (?, 1, 'Default', ?, ?, ?, ?)", (cid, outfit, actor, now, now))
        audit(conn, actor, "character_create", episode_id=episode_id, character_id=cid,
              series_id=library_id if kind == "series" else None,
              details={"slug": slug, "name": name, "role": role})
    return get_character(cid)


def find_character(library: tuple[str, str], slug: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT character_id FROM cast_characters WHERE library_kind = ? AND library_id = ? "
                           "AND slug = ?", (library[0], library[1], slug)).fetchone()
    return get_character(row[0]) if row else None


def update_character(character_id: str, *, name: str | None = None, role: str | None = None,
                     description: str | None = None, actor: str | None = None,
                     episode_id: str | None = None) -> dict:
    """Edit name, role or description. Locked versions keep the description they were locked with."""
    changes: dict[str, str] = {}
    if name is not None:
        changes["name"] = _clean(name, "Name", NAME_MAX, required=True)
    if role is not None:
        changes["role"] = _clean(role, "Role", ROLE_MAX)
    if description is not None:
        changes["description"] = _clean(description, "Description", DESCRIPTION_MAX)
    with connect() as conn, _tx(conn):
        _character_row(conn, character_id)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE cast_characters SET {sets}, updated_at = ? WHERE character_id = ?",
                         (*changes.values(), _now(), character_id))
            audit(conn, actor, "character_update", episode_id=episode_id, character_id=character_id,
                  details=changes)
    return get_character(character_id)


def delete_character(character_id: str, actor: str | None = None, *, force: bool = False, reason: str = "",
                     episode_id: str | None = None) -> dict:
    """Delete a character and its images. A character with a locked version is protected: only an
    admin request with force and a reason removes it (the API decides who may force)."""
    with connect() as conn:
        row = _character_row(conn, character_id)
        locked = [r[0] for r in conn.execute("SELECT version FROM cast_versions WHERE character_id = ? AND locked = 1",
                                             (character_id,))]
        if locked and not force:
            raise CastConflict(f"{row['name']} has locked character sheets (versions {locked}); locked sheets "
                               "are protected and cannot be deleted")
        if locked and not reason.strip():
            raise CastError("A reason is required to remove a character with locked sheets")
        images = conn.execute("SELECT image_id, sha256, ext FROM cast_images WHERE character_id = ?",
                              (character_id,)).fetchall()
        with _tx(conn):
            conn.execute("DELETE FROM cast_images WHERE character_id = ?", (character_id,))
            conn.execute("DELETE FROM cast_versions WHERE character_id = ?", (character_id,))
            conn.execute("DELETE FROM cast_sheet_jobs WHERE character_id = ? AND status IN ('completed', 'failed')",
                         (character_id,))
            conn.execute("DELETE FROM cast_characters WHERE character_id = ?", (character_id,))
            audit(conn, actor, "character_delete_forced" if locked else "character_delete", episode_id=episode_id,
                  character_id=character_id,
                  details={"slug": row["slug"], "name": row["name"], "images": len(images),
                           **({"reason": reason.strip(), "locked_versions": locked} if locked else {})})
        for img in images:
            _remove_unreferenced_file(conn, img["sha256"], img["ext"])
    return {"deleted": character_id, "images_removed": len(images)}


def list_characters(library: tuple[str, str]) -> list[dict]:
    with connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT character_id FROM cast_characters WHERE library_kind = ? AND library_id = ? "
            "ORDER BY created_at, slug", library)]
    return [get_character(cid) for cid in ids]


def get_character(character_id: str) -> dict:
    with connect() as conn:
        row = _character_row(conn, character_id)
        versions = conn.execute("SELECT * FROM cast_versions WHERE character_id = ? ORDER BY version",
                                (character_id,)).fetchall()
        images = conn.execute("SELECT * FROM cast_images WHERE character_id = ? ORDER BY created_at, image_id",
                              (character_id,)).fetchall()
        jobs = conn.execute("SELECT * FROM cast_sheet_jobs WHERE character_id = ? ORDER BY created_at, job_id",
                            (character_id,)).fetchall()
    out = dict(row)
    out["versions"] = []
    for v in versions:
        vd = dict(v)
        vd["locked"] = bool(vd["locked"])
        vd["images"] = [_image_view(dict(i)) for i in images if i["version"] == v["version"]]
        vd["sheet_jobs"] = [dict(j) for j in jobs if j["version"] == v["version"]]
        vd["clause"] = version_clause(out, vd)
        out["versions"].append(vd)
    out["default_version"] = default_version_number(out["versions"])
    return out


def _image_view(row: dict) -> dict:
    row["is_primary"] = bool(row["is_primary"])
    row["placeholder"] = bool(row["placeholder"])
    if row.get("attestation"):
        try:
            row["attestation"] = json.loads(row["attestation"])
        except ValueError:
            pass
    return row


def default_version_number(versions: list[dict]) -> int:
    """The newest locked version, else the lowest version (1)."""
    locked = [v["version"] for v in versions if v.get("locked")]
    if locked:
        return max(locked)
    return min((v["version"] for v in versions), default=1)


def version_clause(character: dict, version: dict) -> str:
    """The prompt sentence for this character version: the locked description once locked."""
    if version.get("locked") and version.get("locked_description"):
        return version["locked_description"]
    return character_clause(character["name"], character.get("description") or "", version.get("outfit") or "")


def create_version(character_id: str, *, label: str = "", outfit: str = "", derive_from_version: int | None = None,
                   actor: str | None = None, episode_id: str | None = None) -> dict:
    """A new version (outfit) of the character. ``derive_from_version`` must be a LOCKED version of the
    same character: its locked master is the identity reference of the new sheet (never scene stills)."""
    label = _clean(label, "Label", LABEL_MAX) or "New outfit"
    outfit = _clean(outfit, "Outfit", OUTFIT_MAX)
    now = _now()
    with connect() as conn, _tx(conn):
        _character_row(conn, character_id)
        if derive_from_version is not None:
            src = _version_row(conn, character_id, int(derive_from_version))
            if not src["locked"]:
                raise CastConflict(f"Version {derive_from_version} is not locked yet; a new outfit is derived "
                                   "from a locked (approved) character sheet")
            if not conn.execute("SELECT 1 FROM cast_images WHERE character_id = ? AND version = ? AND role = ? "
                                "AND status = 'active'", (character_id, int(derive_from_version), MASTER_ROLE)).fetchone():
                raise CastConflict(f"Version {derive_from_version} has no locked master image to derive from")
        n = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM cast_versions WHERE character_id = ?",
                         (character_id,)).fetchone()[0]
        conn.execute("INSERT INTO cast_versions (character_id, version, label, outfit, derived_from_version, "
                     "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (character_id, n, label, outfit, derive_from_version, actor, now, now))
        audit(conn, actor, "version_create", episode_id=episode_id, character_id=character_id,
              details={"version": n, "label": label, "derived_from_version": derive_from_version})
    return get_version(character_id, n)


def get_version(character_id: str, version: int) -> dict:
    character = get_character(character_id)
    for v in character["versions"]:
        if v["version"] == int(version):
            return {**v, "character": {k: character[k] for k in ("character_id", "slug", "name", "role",
                                                                 "description", "library_kind", "library_id")}}
    raise CastNotFound(f"Version {version} of {character['name']} not found")


def update_version(character_id: str, version: int, *, label: str | None = None, outfit: str | None = None,
                   actor: str | None = None, episode_id: str | None = None) -> dict:
    changes: dict[str, str] = {}
    if label is not None:
        changes["label"] = _clean(label, "Label", LABEL_MAX)
    if outfit is not None:
        changes["outfit"] = _clean(outfit, "Outfit", OUTFIT_MAX)
    with connect() as conn, _tx(conn):
        row = _version_row(conn, character_id, version)
        if row["locked"] and changes:
            raise CastConflict(f"Version {version} is locked; create a new version to change it")
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE cast_versions SET {sets}, updated_at = ? WHERE character_id = ? AND version = ?",
                         (*changes.values(), _now(), character_id, version))
            audit(conn, actor, "version_update", episode_id=episode_id, character_id=character_id,
                  details={"version": version, **changes})
    return get_version(character_id, version)


def _require_unlocked(conn, character_id: str, version: int) -> sqlite3.Row:
    row = _version_row(conn, character_id, version)
    if row["locked"]:
        raise CastConflict(f"Version {version} is locked (approved); locked character sheets cannot be changed. "
                           "Create a new version for a new outfit.")
    return row


# ------------------------------------------------------------------------------------- images

def inspect_image(data: bytes, *, max_bytes: int | None = None, min_px: int | None = None) -> dict:
    """Validate an image upload: PNG, JPG or WebP that really decodes, not animated, each side at
    least ``min_px`` and at most ``max_bytes``. Returns format, ext, mime, width, height, bytes, sha256."""
    from PIL import Image, UnidentifiedImageError

    max_bytes = max_upload_bytes() if max_bytes is None else max_bytes
    min_px = min_image_px() if min_px is None else min_px
    if not data:
        raise CastError("The file is empty")
    if len(data) > max_bytes:
        raise UploadTooLarge(f"The image is {len(data) / 1_048_576:.1f} MB; the limit is "
                             f"{max_bytes / 1_048_576:.0f} MB")
    damaged = "This image is damaged or incomplete and cannot be read"
    try:
        probe = Image.open(io.BytesIO(data))
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise CastError("This file is not a readable PNG, JPG or WebP image") from None
    with probe:
        fmt = probe.format
        if fmt not in ALLOWED_FORMATS:
            raise CastError(f"Only PNG, JPG and WebP images are accepted (this file is {fmt or 'another format'})")
        try:
            probe.verify()
        except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
            raise CastError(damaged) from None
    try:
        with Image.open(io.BytesIO(data)) as img:
            if getattr(img, "is_animated", False) and getattr(img, "n_frames", 1) > 1:
                raise CastError("Animated images are not accepted; upload a still image")
            width, height = img.size
            if width * height > MAX_PIXELS:
                raise CastError(f"The image is {width}x{height} px; keep it under {MAX_PIXELS // 1_000_000} megapixels")
            img.load()  # full decode: a truncated or corrupt file fails here
    except CastError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise CastError(damaged) from None
    if min(width, height) < min_px:
        raise CastError(f"The image is {width}x{height} px; each side must be at least {min_px} px")
    ext, mime = ALLOWED_FORMATS[fmt]
    return {"format": fmt, "ext": ext, "mime": mime, "width": width, "height": height, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def validate_attestation(rights: Any, depicts_real_person: Any, likeness_consent: Any, note: Any = "") -> dict:
    """The rights attestation every upload needs. Real people only with the likeness-consent
    attestation (the person agreed to their likeness being used)."""
    def flag(value: Any, field: str) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off", ""):
            return False
        raise CastError(f"{field} must be true or false")

    rights_value = str(rights or "").strip().lower()
    if rights_value not in RIGHTS_VALUES:
        raise CastError("Confirm the image is yours to use: you made it (original) or you own the rights (owned)")
    real = flag(depicts_real_person, "depicts_real_person")
    consent = flag(likeness_consent, LIKENESS_CONSENT)
    if real and not consent:
        raise CastError("Images of a real person need that person's consent: confirm likeness consent, or "
                        "upload an original character instead")
    return {"rights": rights_value, "depicts_real_person": real, LIKENESS_CONSENT: consent,
            "note": _clean(note, "Note", 300), "attested_at": _now()}


def image_file(sha256: str, ext: str) -> Path:
    return cast_root() / "images" / sha256[:2] / f"{sha256}.{ext}"


def thumb_file(sha256: str) -> Path:
    return cast_root() / "thumbs" / f"{sha256}.jpg"


def _write_once(path: Path, data: bytes) -> None:
    """Content-addressed write: an existing file is never overwritten."""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o640)
    try:
        os.link(tmp, path)  # fails if another writer won the race; the content is identical
    except FileExistsError:
        pass
    except OSError:  # a filesystem without hard links: an atomic rename (identical content if raced)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def ensure_thumb(sha256: str, ext: str) -> Path:
    from PIL import Image, ImageOps
    out = thumb_file(sha256)
    if out.exists():
        return out
    with Image.open(image_file(sha256, ext)) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((THUMB_PX, THUMB_PX))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=85)
    _write_once(out, buf.getvalue())
    return out


def _store_image(conn, character_id: str, version: int, data: bytes, info: dict, *, role: str, source: str,
                 actor: str | None, filename: str | None = None, attestation: dict | None = None,
                 job_id: str | None = None, placeholder: bool = False, primary: bool = False) -> tuple[str, bool]:
    """(image_id, created). The same bytes for the same version return the existing row."""
    existing = conn.execute("SELECT image_id, status FROM cast_images WHERE character_id = ? AND version = ? "
                            "AND sha256 = ?", (character_id, version, info["sha256"])).fetchone()
    if existing:
        if existing["status"] != "active":
            conn.execute("UPDATE cast_images SET status = 'active', role = ? WHERE image_id = ?",
                         (role, existing["image_id"]))
        return existing["image_id"], False
    _write_once(image_file(info["sha256"], info["ext"]), data)
    image_id = _new_id("ci")
    conn.execute(
        "INSERT INTO cast_images (image_id, character_id, version, role, status, sha256, ext, mime, width, height, "
        "bytes, is_primary, placeholder, source, job_id, original_filename, attestation, created_by, created_at) "
        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (image_id, character_id, version, role, info["sha256"], info["ext"], info["mime"], info["width"],
         info["height"], info["bytes"], int(primary), int(placeholder), source, job_id,
         (filename or "")[:200] or None, json.dumps(attestation, sort_keys=True) if attestation else None,
         actor, _now()))
    return image_id, True


def add_upload(character_id: str, version: int, data: bytes, *, filename: str | None, attestation: dict,
               actor: str | None, primary: bool | None = None, episode_id: str | None = None) -> dict:
    """Store a validated reference upload for a character version (deduplicated by sha256)."""
    info = inspect_image(data)
    with connect() as conn:
        with _tx(conn):
            _character_row(conn, character_id)
            _require_unlocked(conn, character_id, version)
            uploads = conn.execute("SELECT COUNT(*), SUM(is_primary) FROM cast_images WHERE character_id = ? AND "
                                   "version = ? AND role = ? AND status = 'active'",
                                   (character_id, version, UPLOAD_ROLE)).fetchone()
            count, primaries = uploads[0], uploads[1] or 0
            existing = conn.execute("SELECT image_id FROM cast_images WHERE character_id = ? AND version = ? AND "
                                    "sha256 = ? AND status = 'active'",
                                    (character_id, version, info["sha256"])).fetchone()
            if not existing and count >= MAX_UPLOADS_PER_VERSION:
                raise CastConflict(f"This version already has {count} reference images (the limit is "
                                   f"{MAX_UPLOADS_PER_VERSION}); delete one first")
            make_primary = bool(primary) or (primaries == 0 and not existing)
            image_id, created = _store_image(conn, character_id, version, data, info, role=UPLOAD_ROLE,
                                             source="upload", actor=actor, filename=filename,
                                             attestation=attestation, primary=False)
            if make_primary:
                conn.execute("UPDATE cast_images SET is_primary = (image_id = ?) WHERE character_id = ? AND "
                             "version = ? AND role = ?", (image_id, character_id, version, UPLOAD_ROLE))
            audit(conn, actor, "image_upload" if created else "image_upload_duplicate", episode_id=episode_id,
                  character_id=character_id, image_id=image_id,
                  details={"version": version, "sha256": info["sha256"], "bytes": info["bytes"],
                           "width": info["width"], "height": info["height"], "format": info["format"],
                           "filename": (filename or "")[:200], "attestation": attestation})
    ensure_thumb(info["sha256"], info["ext"])
    return {**get_image(image_id), "duplicate": not created}


def add_generated_image(character_id: str, version: int, data: bytes, *, role: str, job_id: str,
                        placeholder: bool, actor: str | None = None, episode_id: str | None = None) -> dict:
    """Store a generated sheet image (refused once the version is locked). Master options keep
    accumulating; a sheet view replaces the earlier image of the same view."""
    if role not in (OPTION_ROLE, *TURNAROUND_ROLES, HEAD_ROLE, MASTER_ROLE):
        raise CastError(f"Unknown sheet image role {role!r}")
    # paid output is already spent: the same floor as the ref QC (>= 256 px), not the upload minimum
    info = inspect_image(data, max_bytes=max(max_upload_bytes(), 40 * 1024 * 1024), min_px=256)
    with connect() as conn:
        with _tx(conn):
            _character_row(conn, character_id)
            _require_unlocked(conn, character_id, version)
            if role != OPTION_ROLE:
                conn.execute("UPDATE cast_images SET status = 'discarded' WHERE character_id = ? AND version = ? "
                             "AND role = ? AND status = 'active'", (character_id, version, role))
            image_id, _created = _store_image(conn, character_id, version, data, info, role=role, source="generated",
                                              actor=actor, job_id=job_id, placeholder=placeholder)
            _refresh_stage(conn, character_id, version)
            audit(conn, actor, "sheet_image_add", episode_id=episode_id, character_id=character_id,
                  image_id=image_id, details={"version": version, "role": role, "job_id": job_id,
                                              "sha256": info["sha256"], "placeholder": placeholder})
    ensure_thumb(info["sha256"], info["ext"])
    return get_image(image_id)


def get_image(image_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM cast_images WHERE image_id = ?", (image_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Image {image_id} not found")
    return _image_view(dict(row))


def image_path(image: dict) -> Path:
    return image_file(image["sha256"], image["ext"])


def set_primary(character_id: str, version: int, image_id: str, actor: str | None = None,
                episode_id: str | None = None) -> dict:
    with connect() as conn, _tx(conn):
        _require_unlocked(conn, character_id, version)
        row = conn.execute("SELECT role FROM cast_images WHERE image_id = ? AND character_id = ? AND version = ? "
                           "AND status = 'active'", (image_id, character_id, version)).fetchone()
        if not row or row["role"] != UPLOAD_ROLE:
            raise CastNotFound("That upload is not part of this version")
        conn.execute("UPDATE cast_images SET is_primary = (image_id = ?) WHERE character_id = ? AND version = ? "
                     "AND role = ?", (image_id, character_id, version, UPLOAD_ROLE))
        audit(conn, actor, "image_primary", episode_id=episode_id, character_id=character_id, image_id=image_id,
              details={"version": version})
    return get_image(image_id)


def delete_image(image_id: str, actor: str | None = None, *, character_id: str | None = None,
                 episode_id: str | None = None) -> dict:
    """Delete an upload or an unlocked sheet image (its file too, unless another row uses it). Images
    of a locked version are protected."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM cast_images WHERE image_id = ?", (image_id,)).fetchone()
        if not row or (character_id and row["character_id"] != character_id):
            raise CastNotFound(f"Image {image_id} not found")
        with _tx(conn):
            _require_unlocked(conn, row["character_id"], row["version"])
            conn.execute("DELETE FROM cast_images WHERE image_id = ?", (image_id,))
            if row["role"] == UPLOAD_ROLE and row["is_primary"]:
                nxt = conn.execute("SELECT image_id FROM cast_images WHERE character_id = ? AND version = ? AND "
                                   "role = ? AND status = 'active' ORDER BY created_at LIMIT 1",
                                   (row["character_id"], row["version"], UPLOAD_ROLE)).fetchone()
                if nxt:
                    conn.execute("UPDATE cast_images SET is_primary = 1 WHERE image_id = ?", (nxt[0],))
            _refresh_stage(conn, row["character_id"], row["version"])
            audit(conn, actor, "image_delete", episode_id=episode_id, character_id=row["character_id"],
                  image_id=image_id, details={"version": row["version"], "role": row["role"],
                                              "sha256": row["sha256"]})
        removed = _remove_unreferenced_file(conn, row["sha256"], row["ext"])
    return {"deleted": image_id, "file_removed": removed}


def _remove_unreferenced_file(conn, sha256: str, ext: str) -> bool:
    """Remove the file, its thumbnail and every composite built from it once no row uses the image."""
    if conn.execute("SELECT 1 FROM cast_images WHERE sha256 = ?", (sha256,)).fetchone():
        return False
    image_file(sha256, ext).unlink(missing_ok=True)
    thumb_file(sha256).unlink(missing_ok=True)
    comp_dir = cast_root() / "composites"
    if comp_dir.is_dir():
        for meta in comp_dir.glob("*.json"):
            try:
                sources = json.loads(meta.read_text(encoding="utf-8")).get("sources", [])
            except (OSError, ValueError):
                continue
            if sha256 in sources:
                meta.with_suffix(".png").unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
    return True


# ------------------------------------------------------------------------------------- sheets

def _refresh_stage(conn, character_id: str, version: int) -> str:
    """The version's sheet stage from its active images (none -> options_ready -> master_picked ->
    turnaround_ready). Locked versions keep their stage."""
    row = _version_row(conn, character_id, version)
    if row["locked"]:
        return row["sheet_stage"]
    roles = {r[0] for r in conn.execute("SELECT role FROM cast_images WHERE character_id = ? AND version = ? AND "
                                        "status = 'active'", (character_id, version))}
    if MASTER_ROLE in roles and all(r in roles for r in (*TURNAROUND_ROLES, HEAD_ROLE)):
        stage = "turnaround_ready"
    elif MASTER_ROLE in roles:
        stage = "master_picked"
    elif OPTION_ROLE in roles:
        stage = "options_ready"
    else:
        stage = "none"
    conn.execute("UPDATE cast_versions SET sheet_stage = ?, updated_at = ? WHERE character_id = ? AND version = ?",
                 (stage, _now(), character_id, version))
    return stage


def pick_master(character_id: str, version: int, image_id: str, actor: str | None = None,
                episode_id: str | None = None) -> dict:
    """Make one master option the version's master (front full-body). The turnaround is built from it."""
    with connect() as conn, _tx(conn):
        _require_unlocked(conn, character_id, version)
        row = conn.execute("SELECT * FROM cast_images WHERE image_id = ? AND character_id = ? AND version = ? "
                           "AND status = 'active'", (image_id, character_id, version)).fetchone()
        if not row or row["role"] not in (OPTION_ROLE, MASTER_ROLE):
            raise CastNotFound("That image is not one of this version's master options")
        conn.execute("UPDATE cast_images SET role = ? WHERE character_id = ? AND version = ? AND role = ? AND "
                     "status = 'active'", (OPTION_ROLE, character_id, version, MASTER_ROLE))
        conn.execute("UPDATE cast_images SET role = ? WHERE image_id = ?", (MASTER_ROLE, image_id))
        # a new master makes the old turnaround stale
        conn.execute("UPDATE cast_images SET status = 'discarded' WHERE character_id = ? AND version = ? AND "
                     "role IN (?, ?, ?, ?, ?) AND status = 'active'",
                     (character_id, version, *TURNAROUND_ROLES, HEAD_ROLE))
        stage = _refresh_stage(conn, character_id, version)
        audit(conn, actor, "sheet_pick_master", episode_id=episode_id, character_id=character_id,
              image_id=image_id, details={"version": version, "sha256": row["sha256"]})
    return {"version": version, "master_image_id": image_id, "sheet_stage": stage}


def reject_sheet(character_id: str, version: int, stage: str, actor: str | None = None,
                 episode_id: str | None = None) -> dict:
    """Throw away the master options ("options"), the turnaround ("turnaround") or the whole sheet
    ("all") before lock, so it can be generated again."""
    if stage == "options":
        roles = (OPTION_ROLE,)
    elif stage == "turnaround":
        roles = (*TURNAROUND_ROLES, HEAD_ROLE)
    elif stage == "all":
        roles = (OPTION_ROLE, MASTER_ROLE, *TURNAROUND_ROLES, HEAD_ROLE)
    else:
        raise CastError("stage must be options, turnaround or all")
    with connect() as conn, _tx(conn):
        _require_unlocked(conn, character_id, version)
        marks = ",".join("?" for _ in roles)
        cur = conn.execute(f"UPDATE cast_images SET status = 'discarded' WHERE character_id = ? AND version = ? AND "
                           f"status = 'active' AND role IN ({marks})", (character_id, version, *roles))
        new_stage = _refresh_stage(conn, character_id, version)
        audit(conn, actor, "sheet_reject", episode_id=episode_id, character_id=character_id,
              details={"version": version, "stage": stage, "images": cur.rowcount})
    return {"version": version, "discarded": cur.rowcount, "sheet_stage": new_stage}


def _lock_snapshot(character: sqlite3.Row | dict, version: sqlite3.Row | dict, images: list) -> dict:
    """What a lock covers: the character id, the version's text (the locked description carries the
    name and description as locked) and the sheet image hashes. Renaming the character later does
    not touch a locked version."""
    by_role = {i["role"]: i["sha256"] for i in images}
    return {
        "character_id": character["character_id"],
        "slug": character["slug"],
        "version": version["version"],
        "label": version["label"],
        "outfit": version["outfit"],
        "derived_from_version": version["derived_from_version"],
        "locked_description": version["locked_description"],
        "images": [{"role": r, "sha256": by_role[r]} for r in IDENTITY_ORDER if r in by_role],
    }


def lock_hash(snapshot: dict) -> str:
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def approve_version(character_id: str, version: int, actor: str | None = None, *,
                    allow_placeholder: bool = False, episode_id: str | None = None) -> dict:
    """Lock the version: the master and the full turnaround (front, 3/4, side, back, head sheet) must be
    there. Records the locked description and a content hash; from now on nothing in the version
    changes, and its sheet images are the identity refs of every still with this character."""
    with connect() as conn, _tx(conn):
        character = _character_row(conn, character_id)
        row = _require_unlocked(conn, character_id, version)
        images = conn.execute("SELECT * FROM cast_images WHERE character_id = ? AND version = ? AND status = 'active' "
                              "AND role IN (?, ?, ?, ?, ?, ?)",
                              (character_id, version, *SHEET_ROLES)).fetchall()
        have = {i["role"] for i in images}
        missing = [r for r in SHEET_ROLES if r not in have]
        if missing:
            raise CastConflict("The character sheet is not complete yet: " + ", ".join(_role_label(r) for r in missing))
        if not allow_placeholder and any(i["placeholder"] for i in images):
            raise CastConflict("This sheet has dry-run placeholder images; generate the real sheet before locking")
        locked_description = character_clause(character["name"], character["description"], row["outfit"])
        now = _now()
        conn.execute("UPDATE cast_versions SET locked_description = ? WHERE character_id = ? AND version = ?",
                     (locked_description, character_id, version))
        row = _version_row(conn, character_id, version)
        snapshot = _lock_snapshot(character, row, images)
        digest = lock_hash(snapshot)
        conn.execute("UPDATE cast_versions SET locked = 1, locked_at = ?, locked_by = ?, lock_hash = ?, "
                     "updated_at = ? WHERE character_id = ? AND version = ?",
                     (now, actor, digest, now, character_id, version))
        audit(conn, actor, "version_lock", episode_id=episode_id, character_id=character_id,
              details={"version": version, "lock_hash": digest, "images": snapshot["images"]})
    return get_version(character_id, version)


def _role_label(role: str) -> str:
    return {"master": "master (front full body)", "turnaround_front": "front view",
            "turnaround_three_quarter": "3/4 view", "turnaround_side": "side view",
            "turnaround_back": "back view", "head_sheet": "head sheet"}.get(role, role)


def verify_lock(character_id: str, version: int) -> list[str]:
    """Problems with a locked version: hash mismatch (rows changed) or files changed/missing on disk."""
    with connect() as conn:
        character = _character_row(conn, character_id)
        row = _version_row(conn, character_id, version)
        if not row["locked"]:
            return []
        images = conn.execute("SELECT * FROM cast_images WHERE character_id = ? AND version = ? AND status = 'active' "
                              "AND role IN (?, ?, ?, ?, ?, ?)", (character_id, version, *SHEET_ROLES)).fetchall()
    problems = []
    if lock_hash(_lock_snapshot(character, row, images)) != row["lock_hash"]:
        problems.append(f"{character['name']} v{version}: the locked sheet no longer matches its lock hash")
    for img in images:
        path = image_file(img["sha256"], img["ext"])
        if not path.is_file():
            problems.append(f"{character['name']} v{version}: locked {img['role']} image is missing on disk")
        elif hashlib.sha256(path.read_bytes()).hexdigest() != img["sha256"]:
            problems.append(f"{character['name']} v{version}: locked {img['role']} image changed on disk")
    return problems


# ------------------------------------------------------------------------------------- sheet jobs

def record_sheet_job(job: dict) -> None:
    now = _now()
    with connect() as conn, _tx(conn):
        conn.execute(
            "INSERT OR IGNORE INTO cast_sheet_jobs (job_id, character_id, version, stage, view, attempt, episode_id, "
            "workflow_id, status, estimated_usd_micros, requested_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (job["job_id"], job["character_id"], job["version"], job["stage"], job["view"], job["attempt"],
             job["episode_id"], job.get("workflow_id"), job.get("estimated_usd_micros"), job.get("requested_by"),
             now, now))


def update_sheet_job(job_id: str, *, status: str, image_id: str | None = None, error: str | None = None) -> None:
    with connect() as conn, _tx(conn):
        conn.execute("UPDATE cast_sheet_jobs SET status = ?, image_id = COALESCE(?, image_id), error = ?, "
                     "updated_at = ? WHERE job_id = ?", (status, image_id, (error or None) and error[:500],
                                                         _now(), job_id))


def sheet_jobs(character_id: str, version: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM cast_sheet_jobs WHERE character_id = ? AND version = ? "
                            "ORDER BY created_at, job_id", (character_id, version)).fetchall()
    return [dict(r) for r in rows]


def next_attempt(character_id: str, version: int, view: str) -> int:
    """The next attempt number of one sheet view (job ids are never reused, even after a reject)."""
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(attempt), 0) FROM cast_sheet_jobs WHERE character_id = ? AND "
                           "version = ? AND view = ?", (character_id, version, view)).fetchone()
    return int(row[0]) + 1


def active_sheet_jobs(character_id: str, version: int) -> list[dict]:
    return [j for j in sheet_jobs(character_id, version) if j["status"] in ("queued", "running")]


def paid_job_count(character_id: str, version: int) -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM cast_sheet_jobs WHERE character_id = ? AND version = ? AND "
                           "status != 'refused'", (character_id, version)).fetchone()
    return int(row[0])


# ------------------------------------------------------------------------------------- identity refs

def identity_images(character: dict, version: dict, *, live: bool) -> tuple[list[dict], list[str]]:
    """(images, notes): the identity refs of one character version, most useful first.

    Locked: its sheet images (master, head sheet, 3/4, side, back, front). Not locked: the active
    reference uploads (primary first). Live plans never use dry-run placeholder images."""
    notes: list[str] = []
    images = [i for i in version.get("images") or [] if i.get("status") == "active"]
    if version.get("locked"):
        chosen = sorted((i for i in images if i["role"] in SHEET_ROLES), key=lambda i: IDENTITY_ORDER.index(i["role"]))
    else:
        chosen = sorted((i for i in images if i["role"] == UPLOAD_ROLE),
                        key=lambda i: (not i.get("is_primary"), i.get("created_at") or "", i["image_id"]))
        if not chosen and any(i["role"] in SHEET_ROLES for i in images):
            notes.append(f"{character['name']}: the character sheet is not approved yet, so it is not used")
    if live and any(i.get("placeholder") for i in chosen):
        notes.append(f"{character['name']}: dry-run placeholder images are not used in a live run")
        chosen = [i for i in chosen if not i.get("placeholder")]
    present = []
    for img in chosen:
        if image_file(img["sha256"], img["ext"]).is_file():
            present.append(img)
        else:
            notes.append(f"{character['name']}: reference image {img['image_id']} is missing on disk")
    return present, notes
