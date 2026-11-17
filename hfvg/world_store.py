"""Universes: a series and everything that keeps its episodes consistent (docs/WORLDS.md).

A Universe is a series (hfvg.cast_store) with a profile: logline, story bible, style notes, the
wizard card new episodes default to, a cover picture and the narrator voice. Its characters, props
and places are the series library of the cast and props stores; nothing here copies them. Each
character can keep a saved voice. The creation flow keeps the creator's references (notes and
photos), the mood board versions made from them (with the feedback a redo answered and the
approval) and which approved board the Universe bible was written from.

Tables from the universe migration (hfvg.migrations.UNIVERSE_MIGRATION). A series without a
profile row reads as the empty profile. Reference photos and mood board images share the cast
library's private image store (``cast_store.image_file``), never served statically.

What episodes use (``used_in``) is read from each episode's shots.json, so it counts what the
episode was planned with; a missing or unreadable file counts as nothing.

Sync sqlite3 like the cast store; the API calls these through a worker thread.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from typing import Any

from hfvg import cast_store, paths, prop_store
from hfvg.cast_store import CastConflict, CastError, CastNotFound, _clean, _new_id, _now, _tx, audit, connect

LOGLINE_MAX = 200
BIBLE_MAX = 8000
STYLE_NOTES_MAX = 1500
NOTE_MAX = 2000
CAPTION_MAX = 200
FEEDBACK_MAX = 600
MAX_NOTES = 40
MAX_REFERENCE_PHOTOS = 12
NOTE_TOPICS = ("premise", "characters", "places", "tone")
NOTE_LABELS = {"premise": "Premise", "characters": "Characters", "places": "Places", "tone": "Tone"}

BOARD_IMAGES = 4
BOARD_PX = 768
BOARD_PROMPT_MAX = 2400
BOARD_BEING_MADE = "A mood board is being made; wait for it to finish"
BOARD_STOPPED = "The studio's worker stopped before every picture was made."
BOARD_JOB_STATUSES = ("queued", "running", "completed", "failed", "refused", "skipped", "pending_reconcile")

VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VOICE_NAME_MAX = 80
VOICE_DESCRIPTION_MAX = 300
PREVIEW_URL_MAX = 20_000

BIBLE_CHARACTERS_MAX = 8
BIBLE_PLACES_MAX = 6
BIBLE_PROPS_MAX = 8

CONTEXT_CHARACTERS = 12
CONTEXT_ITEMS = 12

_UNSET: Any = object()


# ------------------------------------------------------------------------------------- text

def _clean_block(value: Any, field: str, limit: int) -> str:
    """Text that keeps its paragraphs (a story bible): line ends normalised, trailing spaces and
    runs of blank lines removed, at most ``limit`` characters."""
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CastError(f"{field} must be text")
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > limit:
        raise CastError(f"{field} is {len(text)} characters; keep it under {limit}")
    return text


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# ------------------------------------------------------------------------------------- profile

def _series_row(conn, series_id: str):
    row = conn.execute("SELECT * FROM series WHERE series_id = ?", (series_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Series {series_id} not found")
    return row


def _profile_view(series_id: str, row) -> dict:
    out = {"series_id": series_id, "logline": "", "bible": "", "style_notes": "", "preset_id": None,
           "cover_image_id": None, "narrator_voice": None, "bible_board_id": None, "bible_generated_at": None,
           "updated_by": None, "created_at": None, "updated_at": None}
    if row is None:
        return out
    row = dict(row)
    for key in ("logline", "bible", "style_notes", "preset_id", "cover_image_id", "bible_board_id",
                "bible_generated_at", "updated_by", "created_at", "updated_at"):
        out[key] = row.get(key) if row.get(key) is not None else out[key]
    if row.get("narrator_voice_id"):
        out["narrator_voice"] = {"voice_id": row["narrator_voice_id"], "name": row.get("narrator_voice_name") or "",
                                 "description": row.get("narrator_voice_description") or "",
                                 "preview_url": row.get("narrator_preview_url")}
    return out


def get_profile(series_id: str) -> dict:
    """The Universe profile of a series (the empty profile when it has no row yet)."""
    with connect() as conn:
        _series_row(conn, series_id)
        row = conn.execute("SELECT * FROM series_profiles WHERE series_id = ?", (series_id,)).fetchone()
    return _profile_view(series_id, row)


def _ensure_profile(conn, series_id: str, actor: str | None) -> None:
    now = _now()
    conn.execute("INSERT OR IGNORE INTO series_profiles (series_id, updated_by, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?)", (series_id, actor, now, now))


def _preset_id(value: Any) -> str | None:
    from hfvg.genre_presets import get_preset
    text = str(value or "").strip()
    if not text:
        return None
    if get_preset(text) is None:
        raise CastError(f"Unknown video type {text!r}: pick one of the cards")
    return text


def _image_in_universe(conn, series_id: str, image_id: str) -> bool:
    """Whether the picture belongs to this Universe: a cast picture or a prop/place photo of its
    library, a reference photo or a mood board image."""
    queries = {
        "ci_": "SELECT 1 FROM cast_images i JOIN cast_characters c ON c.character_id = i.character_id "
               "WHERE i.image_id = ? AND i.status = 'active' AND c.library_kind = 'series' AND c.library_id = ?",
        "pi_": "SELECT 1 FROM prop_images i JOIN prop_items p ON p.prop_id = i.prop_id WHERE i.image_id = ? "
               "AND i.status = 'active' AND p.library_kind = 'series' AND p.library_id = ?",
        "ui_": "SELECT 1 FROM universe_photos WHERE image_id = ? AND status = 'active' AND series_id = ?",
        "bi_": "SELECT 1 FROM universe_board_images i JOIN universe_boards b ON b.board_id = i.board_id "
               "WHERE i.image_id = ? AND b.series_id = ?",
    }
    sql = queries.get(image_id[:3])
    return bool(sql) and conn.execute(sql, (image_id, series_id)).fetchone() is not None


def update_profile(series_id: str, *, logline: Any = _UNSET, bible: Any = _UNSET, style_notes: Any = _UNSET,
                   preset_id: Any = _UNSET, cover_image_id: Any = _UNSET, actor: str | None = None) -> dict:
    """Change the given fields of the profile (the row is made on first change)."""
    changes: dict[str, Any] = {}
    if logline is not _UNSET:
        changes["logline"] = _clean(logline, "Logline", LOGLINE_MAX)
    if bible is not _UNSET:
        changes["bible"] = _clean_block(bible, "Story bible", BIBLE_MAX)
    if style_notes is not _UNSET:
        changes["style_notes"] = _clean_block(style_notes, "Style notes", STYLE_NOTES_MAX)
    if preset_id is not _UNSET:
        changes["preset_id"] = _preset_id(preset_id)
    with connect() as conn, _tx(conn):
        _series_row(conn, series_id)
        if cover_image_id is not _UNSET:
            cover = str(cover_image_id or "").strip() or None
            if cover is not None and not _image_in_universe(conn, series_id, cover):
                raise CastNotFound("That picture is not part of this Universe")
            changes["cover_image_id"] = cover
        _ensure_profile(conn, series_id, actor)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE series_profiles SET {sets}, updated_by = ?, updated_at = ? WHERE series_id = ?",
                         (*changes.values(), actor, _now(), series_id))
            audit(conn, actor, "universe_profile_update", series_id=series_id,
                  details={k: (len(v) if k in ("bible", "style_notes") and v else v) for k, v in changes.items()})
    return get_profile(series_id)


def create_universe(name: str, owner_user_id: str | None, *, logline: str = "", preset_id: str | None = None,
                    actor: str | None = None) -> dict:
    """A new series owned by the caller, with its profile. Everything is checked before anything is
    written; nothing here generates or spends."""
    logline = _clean(logline, "Logline", LOGLINE_MAX)
    preset = _preset_id(preset_id)
    series = cast_store.create_series(name, owner_user_id, actor)
    sid = series["series_id"]
    with connect() as conn, _tx(conn):
        _ensure_profile(conn, sid, actor)
        conn.execute("UPDATE series_profiles SET logline = ?, preset_id = ?, updated_by = ? WHERE series_id = ?",
                     (logline, preset, actor, sid))
        audit(conn, actor, "universe_create", series_id=sid, details={"logline": logline, "preset_id": preset})
    return {"series": series, "profile": get_profile(sid)}


# ------------------------------------------------------------------------------------- voices

def _voice_fields(voice_id: Any, name: Any, description: Any, preview_url: Any) -> dict:
    vid = str(voice_id or "").strip()
    if not VOICE_ID_RE.match(vid):
        raise CastError("Pick a voice first (Find a voice), then save it")
    url = str(preview_url or "").strip() or None
    if url is not None:
        if len(url) > PREVIEW_URL_MAX or not (url.startswith("https://") or url.startswith("data:audio/")):
            raise CastError("The voice preview must be the link the voice pick returned")
    return {"voice_id": vid, "voice_name": _clean(name, "Voice name", VOICE_NAME_MAX),
            "description": _clean(description, "Voice description", VOICE_DESCRIPTION_MAX), "preview_url": url}


def _voice_view(row) -> dict | None:
    if row is None:
        return None
    return {"voice_id": row["voice_id"], "name": row["voice_name"], "description": row["description"],
            "preview_url": row["preview_url"], "chosen_by": row["chosen_by"], "chosen_at": row["chosen_at"]}


def set_character_voice(character_id: str, *, voice_id: Any = None, voice_name: Any = "", description: Any = "",
                        preview_url: Any = None, actor: str | None = None) -> dict | None:
    """Save the character's voice (the pick of POST /api/voices/pick), or clear it (voice_id None).
    No provider is called: the pick already happened."""
    fields = None if voice_id is None else _voice_fields(voice_id, voice_name, description, preview_url)
    with connect() as conn, _tx(conn):
        row = cast_store._character_row(conn, character_id)
        series_id = row["library_id"] if row["library_kind"] == "series" else None
        if fields is None:
            conn.execute("DELETE FROM character_voices WHERE character_id = ?", (character_id,))
            audit(conn, actor, "character_voice_clear", series_id=series_id, character_id=character_id)
            return None
        conn.execute("INSERT OR REPLACE INTO character_voices (character_id, description, voice_id, voice_name, "
                     "preview_url, chosen_by, chosen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (character_id, fields["description"], fields["voice_id"], fields["voice_name"],
                      fields["preview_url"], actor, _now()))
        audit(conn, actor, "character_voice_save", series_id=series_id, character_id=character_id,
              details={"voice_id": fields["voice_id"], "voice_name": fields["voice_name"]})
        saved = conn.execute("SELECT * FROM character_voices WHERE character_id = ?", (character_id,)).fetchone()
    return _voice_view(saved)


def character_voices(character_ids: list[str]) -> dict[str, dict]:
    """The saved voice of each character that has one."""
    if not character_ids:
        return {}
    marks = ", ".join("?" for _ in character_ids)
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM character_voices WHERE character_id IN ({marks})",
                            list(character_ids)).fetchall()
    return {r["character_id"]: _voice_view(r) for r in rows}


def set_narrator_voice(series_id: str, *, voice_id: Any = None, voice_name: Any = "", description: Any = "",
                       preview_url: Any = None, actor: str | None = None) -> dict | None:
    """Save the Universe's narrator voice, or clear it (voice_id None)."""
    fields = None if voice_id is None else _voice_fields(voice_id, voice_name, description, preview_url)
    with connect() as conn, _tx(conn):
        _series_row(conn, series_id)
        _ensure_profile(conn, series_id, actor)
        values = (None, None, "", None) if fields is None else (
            fields["voice_id"], fields["voice_name"], fields["description"], fields["preview_url"])
        conn.execute("UPDATE series_profiles SET narrator_voice_id = ?, narrator_voice_name = ?, "
                     "narrator_voice_description = ?, narrator_preview_url = ?, updated_by = ?, updated_at = ? "
                     "WHERE series_id = ?", (*values, actor, _now(), series_id))
        audit(conn, actor, "narrator_voice_clear" if fields is None else "narrator_voice_save", series_id=series_id,
              details=None if fields is None else {"voice_id": fields["voice_id"],
                                                   "voice_name": fields["voice_name"]})
    return get_profile(series_id)["narrator_voice"]


# ------------------------------------------------------------------------------------- references

def _note_row(conn, series_id: str, note_id: str):
    row = conn.execute("SELECT * FROM universe_notes WHERE note_id = ? AND series_id = ?",
                       (note_id, series_id)).fetchone()
    if not row:
        raise CastNotFound(f"Note {note_id} not found")
    return row


def _topic(value: Any) -> str:
    topic = str(value or "").strip().lower()
    if topic not in NOTE_TOPICS:
        raise CastError("Say what the note is about: premise, characters, places or tone")
    return topic


def list_notes(series_id: str) -> list[dict]:
    with connect() as conn:
        _series_row(conn, series_id)
        rows = conn.execute("SELECT * FROM universe_notes WHERE series_id = ? ORDER BY created_at, note_id",
                            (series_id,)).fetchall()
    return [dict(r) for r in rows]


def add_note(series_id: str, topic: Any, body: Any, actor: str | None = None) -> dict:
    topic = _topic(topic)
    body = _clean_block(body, "Note", NOTE_MAX)
    if not body:
        raise CastError("Write the note first")
    note_id = _new_id("un")
    now = _now()
    with connect() as conn, _tx(conn):
        _series_row(conn, series_id)
        count = conn.execute("SELECT COUNT(*) FROM universe_notes WHERE series_id = ?", (series_id,)).fetchone()[0]
        if count >= MAX_NOTES:
            raise CastConflict(f"This Universe already has {count} notes (the limit is {MAX_NOTES})")
        conn.execute("INSERT INTO universe_notes (note_id, series_id, topic, body, created_by, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (note_id, series_id, topic, body, actor, now, now))
        audit(conn, actor, "universe_note_add", series_id=series_id,
              details={"note_id": note_id, "topic": topic, "chars": len(body)})
        row = _note_row(conn, series_id, note_id)
    return dict(row)


def update_note(series_id: str, note_id: str, body: Any, actor: str | None = None) -> dict:
    body = _clean_block(body, "Note", NOTE_MAX)
    if not body:
        raise CastError("A note cannot be empty; delete it instead")
    with connect() as conn, _tx(conn):
        _note_row(conn, series_id, note_id)
        conn.execute("UPDATE universe_notes SET body = ?, updated_at = ? WHERE note_id = ?", (body, _now(), note_id))
        audit(conn, actor, "universe_note_update", series_id=series_id, details={"note_id": note_id, "chars": len(body)})
        row = _note_row(conn, series_id, note_id)
    return dict(row)


def delete_note(series_id: str, note_id: str, actor: str | None = None) -> dict:
    with connect() as conn, _tx(conn):
        row = _note_row(conn, series_id, note_id)
        conn.execute("DELETE FROM universe_notes WHERE note_id = ?", (note_id,))
        audit(conn, actor, "universe_note_delete", series_id=series_id,
              details={"note_id": note_id, "topic": row["topic"]})
    return {"deleted": note_id}


def _photo_view(row) -> dict:
    out = dict(row)
    if out.get("attestation"):
        try:
            out["attestation"] = json.loads(out["attestation"])
        except ValueError:
            pass
    return out


def list_reference_photos(series_id: str) -> list[dict]:
    with connect() as conn:
        _series_row(conn, series_id)
        rows = conn.execute("SELECT * FROM universe_photos WHERE series_id = ? AND status = 'active' "
                            "ORDER BY created_at, image_id", (series_id,)).fetchall()
    return [_photo_view(r) for r in rows]


def get_reference_photo(image_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM universe_photos WHERE image_id = ? AND status = 'active'",
                           (image_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Image {image_id} not found")
    return _photo_view(row)


def _reference_photo_in(conn, image_id: str) -> dict:
    return _photo_view(conn.execute("SELECT * FROM universe_photos WHERE image_id = ?", (image_id,)).fetchone())


def add_reference_photo(series_id: str, data: bytes, *, filename: str | None, attestation: dict, caption: Any = "",
                        actor: str | None = None) -> dict:
    """A reference photo for the mood board: the cast upload checks and rights attestation, stored
    privately, deduplicated by content within the Universe."""
    caption = _clean(caption, "Caption", CAPTION_MAX)
    info = cast_store.inspect_image(data)
    with connect() as conn:
        with _tx(conn):
            _series_row(conn, series_id)
            existing = conn.execute("SELECT image_id FROM universe_photos WHERE series_id = ? AND sha256 = ?",
                                    (series_id, info["sha256"])).fetchone()
            if existing:
                conn.execute("UPDATE universe_photos SET status = 'active' WHERE image_id = ?", (existing[0],))
                return {**_reference_photo_in(conn, existing[0]), "duplicate": True}
            count = conn.execute("SELECT COUNT(*) FROM universe_photos WHERE series_id = ? AND status = 'active'",
                                 (series_id,)).fetchone()[0]
            if count >= MAX_REFERENCE_PHOTOS:
                raise CastConflict(f"This Universe already has {count} reference photos (the limit is "
                                   f"{MAX_REFERENCE_PHOTOS}); delete one first")
            cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
            image_id = _new_id("ui")
            conn.execute(
                "INSERT INTO universe_photos (image_id, series_id, status, sha256, ext, mime, width, height, bytes, "
                "caption, original_filename, attestation, created_by, created_at) "
                "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (image_id, series_id, info["sha256"], info["ext"], info["mime"], info["width"], info["height"],
                 info["bytes"], caption, (filename or "")[:200] or None, json.dumps(attestation, sort_keys=True),
                 actor, _now()))
            audit(conn, actor, "universe_photo_upload", series_id=series_id, image_id=image_id,
                  details={"sha256": info["sha256"], "bytes": info["bytes"], "width": info["width"],
                           "height": info["height"], "format": info["format"], "filename": (filename or "")[:200],
                           "attestation": attestation})
    cast_store.ensure_thumb(info["sha256"], info["ext"])
    return {**get_reference_photo(image_id), "duplicate": False}


def delete_reference_photo(series_id: str, image_id: str, actor: str | None = None) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM universe_photos WHERE image_id = ? AND series_id = ?",
                           (image_id, series_id)).fetchone()
        if not row:
            raise CastNotFound(f"Image {image_id} not found")
        with _tx(conn):
            if _making_request(conn, series_id) is not None:
                raise CastConflict(f"{BOARD_BEING_MADE} from the reference photos; delete this photo after that")
            conn.execute("DELETE FROM universe_photos WHERE image_id = ?", (image_id,))
            conn.execute("UPDATE series_profiles SET cover_image_id = NULL WHERE series_id = ? AND cover_image_id = ?",
                         (series_id, image_id))
            audit(conn, actor, "universe_photo_delete", series_id=series_id, image_id=image_id,
                  details={"sha256": row["sha256"]})
        removed = cast_store._remove_unreferenced_file(conn, row["sha256"], row["ext"])
    return {"deleted": image_id, "file_removed": removed}


# ------------------------------------------------------------------------------------- mood boards

def _board_rows(conn, series_id: str) -> list:
    return conn.execute("SELECT * FROM universe_boards WHERE series_id = ? ORDER BY version", (series_id,)).fetchall()


def _board_view(conn, row, *, with_prompt: bool) -> dict:
    out = dict(row)
    out["dry_run"] = bool(out["dry_run"])
    try:
        out["sources"] = json.loads(out.get("sources") or "{}")
    except ValueError:
        out["sources"] = {}
    if not with_prompt:
        out.pop("prompt", None)
    images = conn.execute("SELECT image_id, position, width, height, placeholder, source FROM universe_board_images "
                          "WHERE board_id = ? ORDER BY position", (row["board_id"],)).fetchall()
    out["images"] = [{**dict(i), "placeholder": bool(i["placeholder"])} for i in images]
    return out


def list_boards(series_id: str, *, with_prompt: bool = False) -> list[dict]:
    """Every mood board version, oldest first. The prompt is the owner's (``with_prompt``)."""
    with connect() as conn:
        _series_row(conn, series_id)
        return [_board_view(conn, r, with_prompt=with_prompt) for r in _board_rows(conn, series_id)]


def board_prompt(name: str, profile: dict, notes: list[dict], photos: list[dict], feedback: str = "") -> str:
    """The words a mood board is made from: the creator's notes by topic, the photo captions, the
    style notes and the redo feedback. Deterministic, at most BOARD_PROMPT_MAX characters."""
    parts = [f'Mood board for an original story universe called "{name}": {BOARD_IMAGES} images that set its '
             "look (characters, places, light, colour and texture). No words or lettering in the images."]
    if profile.get("logline"):
        parts.append(f"Logline: {_clip(profile['logline'], 200)}")
    for topic in NOTE_TOPICS:
        bodies = [n["body"] for n in notes if n["topic"] == topic]
        if bodies:
            parts.append(f"{NOTE_LABELS[topic]}: {_clip(' '.join(bodies), 500)}")
    if profile.get("style_notes"):
        parts.append(f"Style: {_clip(profile['style_notes'], 300)}")
    if photos:
        captions = [p["caption"] for p in photos if p.get("caption")]
        parts.append(f"Reference photos: {len(photos)}" + (f" ({_clip('; '.join(captions), 200)})" if captions else ""))
    if feedback:
        parts.append(f"Change from the last board: {_clip(feedback, FEEDBACK_MAX)}")
    text = "\n".join(parts)
    return text if len(text) <= BOARD_PROMPT_MAX else text[: BOARD_PROMPT_MAX - 1].rstrip() + "…"


def plan_board(series_id: str, feedback: Any = "") -> dict:
    """What the next mood board would be: its version, prompt, image count and the references it is
    made from. Refused once a board is approved, while a live board is being made, while the latest
    board waits for a decision and no redo feedback is given, and before there is any reference."""
    feedback = _clean(feedback, "Feedback", FEEDBACK_MAX)
    with connect() as conn:
        series = _series_row(conn, series_id)
        boards = _board_rows(conn, series_id)
        profile = _profile_view(series_id, conn.execute("SELECT * FROM series_profiles WHERE series_id = ?",
                                                        (series_id,)).fetchone())
        notes = [dict(r) for r in conn.execute("SELECT * FROM universe_notes WHERE series_id = ? "
                                               "ORDER BY created_at, note_id", (series_id,))]
        photos = [dict(r) for r in conn.execute("SELECT * FROM universe_photos WHERE series_id = ? AND "
                                                "status = 'active' ORDER BY created_at, image_id", (series_id,))]
        making = _making_request(conn, series_id)
    if any(b["status"] == "approved" for b in boards):
        raise CastConflict("The mood board is approved; the next step is the Universe bible")
    if making is not None:
        raise CastConflict(BOARD_BEING_MADE)
    latest = boards[-1] if boards else None
    if latest is None and feedback:
        raise CastConflict("There is no mood board to redo yet")
    if latest is not None and latest["status"] == "ready" and not feedback:
        raise CastConflict("Approve this mood board, or say what to change and ask for a redo")
    if not notes and not photos:
        raise CastConflict("Add a reference first: a few words about the premise, characters, places or tone, "
                           "or a photo")
    return {"version": (latest["version"] + 1) if latest else 1,
            "prompt": board_prompt(series["name"], profile, notes, photos, feedback),
            "answers_feedback": feedback, "image_count": BOARD_IMAGES,
            "replaces_board_id": latest["board_id"] if latest is not None and feedback else None,
            "sources": {"notes": [n["note_id"] for n in notes], "photos": [p["image_id"] for p in photos]},
            "photo_files": [str(cast_store.image_file(p["sha256"], p["ext"])) for p in photos],
            "photo_refs": [{"image_id": p["image_id"], "sha256": p["sha256"], "ext": p["ext"]} for p in photos]}


def placeholder_board_image(seed: str, photo_files: list[str], position: int) -> bytes:
    """One deterministic dry-run stand-in for a mood board picture (no provider call): it frames one
    of the reference photos (when there are any) on a colour field, with a palette strip taken from it."""
    from PIL import Image, ImageDraw, ImageOps
    digest = hashlib.sha256(f"{seed}:{position}".encode()).digest()
    bg = (60 + digest[0] % 150, 60 + digest[1] % 150, 60 + digest[2] % 150)
    img = Image.new("RGB", (BOARD_PX, BOARD_PX), bg)
    draw = ImageDraw.Draw(img)
    palette = [(digest[3 + 3 * k] % 256, digest[4 + 3 * k] % 256, digest[5 + 3 * k] % 256) for k in range(3)]
    photo = None
    if photo_files:
        try:
            with Image.open(photo_files[position % len(photo_files)]) as src:
                photo = ImageOps.fit(ImageOps.exif_transpose(src).convert("RGB"), (BOARD_PX * 2 // 3,) * 2)
        except (OSError, ValueError):
            photo = None
    if photo is not None:
        img.paste(photo, (BOARD_PX // 6, BOARD_PX // 12))
        small = photo.resize((3, 1))
        palette = [small.getpixel((k, 0)) for k in range(3)]
    else:
        for k, colour in enumerate(palette):
            r = BOARD_PX // (4 + k)
            cx = BOARD_PX * (1 + 2 * k) // 6
            draw.ellipse((cx - r // 2, BOARD_PX // 3 - r // 2, cx + r // 2, BOARD_PX // 3 + r // 2), fill=colour)
    band = BOARD_PX // 8
    for k, colour in enumerate(palette):
        draw.rectangle((k * BOARD_PX // 3, BOARD_PX - band, (k + 1) * BOARD_PX // 3, BOARD_PX), fill=colour)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def placeholder_board_images(seed: str, photo_files: list[str], count: int = BOARD_IMAGES) -> list[bytes]:
    """The dry-run stand-ins of a whole mood board (placeholder_board_image for each position)."""
    return [placeholder_board_image(seed, photo_files, position) for position in range(count)]


def create_board(series_id: str, plan: dict, *, estimate_usd_micros: int, approved_total_usd_micros: int,
                 dry_run: bool, images: list[bytes], placeholder: bool, requested_by: str | None) -> dict:
    """Record a mood board version and its images. A redo marks the board it answers as
    ``redo_requested`` with the feedback. Refused when another board was made meanwhile."""
    infos = [cast_store.inspect_image(data, max_bytes=40 * 1024 * 1024, min_px=256) for data in images]
    board_id = _new_id("mb")
    now = _now()
    with connect() as conn:
        with _tx(conn):
            _series_row(conn, series_id)
            boards = _board_rows(conn, series_id)
            latest = boards[-1] if boards else None
            if ((latest["version"] + 1) if latest else 1) != plan["version"] or any(
                    b["status"] == "approved" for b in boards):
                raise CastConflict("The mood board changed meanwhile; look at it again before asking for another")
            if plan.get("replaces_board_id"):
                conn.execute("UPDATE universe_boards SET status = 'redo_requested', redo_feedback = ?, decided_by = ?, "
                             "decided_at = ?, updated_at = ? WHERE board_id = ? AND status = 'ready'",
                             (plan["answers_feedback"], requested_by, now, now, plan["replaces_board_id"]))
            conn.execute(
                "INSERT INTO universe_boards (board_id, series_id, version, status, prompt, answers_feedback, sources, "
                "image_count, estimate_usd_micros, approved_total_usd_micros, dry_run, requested_by, created_at, "
                "updated_at) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (board_id, series_id, plan["version"], plan["prompt"], plan["answers_feedback"],
                 json.dumps(plan["sources"], sort_keys=True), len(images), int(estimate_usd_micros),
                 int(approved_total_usd_micros), int(dry_run), requested_by, now, now))
            for position, (data, info) in enumerate(zip(images, infos)):
                cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
                conn.execute(
                    "INSERT INTO universe_board_images (image_id, board_id, position, sha256, ext, mime, width, "
                    "height, bytes, placeholder, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_new_id("bi"), board_id, position, info["sha256"], info["ext"], info["mime"], info["width"],
                     info["height"], info["bytes"], int(placeholder), "placeholder" if placeholder else "generated",
                     now))
            audit(conn, requested_by, "universe_board_create", series_id=series_id,
                  details={"board_id": board_id, "version": plan["version"], "images": len(images),
                           "estimate_usd_micros": int(estimate_usd_micros),
                           "approved_total_usd_micros": int(approved_total_usd_micros), "dry_run": bool(dry_run),
                           "redo_of": plan.get("replaces_board_id")})
        for info in infos:
            cast_store.ensure_thumb(info["sha256"], info["ext"])
        return _board_view(conn, conn.execute("SELECT * FROM universe_boards WHERE board_id = ?",
                                              (board_id,)).fetchone(), with_prompt=True)


def approve_board(series_id: str, board_id: str, actor: str | None = None) -> dict:
    """Approve the latest board while it waits for a decision; nothing downstream (the Universe
    bible) starts before this."""
    now = _now()
    with connect() as conn:
        with _tx(conn):
            boards = _board_rows(conn, series_id)
            row = next((b for b in boards if b["board_id"] == board_id), None)
            if row is None:
                raise CastNotFound(f"Mood board {board_id} not found")
            if row["status"] == "approved":
                raise CastConflict("This mood board is approved already")
            if row["status"] != "ready" or row["board_id"] != boards[-1]["board_id"]:
                raise CastConflict("Only the latest mood board can be approved")
            if _making_request(conn, series_id) is not None:
                raise CastConflict(f"{BOARD_BEING_MADE} before approving one")
            conn.execute("UPDATE universe_boards SET status = 'approved', decided_by = ?, decided_at = ?, updated_at = ? "
                         "WHERE board_id = ?", (actor, now, now, board_id))
            audit(conn, actor, "universe_board_approve", series_id=series_id,
                  details={"board_id": board_id, "version": row["version"]})
        return _board_view(conn, conn.execute("SELECT * FROM universe_boards WHERE board_id = ?",
                                              (board_id,)).fetchone(), with_prompt=True)


def get_board_image(image_id: str) -> dict:
    """A mood board image with its board's series id and status (board_status)."""
    with connect() as conn:
        row = conn.execute("SELECT i.*, b.series_id, b.status AS board_status FROM universe_board_images i "
                           "JOIN universe_boards b ON b.board_id = i.board_id WHERE i.image_id = ?",
                           (image_id,)).fetchone()
    if not row:
        raise CastNotFound(f"Image {image_id} not found")
    return dict(row)


# ------------------------------------------------------------------------------------- live mood boards
# A live board is a request the worker makes picture by picture (hfvg.activities.universe_board);
# its pictures become the next board version only when every one of them is made.

def _making_request(conn, series_id: str):
    return conn.execute("SELECT * FROM universe_board_requests WHERE series_id = ? AND status = 'making' "
                        "ORDER BY created_at DESC LIMIT 1", (series_id,)).fetchone()


def _request_row(conn, request_id: str):
    row = conn.execute("SELECT * FROM universe_board_requests WHERE request_id = ?", (request_id,)).fetchone()
    if row is None:
        raise CastNotFound(f"Mood board request {request_id} not found")
    return row


def _request_view(conn, row) -> dict:
    out = {k: row[k] for k in ("request_id", "series_id", "status", "answers_feedback", "replaces_board_id",
                               "image_count", "model_path", "estimate_usd_micros", "approved_total_usd_micros",
                               "workflow_id", "board_id", "error", "requested_by", "created_at", "updated_at")}
    jobs = conn.execute("SELECT position, status FROM universe_board_jobs WHERE request_id = ? ORDER BY position",
                        (row["request_id"],)).fetchall()
    out["jobs"] = [dict(j) for j in jobs]
    out["made"] = sum(1 for j in jobs if j["status"] == "completed")
    return out


def board_request(request_id: str) -> dict:
    with connect() as conn:
        return _request_view(conn, _request_row(conn, request_id))


def open_board_request(series_id: str) -> dict | None:
    """The live board the owner should hear about: the one being made, the last one that failed after
    the latest board was made, or the one that made the latest board with fewer pictures than asked
    (each with its reason). None otherwise."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM universe_board_requests WHERE series_id = ? "
                           "ORDER BY created_at DESC, rowid DESC LIMIT 1", (series_id,)).fetchone()
        if row is None:
            return None
        latest = conn.execute("SELECT board_id, created_at FROM universe_boards WHERE series_id = ? "
                              "ORDER BY version DESC LIMIT 1", (series_id,)).fetchone()
        if row["status"] == "done" and (not row["error"] or latest is None or latest["board_id"] != row["board_id"]):
            return None
        if row["status"] == "failed" and latest is not None and latest["created_at"] > row["updated_at"]:
            return None
        return _request_view(conn, row)


def making_board_requests(series_id: str) -> list[dict]:
    """The live boards of a Universe that are being made (request id, workflow id, created at)."""
    with connect() as conn:
        rows = conn.execute("SELECT request_id, workflow_id, created_at FROM universe_board_requests "
                            "WHERE series_id = ? AND status = 'making'", (series_id,)).fetchall()
    return [dict(r) for r in rows]


def start_board_request(series_id: str, plan: dict, jobs: list[dict], *, request_id: str, workflow_id: str,
                        estimate_usd_micros: int, approved_total_usd_micros: int, model_path: str,
                        requested_by: str | None) -> dict:
    """Record a live board whose estimate the owner approved, with its picture jobs (queued), before
    the worker is asked to make it. Refused when the boards changed since the plan, or while another
    live board is being made."""
    now = _now()
    with connect() as conn:
        with _tx(conn):
            _series_row(conn, series_id)
            boards = _board_rows(conn, series_id)
            latest = boards[-1] if boards else None
            if ((latest["version"] + 1) if latest else 1) != plan["version"] or any(
                    b["status"] == "approved" for b in boards):
                raise CastConflict("The mood board changed meanwhile; look at it again before asking for another")
            if _making_request(conn, series_id) is not None:
                raise CastConflict(BOARD_BEING_MADE)
            conn.execute(
                "INSERT INTO universe_board_requests (request_id, series_id, status, prompt, answers_feedback, "
                "replaces_board_id, sources, image_count, model_path, estimate_usd_micros, approved_total_usd_micros, "
                "workflow_id, requested_by, created_at, updated_at) "
                "VALUES (?, ?, 'making', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, series_id, plan["prompt"], plan["answers_feedback"], plan.get("replaces_board_id"),
                 json.dumps(plan["sources"], sort_keys=True), len(jobs), model_path, int(estimate_usd_micros),
                 int(approved_total_usd_micros), workflow_id, requested_by, now, now))
            for job in jobs:
                conn.execute(
                    "INSERT INTO universe_board_jobs (job_id, request_id, position, status, estimate_usd_micros, "
                    "approved_usd_micros, reference_count, created_at, updated_at) "
                    "VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)",
                    (job["job_id"], request_id, int(job["position"]), int(job["estimated_usd_micros"]),
                     int(job["approved_usd_micros"]), len(job["refs"]), now, now))
            audit(conn, requested_by, "universe_board_request", series_id=series_id,
                  details={"request_id": request_id, "version": plan["version"], "images": len(jobs),
                           "estimate_usd_micros": int(estimate_usd_micros),
                           "approved_total_usd_micros": int(approved_total_usd_micros),
                           "redo_of": plan.get("replaces_board_id"), "workflow_id": workflow_id,
                           "model_path": model_path})
        return _request_view(conn, _request_row(conn, request_id))


def board_job(job_id: str) -> dict | None:
    """A live board picture job, with its request's series id and status (request_status)."""
    with connect() as conn:
        row = conn.execute("SELECT j.*, r.series_id, r.status AS request_status FROM universe_board_jobs j "
                           "JOIN universe_board_requests r ON r.request_id = j.request_id WHERE j.job_id = ?",
                           (job_id,)).fetchone()
    return dict(row) if row else None


def set_board_job(job_id: str, status: str, error: str | None = None) -> None:
    if status not in BOARD_JOB_STATUSES:
        raise CastError(f"Unknown mood board job status {status!r}")
    with connect() as conn:
        conn.execute("UPDATE universe_board_jobs SET status = ?, error = ?, updated_at = ? WHERE job_id = ?",
                     (status, (error or "")[:1000] or None, _now(), job_id))


def store_board_job_picture(job_id: str, data: bytes, *, placeholder: bool) -> dict:
    """Keep the picture a board job made (the private image store) and complete the job.
    Idempotent: a job that has its picture keeps the first one."""
    info = cast_store.inspect_image(data, max_bytes=40 * 1024 * 1024, min_px=256)
    with connect() as conn:
        with _tx(conn):
            row = conn.execute("SELECT * FROM universe_board_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise CastNotFound(f"Mood board job {job_id} not found")
            if row["sha256"] is None:
                cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
                conn.execute(
                    "UPDATE universe_board_jobs SET status = 'completed', sha256 = ?, ext = ?, mime = ?, width = ?, "
                    "height = ?, bytes = ?, placeholder = ?, error = NULL, updated_at = ? WHERE job_id = ?",
                    (info["sha256"], info["ext"], info["mime"], info["width"], info["height"], info["bytes"],
                     int(placeholder), _now(), job_id))
            else:
                conn.execute("UPDATE universe_board_jobs SET status = 'completed', error = NULL, updated_at = ? "
                             "WHERE job_id = ?", (_now(), job_id))
            sha, ext = conn.execute("SELECT sha256, ext FROM universe_board_jobs WHERE job_id = ?",
                                    (job_id,)).fetchone()
    cast_store.ensure_thumb(sha, ext)
    return board_job(job_id)


def finish_board_request(request_id: str, error: str | None = None, actor: str | None = "worker") -> dict:
    """End a live board request: the pictures it made become the next mood board version, ``ready``
    for the owner (a redo marks the board it answers ``redo_requested``), so a picture that was paid
    for is never lost. A request that stopped early (``error``, or fewer pictures than asked) keeps the
    reason and its jobs that did not run are ``skipped``; with no picture made it is ``failed``.
    Idempotent: an ended request is returned as it is."""
    with connect() as conn:
        with _tx(conn):
            row = _request_row(conn, request_id)
            if row["status"] == "making":
                _end_request(conn, row, error, actor, _now())
        return _request_view(conn, _request_row(conn, request_id))


def _end_request(conn, req, error: str | None, actor: str | None, now: str) -> None:
    jobs = conn.execute("SELECT * FROM universe_board_jobs WHERE request_id = ? ORDER BY position",
                        (req["request_id"],)).fetchall()
    made = [j for j in jobs if j["status"] == "completed" and j["sha256"]]
    boards = _board_rows(conn, req["series_id"])
    if error is None and len(made) < req["image_count"]:
        error = BOARD_STOPPED
    if error is not None:
        conn.execute("UPDATE universe_board_jobs SET status = 'skipped', error = ?, updated_at = ? "
                     "WHERE request_id = ? AND status IN ('queued', 'running')",
                     (f"Stopped: {error}"[:1000], now, req["request_id"]))
    if any(b["status"] == "approved" for b in boards):
        error = "A mood board was approved meanwhile"
    elif not made:
        error = error or BOARD_STOPPED
    else:
        _board_from_request(conn, req, made, boards, error, now)
        return
    conn.execute("UPDATE universe_board_requests SET status = 'failed', error = ?, updated_at = ? "
                 "WHERE request_id = ?", (error[:1000], now, req["request_id"]))
    audit(conn, actor, "universe_board_failed", series_id=req["series_id"],
          details={"request_id": req["request_id"], "made": len(made), "error": error[:300]})


def _board_from_request(conn, req, made: list, boards: list, error: str | None, now: str) -> None:
    version = (boards[-1]["version"] + 1) if boards else 1
    board_id = _new_id("mb")
    placeholder = all(j["placeholder"] for j in made)
    if req["replaces_board_id"]:
        conn.execute("UPDATE universe_boards SET status = 'redo_requested', redo_feedback = ?, decided_by = ?, "
                     "decided_at = ?, updated_at = ? WHERE board_id = ? AND status = 'ready'",
                     (req["answers_feedback"], req["requested_by"], now, now, req["replaces_board_id"]))
    conn.execute(
        "INSERT INTO universe_boards (board_id, series_id, version, status, prompt, answers_feedback, sources, "
        "image_count, estimate_usd_micros, approved_total_usd_micros, dry_run, requested_by, created_at, "
        "updated_at) VALUES (?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (board_id, req["series_id"], version, req["prompt"], req["answers_feedback"], req["sources"], len(made),
         req["estimate_usd_micros"], req["approved_total_usd_micros"], int(placeholder), req["requested_by"], now,
         now))
    for job in made:
        conn.execute(
            "INSERT INTO universe_board_images (image_id, board_id, position, sha256, ext, mime, width, height, "
            "bytes, placeholder, source, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("bi"), board_id, job["position"], job["sha256"], job["ext"], job["mime"], job["width"],
             job["height"], job["bytes"], job["placeholder"], "placeholder" if job["placeholder"] else "generated",
             now))
    conn.execute("UPDATE universe_board_requests SET status = 'done', board_id = ?, error = ?, updated_at = ? "
                 "WHERE request_id = ?", (board_id, error[:1000] if error else None, now, req["request_id"]))
    audit(conn, req["requested_by"], "universe_board_create", series_id=req["series_id"],
          details={"board_id": board_id, "version": version, "images": len(made),
                   "asked_images": req["image_count"], "estimate_usd_micros": req["estimate_usd_micros"],
                   "approved_total_usd_micros": req["approved_total_usd_micros"], "dry_run": placeholder,
                   "redo_of": req["replaces_board_id"], "request_id": req["request_id"]})


# ------------------------------------------------------------------------------------- the bible

def bible_inputs(series_id: str) -> dict:
    """What the Universe bible is written from: the approved mood board, the references and the
    names already in the Universe. Refused before a board is approved, and once the bible of the
    approved board was made."""
    with connect() as conn:
        series = _series_row(conn, series_id)
        profile = _profile_view(series_id, conn.execute("SELECT * FROM series_profiles WHERE series_id = ?",
                                                        (series_id,)).fetchone())
        approved = conn.execute("SELECT * FROM universe_boards WHERE series_id = ? AND status = 'approved' "
                                "ORDER BY version DESC LIMIT 1", (series_id,)).fetchone()
        notes = [dict(r) for r in conn.execute("SELECT topic, body FROM universe_notes WHERE series_id = ? "
                                               "ORDER BY created_at, note_id", (series_id,))]
        captions = [r[0] for r in conn.execute("SELECT caption FROM universe_photos WHERE series_id = ? AND "
                                               "status = 'active' AND caption != '' ORDER BY created_at",
                                               (series_id,))]
        characters = [r[0] for r in conn.execute("SELECT name FROM cast_characters WHERE library_kind = 'series' "
                                                 "AND library_id = ? ORDER BY created_at", (series_id,))]
        items = conn.execute("SELECT kind, name FROM prop_items WHERE library_kind = 'series' AND library_id = ? "
                             "ORDER BY created_at", (series_id,)).fetchall()
    if approved is None:
        raise CastConflict("Approve a mood board first: the Universe bible is written from it")
    if profile["bible_board_id"] == approved["board_id"]:
        raise CastConflict("The bible of this mood board is made already; change the characters, places and props "
                           "on the Universe page")
    return {"series_id": series_id, "name": series["name"], "logline": profile["logline"],
            "style_notes": profile["style_notes"], "preset_id": profile["preset_id"],
            "notes": {topic: [n["body"] for n in notes if n["topic"] == topic] for topic in NOTE_TOPICS},
            "photo_captions": captions, "board_id": approved["board_id"], "board_prompt": approved["prompt"],
            "existing": {"characters": characters, "places": [r[1] for r in items if r[0] == "place"],
                         "props": [r[1] for r in items if r[0] == "prop"]}}


def bible_messages(inputs: dict) -> list[dict]:
    """The language model request for the Universe bible (owner and admins only ever see it)."""
    system = (
        "You write the bible of an original story universe for a short-video studio. Return a JSON object with "
        'these keys: "bible" (2-4 short paragraphs: the world, its rules and its recurring story engine), '
        '"style_notes" (one paragraph: look, palette, light, camera), "characters" (a list of at most '
        f'{BIBLE_CHARACTERS_MAX} objects with "name", "role" and "description"), "places" (at most '
        f'{BIBLE_PLACES_MAX} objects with "name" and "description") and "props" (at most {BIBLE_PROPS_MAX} objects '
        'with "name" and "description"). Each description says how it looks, the same in every episode (build, '
        "age, face, hair, colours, clothing; materials and shapes for places and props).\n"
        "CONTENT POLICY (CRITICAL): only original characters, places and things. No named IP, franchises, brands, "
        "celebrities or real people; turn any such mention into an original homage with a new name.")
    lines = [f"Universe: {inputs['name']}"]
    if inputs.get("logline"):
        lines.append(f"Logline: {inputs['logline']}")
    for topic in NOTE_TOPICS:
        for body in inputs["notes"].get(topic) or []:
            lines.append(f"{NOTE_LABELS[topic]} (from the creator): {body}")
    if inputs.get("photo_captions"):
        lines.append("Reference photo captions: " + "; ".join(inputs["photo_captions"]))
    if inputs.get("style_notes"):
        lines.append(f"Style notes: {inputs['style_notes']}")
    lines.append(f"Approved mood board (stay true to this look):\n{inputs['board_prompt']}")
    existing = inputs["existing"]
    if any(existing.values()):
        lines.append("Already in the Universe (keep them, do not list them again): " + ", ".join(
            [*existing["characters"], *existing["places"], *existing["props"]]))
    lines.append("Write the bible as JSON.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


def _named_lines(bodies: list[str]) -> list[tuple[str, str]]:
    """'Name: description' (or 'Name - description') lines of the creator's notes."""
    out = []
    for body in bodies:
        for line in body.split("\n"):
            line = line.strip(" -*\t")
            if not line:
                continue
            match = re.match(r"^([^:\u2014\u2013]{1,60}?)\s*(?::|\u2014|\u2013| - )\s*(.+)$", line)
            name, description = (match.group(1), match.group(2)) if match else (line[:60], "")
            if name.strip():
                out.append((name.strip(), description.strip()))
    return out


def fake_bible(inputs: dict) -> dict:
    """Deterministic DRAFT_LLM_FAKE bible (test/e2e only): the characters and places named in the
    notes, the premise and tone as the bible, the approved board's look as the style notes."""
    premise = " ".join(inputs["notes"].get("premise") or []) or inputs.get("logline") or inputs["name"]
    tone = " ".join(inputs["notes"].get("tone") or [])
    characters = [{"name": n, "role": "", "description": d or f"original character of {inputs['name']}"}
                  for n, d in _named_lines(inputs["notes"].get("characters") or [])]
    places = [{"name": n, "description": d or f"a place in {inputs['name']}"}
              for n, d in _named_lines(inputs["notes"].get("places") or [])]
    return {"bible": f"{premise}\n\n{tone}".strip(),
            "style_notes": _clip(f"The approved mood board's look. {tone}".strip(), STYLE_NOTES_MAX),
            "characters": characters, "places": places, "props": []}


def parse_bible(raw: Any) -> dict:
    """The model's bible, cleaned: texts trimmed to the field limits, entries without a name left
    out, names unique (case-insensitive) within each list."""
    raw = raw if isinstance(raw, dict) else {}

    def text(value: Any, limit: int) -> str:
        return _clip(value if isinstance(value, str) else "", limit)

    def entries(key: str, limit: int, description_max: int, with_role: bool) -> list[dict]:
        seen: set[str] = set()
        out = []
        for entry in raw.get(key) if isinstance(raw.get(key), list) else []:
            if not isinstance(entry, dict):
                continue
            name = text(entry.get("name"), cast_store.NAME_MAX)
            if not name or name.lower() in seen:
                continue
            seen.add(name.lower())
            item = {"name": name, "description": text(entry.get("description"), description_max)}
            if with_role:
                item["role"] = text(entry.get("role"), cast_store.ROLE_MAX)
            out.append(item)
            if len(out) >= limit:
                break
        return out

    bible = raw.get("bible") if isinstance(raw.get("bible"), str) else ""
    return {"bible": _clean_block(bible[:BIBLE_MAX], "Story bible", BIBLE_MAX),
            "style_notes": text(raw.get("style_notes"), STYLE_NOTES_MAX),
            "characters": entries("characters", BIBLE_CHARACTERS_MAX, cast_store.DESCRIPTION_MAX, True),
            "places": entries("places", BIBLE_PLACES_MAX, prop_store.DESCRIPTION_MAX, False),
            "props": entries("props", BIBLE_PROPS_MAX, prop_store.DESCRIPTION_MAX, False)}


def apply_bible(series_id: str, bible: dict, *, board_id: str, actor: str | None = None) -> dict:
    """Add the bible's characters, places and props to the Universe (names it has already are kept
    as they are), fill the story bible and style notes when they are still empty, and record which
    approved board the bible was written from. One transaction."""
    library = ("series", series_id)
    created: dict[str, list[str]] = {"characters": [], "places": [], "props": []}
    skipped: list[str] = []
    with connect() as conn:
        with _tx(conn):
            _series_row(conn, series_id)
            approved = conn.execute("SELECT board_id FROM universe_boards WHERE series_id = ? AND status = 'approved' "
                                    "ORDER BY version DESC LIMIT 1", (series_id,)).fetchone()
            if approved is None or approved[0] != board_id:
                raise CastConflict("The approved mood board changed meanwhile; look at it again")
            _ensure_profile(conn, series_id, actor)
            profile = _profile_view(series_id, conn.execute("SELECT * FROM series_profiles WHERE series_id = ?",
                                                            (series_id,)).fetchone())
            if profile["bible_board_id"] == board_id:
                raise CastConflict("The bible of this mood board is made already")
            have = {r[0].lower() for r in conn.execute(
                "SELECT name FROM cast_characters WHERE library_kind = 'series' AND library_id = ?", (series_id,))}
            count = len(have)
            for entry in bible["characters"]:
                if entry["name"].lower() in have or count >= cast_store.MAX_CHARACTERS_PER_LIBRARY:
                    skipped.append(entry["name"])
                    continue
                fields = cast_store._character_fields(entry["name"], entry.get("role", ""), entry["description"], "",
                                                      None)
                cast_store._insert_character(conn, library, fields, actor, None)
                have.add(entry["name"].lower())
                count += 1
                created["characters"].append(entry["name"])
            items = {r[0].lower() for r in conn.execute(
                "SELECT name FROM prop_items WHERE library_kind = 'series' AND library_id = ?", (series_id,))}
            count = len(items)
            for kind, key in (("place", "places"), ("prop", "props")):
                for entry in bible[key]:
                    if entry["name"].lower() in items or count >= prop_store.MAX_ITEMS_PER_LIBRARY:
                        skipped.append(entry["name"])
                        continue
                    prop_store._insert_item(conn, library, prop_store._fields(kind, entry["name"],
                                                                              entry["description"], None), actor, None)
                    items.add(entry["name"].lower())
                    count += 1
                    created[key].append(entry["name"])
            fills = {}
            if not profile["bible"] and bible["bible"]:
                fills["bible"] = bible["bible"]
            if not profile["style_notes"] and bible["style_notes"]:
                fills["style_notes"] = bible["style_notes"]
            now = _now()
            sets = "".join(f"{k} = ?, " for k in fills)
            conn.execute(f"UPDATE series_profiles SET {sets}bible_board_id = ?, bible_generated_at = ?, updated_by = ?, "
                         "updated_at = ? WHERE series_id = ?", (*fills.values(), board_id, now, actor, now, series_id))
            audit(conn, actor, "universe_bible_apply", series_id=series_id,
                  details={"board_id": board_id, "created": created, "skipped": skipped, "filled": sorted(fills)})
    return {"created": created, "skipped": skipped, "filled": sorted(fills)}


# ------------------------------------------------------------------------------------- what episodes use

def _cast_entry(entry: Any) -> tuple[str, int | None] | None:
    """(slug, version named or None) of a shots.json cast entry; None for anything else."""
    if isinstance(entry, str):
        slug, version = entry.strip(), None
    elif isinstance(entry, dict) and isinstance(entry.get("character"), str):
        slug, version = entry["character"].strip(), entry.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            version = None
    else:
        return None
    return (slug, version) if slug else None


def shot_usage(episode_id: str) -> list[dict]:
    """Each shot's picks in shots.json order: [{"shot_id", "characters": [(slug, version or None)],
    "place": slug or None, "props": [slugs]}], a cast's ``shot_meta.cast`` versions included.
    A missing or unreadable file, and odd entries, count as nothing."""
    try:
        raw = json.loads((paths.episode_dir(episode_id) / "shots.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    shots = raw.get("shots") if isinstance(raw, dict) else None
    out: list[dict] = []
    for shot in shots if isinstance(shots, list) else []:
        if not isinstance(shot, dict):
            continue
        meta = shot.get("shot_meta") if isinstance(shot.get("shot_meta"), dict) else {}
        characters: list[tuple[str, int | None]] = []
        for entry in [*(shot.get("cast") if isinstance(shot.get("cast"), list) else []),
                      *(meta.get("cast") if isinstance(meta.get("cast"), list) else [])]:
            picked = _cast_entry(entry)
            if picked is not None and picked not in characters:
                characters.append(picked)
        place = shot.get("place").strip() if isinstance(shot.get("place"), str) else ""
        props = []
        for prop in shot.get("props") if isinstance(shot.get("props"), list) else []:
            if isinstance(prop, str) and prop.strip() and prop.strip() not in props:
                props.append(prop.strip())
        out.append({"shot_id": shot.get("id") if isinstance(shot.get("id"), str) else None,
                    "characters": characters, "place": place or None, "props": props})
    return out


def episode_usage(episode_id: str) -> dict:
    """What an episode's shots.json picks: {"characters": {slug: [versions named]}, "places": [...],
    "props": [...]}. Missing, unreadable or odd entries count as nothing."""
    usage: dict[str, Any] = {"characters": {}, "places": [], "props": []}
    for shot in shot_usage(episode_id):
        for slug, version in shot["characters"]:
            versions = usage["characters"].setdefault(slug, [])
            if version is not None and version not in versions:
                versions.append(version)
        if shot["place"] and shot["place"] not in usage["places"]:
            usage["places"].append(shot["place"])
        for prop in shot["props"]:
            if prop not in usage["props"]:
                usage["props"].append(prop)
    return usage


def used_in(episode_ids: list[str]) -> dict:
    """In how many of these episodes each character (and each of its versions named) and each prop
    or place is used: every episode counts once per item."""
    characters: dict[str, dict] = {}
    items: dict[str, int] = {}
    for episode_id in episode_ids:
        usage = episode_usage(episode_id)
        for slug, versions in usage["characters"].items():
            entry = characters.setdefault(slug, {"episodes": 0, "versions": {}})
            entry["episodes"] += 1
            for version in versions:
                entry["versions"][version] = entry["versions"].get(version, 0) + 1
        for slug in {*usage["places"], *usage["props"]}:
            items[slug] = items.get(slug, 0) + 1
    return {"characters": characters, "items": items}


def unlocked_cast(episode_id: str, raw: Any = None) -> list[dict]:
    """The characters an episode in a Universe uses whose look is not locked, in the order they
    first appear: [{"character", "character_id", "name", "version"}]. ``raw``: a shots.json to check
    instead of the episode's own. Empty for an episode outside a Universe and one without a readable
    shots.json; characters or versions the Universe does not have are left to the run's own cast
    checks."""
    from hfvg.shot_data import parse_shots
    library = cast_store.library_for_episode(episode_id)
    if library[0] != "series":
        return []
    try:
        if raw is None:
            raw = json.loads((paths.episode_dir(episode_id) / "shots.json").read_text(encoding="utf-8"))
        plans = parse_shots(raw)
    except (OSError, ValueError):
        return []
    characters = {c["slug"]: c for c in cast_store.list_characters(library)}
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for plan in plans.values():
        for req in plan.get("cast") or []:
            character = characters.get(req["character"])
            if character is None:
                continue
            number = req["version"] or character["default_version"]
            version = next((v for v in character["versions"] if v["version"] == number), None)
            if version is None or version["locked"] or (character["slug"], number) in seen:
                continue
            seen.add((character["slug"], number))
            out.append({"character": character["slug"], "character_id": character["character_id"],
                        "name": character["name"], "version": number})
    return out


LOCK_BEFORE = {"start": "this video starts", "approve": "the shot plan is approved",
               "pick": "they are put in a shot"}


def lock_message(unlocked: list[dict], before: str = "approve") -> str:
    """Why a Universe episode waits (unlocked_cast is not empty): to start, to have its shot plan
    approved, or to put the characters in a shot while the run waits at the shot plan."""
    names = [c["name"] + (f" (look {c['version']})" if c["version"] != 1 else "") for c in unlocked]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return (f"Lock the look of {who} before {LOCK_BEFORE[before]}: this video is in a Universe, where every "
            "character keeps one locked look in every episode. Open Cast on this video's page, make the character "
            "sheet and lock it.")


# ------------------------------------------------------------------------------------- the Universe list

COVER_KINDS = {"ci_": "cast", "pi_": "prop", "ui_": "reference", "bi_": "board"}
_COVER_OWNERS = {  # (image_id, series id) of the active pictures of each kind among "{}"
    "ci_": "SELECT i.image_id, c.library_id FROM cast_images i JOIN cast_characters c ON c.character_id = i.character_id "
           "WHERE i.status = 'active' AND c.library_kind = 'series' AND i.image_id IN ({})",
    "pi_": "SELECT i.image_id, p.library_id FROM prop_images i JOIN prop_items p ON p.prop_id = i.prop_id "
           "WHERE i.status = 'active' AND p.library_kind = 'series' AND i.image_id IN ({})",
    "ui_": "SELECT image_id, series_id FROM universe_photos WHERE status = 'active' AND image_id IN ({})",
    "bi_": "SELECT i.image_id, b.series_id FROM universe_board_images i JOIN universe_boards b "
           "ON b.board_id = i.board_id WHERE i.image_id IN ({})",
}


def _rows_in(conn, sql: str, values: list) -> list:
    """Rows of ``sql`` with its "{}" filled by placeholders for ``values``, a few hundred at a time
    (callers group rows by a key that is in one part)."""
    rows: list = []
    for start in range(0, len(values), 500):
        part = list(values[start:start + 500])
        rows += conn.execute(sql.format(", ".join("?" for _ in part)), part).fetchall()
    return rows


def _first_by_key(rows: list) -> dict:
    first: dict = {}
    for *key, value in rows:
        first.setdefault(tuple(key) if len(key) > 1 else key[0], value)
    return first


def _covers(conn, series_ids: list[str], profiles: dict, characters: dict, places: dict) -> dict[str, dict | None]:
    """Each card's picture: the chosen cover, else the first locked character's main picture, else
    the first place's main photo, else the approved mood board's first image. A fixed number of
    queries for any number of Universes."""
    chosen = {sid: profiles[sid]["cover_image_id"] for sid in series_ids
              if profiles[sid]["cover_image_id"] and profiles[sid]["cover_image_id"][:3] in COVER_KINDS}
    valid: set[tuple[str, str]] = set()
    for prefix, sql in _COVER_OWNERS.items():
        ids = sorted({c for c in chosen.values() if c.startswith(prefix)})
        valid |= {(r[1], r[0]) for r in _rows_in(conn, sql, ids)}
    locked = sorted({c["character_id"] for sid in series_ids for c in characters[sid] if c["locked_version"] is not None})
    cast_first = _first_by_key(_rows_in(
        conn, "SELECT character_id, version, image_id FROM cast_images WHERE status = 'active' AND character_id IN ({}) "
              "ORDER BY role = 'master' DESC, (role = 'upload' AND is_primary = 1) DESC, created_at, image_id", locked))
    photo_first = _first_by_key(_rows_in(
        conn, "SELECT prop_id, image_id FROM prop_images WHERE status = 'active' AND prop_id IN ({}) "
              "ORDER BY is_primary DESC, created_at, image_id", sorted({p for sid in series_ids for p in places[sid]})))
    board_first = _first_by_key(_rows_in(
        conn, "SELECT b.series_id, i.image_id FROM universe_board_images i JOIN universe_boards b "
              "ON b.board_id = i.board_id WHERE b.status = 'approved' AND b.series_id IN ({}) "
              "ORDER BY b.version DESC, i.position", list(series_ids)))
    out: dict[str, dict | None] = {}
    for sid in series_ids:
        cover = chosen.get(sid)
        if cover and (sid, cover) in valid:
            out[sid] = {"image_id": cover, "kind": COVER_KINDS[cover[:3]]}
            continue
        cast = next((cast_first[(c["character_id"], c["locked_version"])] for c in characters[sid]
                     if (c["character_id"], c["locked_version"]) in cast_first), None)
        photo = next((photo_first[p] for p in places[sid] if p in photo_first), None)
        out[sid] = ({"image_id": cast, "kind": "cast"} if cast else {"image_id": photo, "kind": "prop"} if photo
                    else {"image_id": board_first[sid], "kind": "board"} if sid in board_first else None)
    return out


def summaries(series_ids: list[str]) -> dict[str, dict]:
    """For each series: its profile, counts (characters, characters with a locked look, locked
    versions, places, props) and cover, in a fixed number of queries for any number of series."""
    out: dict[str, dict] = {}
    if not series_ids:
        return out
    series_ids = list(series_ids)
    with connect() as conn:
        profiles = {r["series_id"]: r for r in _rows_in(conn, "SELECT * FROM series_profiles WHERE series_id IN ({})",
                                                        series_ids)}
        rows = _rows_in(
            conn, "SELECT c.library_id, c.character_id, c.created_at, c.slug, "
                  "(SELECT MIN(v.version) FROM cast_versions v WHERE v.character_id = c.character_id AND v.locked = 1) "
                  "AS locked_version, (SELECT COUNT(*) FROM cast_versions v WHERE v.character_id = c.character_id AND "
                  "v.locked = 1) AS locked_versions FROM cast_characters c WHERE c.library_kind = 'series' AND "
                  "c.library_id IN ({})", series_ids)
        items = _rows_in(conn, "SELECT library_id, prop_id, kind, created_at, slug FROM prop_items "
                               "WHERE library_kind = 'series' AND library_id IN ({})", series_ids)
        latest = _first_by_key(_rows_in(conn, "SELECT series_id, status FROM universe_boards WHERE series_id IN ({}) "
                                              "ORDER BY version DESC", series_ids))
        rows = sorted((dict(r) for r in rows), key=lambda r: (r["created_at"], r["slug"]))
        items = sorted((dict(r) for r in items), key=lambda r: (r["created_at"], r["slug"]))
        characters = {sid: [r for r in rows if r["library_id"] == sid] for sid in series_ids}
        places = {sid: [r["prop_id"] for r in items if r["library_id"] == sid and r["kind"] == "place"]
                  for sid in series_ids}
        views = {sid: _profile_view(sid, profiles.get(sid)) for sid in series_ids}
        covers = _covers(conn, series_ids, views, characters, places)
    for sid in series_ids:
        mine = characters[sid]
        out[sid] = {
            "profile": views[sid],
            "counts": {"characters": len(mine),
                       "locked_characters": sum(1 for c in mine if c["locked_version"] is not None),
                       "locked_versions": sum(c["locked_versions"] for c in mine),
                       "places": len(places[sid]),
                       "props": sum(1 for r in items if r["library_id"] == sid and r["kind"] == "prop")},
            "board_status": latest.get(sid),
            "cover": covers[sid],
        }
    return out


# ------------------------------------------------------------------------------------- draft context

def draft_context(series_id: str, *, vo_mode: str | None, include_private: bool) -> dict:
    """The short Universe context a draft is written with: its name, logline, (for its owner and
    admins) story bible and style notes, and its characters, places and props with their
    descriptions. For a dialogue card the Universe's characters are the script's speakers.
    Returns {"context", "names", "speakers"}."""
    series = cast_store.get_series(series_id)
    profile = get_profile(series_id)
    characters = cast_store.list_characters(("series", series_id))[:CONTEXT_CHARACTERS]
    items = prop_store.list_items(("series", series_id))
    lines = ["UNIVERSE (this episode belongs to it: keep its characters, places, props and look consistent)",
             f"Name: {series['name']}"]
    if profile["logline"]:
        lines.append(f"Logline: {profile['logline']}")
    if include_private and profile["bible"]:
        lines.append(f"Story bible: {_clip(profile['bible'], 1500)}")
    if include_private and profile["style_notes"]:
        lines.append(f"Style notes: {_clip(profile['style_notes'], 500)}")
    if characters:
        lines.append("Characters (when the story needs them, use these exact names in prompt_kit_json characters):")
        for c in characters:
            role = f" ({c['role']})" if c.get("role") else ""
            description = f": {_clip(c['description'], 300)}" if c.get("description") else ""
            lines.append(f"- {c['name']}{role}{description}")
    for kind, label in (("place", "Places (use them as sets when they fit)"), ("prop", "Props")):
        chosen = [i for i in items if i["kind"] == kind][:CONTEXT_ITEMS]
        if chosen:
            lines.append(f"{label}:")
            lines.extend(f"- {i['name']}" + (f": {_clip(i['description'], 200)}" if i.get("description") else "")
                         for i in chosen)
    names = [c["name"] for c in characters]
    speakers: list[str] = []
    if vo_mode == "dialogue" and names:
        speakers = names[:2]
        lines.append('Dialogue: write every "audio_script" line with "speaker" set to one of these Universe '
                     f"character names: {', '.join(speakers)}.")
    return {"context": "\n".join(lines), "names": names, "speakers": speakers}
