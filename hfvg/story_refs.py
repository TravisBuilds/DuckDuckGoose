"""A Universe's story and the reference checklist read from it (docs/WORLDS.md, "Story first").

The owner writes the story first, in plain words; rough is fine. The language model reads it and
lists the references the studio needs to draw it: each character (face and outfit), each place and
each key prop, with why it is needed, how many photos are ideal, the fewest that work, and whether it
is optional. Photos upload onto an item. They are the Universe's reference photos
(hfvg.world_store), so the mood board uses them as before. The owner can add and remove items and
make the list again: items that have photos and items the owner added stay.

Tables from the universe story migration (hfvg.migrations.UNIVERSE_STORY_MIGRATION). A Universe
without a story reads as one made before stories: its notes and photos work as they always did.

Sync sqlite3 like the cast store; the API calls these through a worker thread.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from hfvg import cast_store, prop_store, world_store
from hfvg.cast_store import (
    CastConflict,
    CastError,
    CastNotFound,
    _clean,
    _new_id,
    _now,
    _tx,
    audit,
    connect,
)

logger = logging.getLogger(__name__)

STORY_MAX = 4000
STORY_MIN_CHARS = 20
MAX_ITEMS = 16
MAX_PHOTOS_PER_ITEM = 6
NAME_MAX = cast_store.NAME_MAX
WHY_MAX = 300
SHOW_MAX = 200
ITEM_KINDS = ("character", "place", "prop")
KIND_LABELS = {"character": "Character", "place": "Place", "prop": "Prop"}
DEFAULTS = {
    "character": {"ideal": 3, "min": 1, "show": "The face from the front and from the side, and the full outfit"},
    "place": {"ideal": 2, "min": 1, "show": "A wide view, and one closer detail"},
    "prop": {"ideal": 2, "min": 1, "show": "The whole thing on a plain background, and a close-up"},
}
STORY_FIRST = "Write a few lines of the story first: who is in it, where it happens and what happens"
_KIND_WORDS = {"characters": "character", "person": "character", "people": "character", "places": "place",
               "location": "place", "setting": "place", "set": "place", "props": "prop", "object": "prop",
               "item": "prop", "thing": "prop"}


def story_sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------- the story

def _story_row(conn, series_id: str):
    return conn.execute("SELECT * FROM universe_stories WHERE series_id = ?", (series_id,)).fetchone()


def story_view(conn, series_id: str) -> dict:
    """The story ("" before one is written) and whether the checklist was made from it as it reads now."""
    row = _story_row(conn, series_id)
    body = row["body"] if row else ""
    made_from = row["list_story_sha"] if row else None
    return {"body": body, "written": bool(body), "updated_at": row["updated_at"] if row else None,
            "list_made": made_from is not None, "list_current": bool(body) and made_from == story_sha(body),
            "list_made_at": row["list_made_at"] if row else None}


def get_story(series_id: str) -> dict:
    with connect() as conn:
        world_store._series_row(conn, series_id)
        return story_view(conn, series_id)


def save_story(series_id: str, body: Any, actor: str | None = None) -> dict:
    """Keep the story as written (paragraphs kept, at most STORY_MAX characters). Free: nothing is made."""
    body = world_store._clean_block(body, "Story", STORY_MAX)
    now = _now()
    with connect() as conn, _tx(conn):
        world_store._series_row(conn, series_id)
        conn.execute("INSERT INTO universe_stories (series_id, body, updated_by, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?) ON CONFLICT(series_id) DO UPDATE SET body = excluded.body, "
                     "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                     (series_id, body, actor, now, now))
        audit(conn, actor, "universe_story_save", series_id=series_id, details={"chars": len(body)})
        return story_view(conn, series_id)


# ------------------------------------------------------------------------------------- the checklist

def _kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    kind = _KIND_WORDS.get(kind, kind)
    if kind not in ITEM_KINDS:
        raise CastError("Say what it is: a character, a place or a prop")
    return kind


def _whole(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _counts(kind: str, ideal: Any, minimum: Any) -> tuple[int, int]:
    """(ideal, minimum) photo counts: the kind's defaults for anything missing; ideal 1 to
    MAX_PHOTOS_PER_ITEM, minimum 0 up to the ideal."""
    ideal = _whole(ideal)
    ideal = DEFAULTS[kind]["ideal"] if ideal is None else min(max(ideal, 1), MAX_PHOTOS_PER_ITEM)
    minimum = _whole(minimum)
    minimum = min(DEFAULTS[kind]["min"], ideal) if minimum is None else min(max(minimum, 0), ideal)
    return ideal, minimum


def _item_view(row, photos: list[str]) -> dict:
    out = dict(row)
    out["optional"] = bool(out["optional"])
    out["photos"] = photos
    out["photo_count"] = len(photos)
    out["ready"] = len(photos) >= max(out["min_photos"], 1)
    return out


def _items(conn, series_id: str) -> list[dict]:
    rows = conn.execute("SELECT * FROM universe_checklist_items WHERE series_id = ? AND status = 'active' "
                        "ORDER BY position, created_at", (series_id,)).fetchall()
    links = conn.execute(
        "SELECT l.item_id, l.image_id FROM universe_checklist_photos l "
        "JOIN universe_checklist_items i ON i.item_id = l.item_id AND i.series_id = ? AND i.status = 'active' "
        "JOIN universe_photos p ON p.image_id = l.image_id AND p.status = 'active' "
        "ORDER BY l.added_at, l.image_id", (series_id,)).fetchall()
    photos: dict[str, list[str]] = {}
    for item_id, image_id in links:
        photos.setdefault(item_id, []).append(image_id)
    return [_item_view(r, photos.get(r["item_id"], [])) for r in rows]


def progress(items: list[dict]) -> dict:
    """How far the list is: required items with enough photos ("4 of 7 ready"), and the optional ones."""
    required = [i for i in items if not i["optional"]]
    optional = [i for i in items if i["optional"]]
    return {"ready": sum(1 for i in required if i["ready"]), "required": len(required),
            "optional": len(optional), "optional_ready": sum(1 for i in optional if i["ready"]),
            "photos": sum(i["photo_count"] for i in items)}


def checklist(series_id: str) -> dict:
    with connect() as conn:
        world_store._series_row(conn, series_id)
        items = _items(conn, series_id)
    return {"items": items, "progress": progress(items)}


def names(conn, series_id: str) -> dict[str, list[str]]:
    """The names on the active list by kind, in list order (for the mood board and the bible)."""
    out: dict[str, list[str]] = {kind: [] for kind in ITEM_KINDS}
    for kind, name in conn.execute("SELECT kind, name FROM universe_checklist_items WHERE series_id = ? AND "
                                   "status = 'active' ORDER BY position, created_at", (series_id,)):
        out[kind].append(name)
    return out


def item_for_photo(conn, series_id: str, item_id: str) -> dict:
    """The active item a photo is uploaded onto; refused when it has MAX_PHOTOS_PER_ITEM photos."""
    row = conn.execute("SELECT * FROM universe_checklist_items WHERE item_id = ? AND series_id = ? AND "
                       "status = 'active'", (item_id, series_id)).fetchone()
    if not row:
        raise CastNotFound(f"Item {item_id} is not on the reference list")
    count = conn.execute("SELECT COUNT(*) FROM universe_checklist_photos l JOIN universe_photos p ON "
                         "p.image_id = l.image_id AND p.status = 'active' WHERE l.item_id = ?",
                         (item_id,)).fetchone()[0]
    if count >= MAX_PHOTOS_PER_ITEM:
        raise CastConflict(f"{row['name']} has {count} photos (the limit is {MAX_PHOTOS_PER_ITEM}); "
                           "remove one first")
    return dict(row)


def link_photo(conn, item_id: str, image_id: str, actor: str | None) -> None:
    conn.execute("INSERT OR IGNORE INTO universe_checklist_photos (item_id, image_id, added_by, added_at) "
                 "VALUES (?, ?, ?, ?)", (item_id, image_id, actor, _now()))


def add_item(series_id: str, kind: Any, name: Any, *, why: Any = "", show: Any = "", ideal_photos: Any = None,
             min_photos: Any = None, optional: bool = False, actor: str | None = None) -> dict:
    """An item the owner adds by hand (kept when the list is made again)."""
    kind = _kind(kind)
    name = _clean(name, "Name", NAME_MAX, required=True)
    why = _clean(why, "Why", WHY_MAX)
    show = _clean(show, "What to show", SHOW_MAX) or DEFAULTS[kind]["show"]
    ideal, minimum = _counts(kind, ideal_photos, min_photos)
    now = _now()
    item_id = _new_id("rc")
    with connect() as conn, _tx(conn):
        world_store._series_row(conn, series_id)
        current = _items(conn, series_id)
        if any(i["kind"] == kind and i["name"].lower() == name.lower() for i in current):
            raise CastConflict(f"{name} is on the list already")
        if len(current) >= MAX_ITEMS:
            raise CastConflict(f"The list has {len(current)} items (the limit is {MAX_ITEMS}); remove one first")
        position = max((i["position"] for i in current), default=-1) + 1
        conn.execute("INSERT INTO universe_checklist_items (item_id, series_id, kind, name, why, show, ideal_photos, "
                     "min_photos, optional, position, source, status, created_by, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'owner', 'active', ?, ?, ?)",
                     (item_id, series_id, kind, name, why, show, ideal, minimum, int(bool(optional)), position,
                      actor, now, now))
        audit(conn, actor, "universe_checklist_add", series_id=series_id,
              details={"item_id": item_id, "kind": kind, "name": name})
        return next(i for i in _items(conn, series_id) if i["item_id"] == item_id)


def attach_to_library(series_id: str, created: dict[str, list[str]], actor: str | None = None) -> dict[str, int]:
    """After the bible: the photos on a list item go to the character, place or prop the bible just
    made under the same name (a character's first version, a place's or prop's photos) with the
    rights attestation they were uploaded with, so nothing is uploaded twice. Returns how many photos
    each name got. A photo the library refuses (its limit, a missing file) is left out."""
    kinds = {"characters": "character", "places": "place", "props": "prop"}
    with connect() as conn:
        items = {(i["kind"], i["name"].lower()): i for i in _items(conn, series_id) if i["photos"]}
        photos = {r["image_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM universe_photos WHERE series_id = ? AND status = 'active'", (series_id,))}
        owners = {("character", r[1].lower()): r[0] for r in conn.execute(
            "SELECT character_id, name FROM cast_characters WHERE library_kind = 'series' AND library_id = ?",
            (series_id,))}
        owners.update({(r[1], r[2].lower()): r[0] for r in conn.execute(
            "SELECT prop_id, kind, name FROM prop_items WHERE library_kind = 'series' AND library_id = ?",
            (series_id,))})
    out: dict[str, int] = {}
    for key, kind in kinds.items():
        for name in created.get(key) or []:
            item = items.get((kind, name.lower()))
            owner = owners.get((kind, name.lower()))
            if item is None or owner is None:
                continue
            for image_id in item["photos"]:
                photo = photos.get(image_id)
                if photo is None:
                    continue
                try:
                    data = cast_store.image_file(photo["sha256"], photo["ext"]).read_bytes()
                    attestation = json.loads(photo["attestation"] or "{}")
                    if kind == "character":
                        cast_store.add_upload(owner, 1, data, filename=photo["original_filename"],
                                              attestation=attestation, actor=actor)
                    else:
                        prop_store.add_photo(owner, data, filename=photo["original_filename"],
                                             attestation=attestation, actor=actor)
                except (CastError, OSError, ValueError) as e:
                    logger.warning("reference photo %s not added to %s: %s", image_id, name, e)
                    continue
                out[name] = out.get(name, 0) + 1
    return out


def remove_item(series_id: str, item_id: str, actor: str | None = None) -> dict:
    """Take an item off the list. Its photos stay with the Universe's references."""
    with connect() as conn, _tx(conn):
        row = conn.execute("SELECT * FROM universe_checklist_items WHERE item_id = ? AND series_id = ? AND "
                           "status = 'active'", (item_id, series_id)).fetchone()
        if not row:
            raise CastNotFound(f"Item {item_id} is not on the reference list")
        conn.execute("UPDATE universe_checklist_items SET status = 'removed', updated_at = ? WHERE item_id = ?",
                     (_now(), item_id))
        conn.execute("DELETE FROM universe_checklist_photos WHERE item_id = ?", (item_id,))
        audit(conn, actor, "universe_checklist_remove", series_id=series_id,
              details={"item_id": item_id, "kind": row["kind"], "name": row["name"]})
    return {"removed": item_id}


# ------------------------------------------------------------------------------------- reading the story

def checklist_inputs(series_id: str) -> dict:
    """What the list is made from: the story, the logline and the names the Universe has already.
    Refused before the story has a few lines."""
    with connect() as conn:
        series = world_store._series_row(conn, series_id)
        story = story_view(conn, series_id)
        profile = world_store._profile_view(series_id, conn.execute(
            "SELECT * FROM series_profiles WHERE series_id = ?", (series_id,)).fetchone())
        characters = [r[0] for r in conn.execute("SELECT name FROM cast_characters WHERE library_kind = 'series' "
                                                 "AND library_id = ? ORDER BY created_at", (series_id,))]
        things = conn.execute("SELECT kind, name FROM prop_items WHERE library_kind = 'series' AND library_id = ? "
                              "ORDER BY created_at", (series_id,)).fetchall()
    if len(story["body"]) < STORY_MIN_CHARS:
        raise CastConflict(STORY_FIRST)
    return {"series_id": series_id, "name": series["name"], "logline": profile["logline"],
            "story": story["body"], "story_sha": story_sha(story["body"]),
            "existing": {"characters": characters, "places": [r[1] for r in things if r[0] == "place"],
                         "props": [r[1] for r in things if r[0] == "prop"]}}


def checklist_messages(inputs: dict) -> list[dict]:
    """The language model request for the reference checklist (only the owner and admins see it)."""
    system = (
        "You plan the reference photos a short-video studio needs before it can draw an original story. Read "
        f'the story and return a JSON object {{"items": [...]}} with at most {MAX_ITEMS} items, in the order '
        'they matter. Each item has "kind" ("character", "place" or "prop"), "name" (as the story names it; '
        'give an unnamed one a short plain name), "why" (one sentence: what it does in the story and why a '
        'photo helps), "show" (what the photos should show: for a character the face from the front and the '
        "side and the full outfit; for a place a wide view and a detail; for a prop the whole thing and a "
        f'close-up), "ideal_photos" (1 to {MAX_PHOTOS_PER_ITEM}), "min_photos" (0 up to ideal_photos) and '
        '"optional" (true for things that only pass by). List every character who appears, every place where '
        "a scene happens and the props the story turns on. Leave out things anyone can draw without a photo "
        "(a chair, the sky).\n"
        "CONTENT POLICY (CRITICAL): only original characters, places and things. No named IP, franchises, brands, "
        "celebrities or real people; turn any such mention into an original homage with a new name.")
    lines = [f"Universe: {inputs['name']}"]
    if inputs.get("logline"):
        lines.append(f"Logline: {inputs['logline']}")
    lines.append(f"Story (from the owner, rough is fine):\n{inputs['story']}")
    existing = inputs["existing"]
    if any(existing.values()):
        lines.append("Already in the Universe (list them too if the story uses them, with the same names): "
                     + ", ".join([*existing["characters"], *existing["places"], *existing["props"]]))
    lines.append("List the references as JSON.")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n".join(lines)}]


_NOT_NAMES = {
    "a", "an", "and", "after", "all", "as", "at", "because", "before", "but", "by", "each", "every", "for", "from",
    "he", "her", "here", "his", "how", "i", "if", "in", "into", "it", "its", "later", "meanwhile", "my", "no",
    "not", "now", "of", "on", "once", "one", "or", "our", "she", "so", "some", "soon", "that", "the", "their",
    "then", "there", "these", "they", "this", "those", "to", "today", "tonight", "two", "until", "we", "what",
    "when", "where", "while", "who", "why", "with", "you", "your",
}
_CAPITALISED = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b")
_PLACE = re.compile(r"\b(?:in|at|to|into|inside|under|across|through|from)\s+the\s+((?:[a-z]+\s+){0,2}[a-z]+)")
_PROP = re.compile(r"\b(?:finds|carries|holds|opens|loses|keeps|sells|mends|wears|with)\s+(?:a|an|the|her|his|their)"
                   r"\s+((?:[a-z]+\s+)?[a-z]+)")
_ENDS = {"and", "with", "where", "when", "while", "who", "that", "as", "of", "for", "to", "at", "in", "on", "but",
         "so", "she", "he", "they", "it"}


def _phrase(words: str) -> str:
    kept = []
    for word in words.split():
        if word in _ENDS:
            break
        kept.append(word)
    return " ".join(kept)


def _distinct(phrases) -> list[str]:
    """The first of each thing ("brass lantern" then "the lantern" is one lantern)."""
    out: list[str] = []
    for phrase in phrases:
        if phrase and not any(o.split()[-1] == phrase.split()[-1] for o in out):
            out.append(phrase)
    return out


def fake_checklist(inputs: dict) -> dict:
    """Deterministic DRAFT_LLM_FAKE list (test/e2e only): the capitalised names in the story as
    characters, "in/at/to the ..." as places and "finds/carries/... a ..." as props."""
    story = inputs["story"]
    people: list[str] = []
    for match in _CAPITALISED.finditer(story):
        words = match.group(0).split()
        while words and words[0].lower() in _NOT_NAMES:
            words.pop(0)
        name = " ".join(words)
        if not name:
            continue
        first = name.split()[0].lower()
        same = next((i for i, p in enumerate(people) if p.split()[0].lower() == first), None)
        if same is None:
            people.append(name)
        elif len(name) > len(people[same]):
            people[same] = name
    places = _distinct(_phrase(m.group(1)) for m in _PLACE.finditer(story))
    props = _distinct(_phrase(m.group(1)) for m in _PROP.finditer(story))
    items = [{"kind": "character", "name": name, "ideal_photos": 3, "min_photos": 1, "optional": False,
              "why": f"{name} is in the story: photos keep the face and the outfit the same in every shot."}
             for name in people[:6]]
    items += [{"kind": "place", "name": place[:1].upper() + place[1:], "ideal_photos": 2, "min_photos": 1,
               "optional": False, "why": f"A scene happens at the {place}."} for place in places[:4]]
    items += [{"kind": "prop", "name": prop[:1].upper() + prop[1:], "ideal_photos": 2, "min_photos": 1,
               "optional": k > 0, "why": f"The story turns on the {prop}."} for k, prop in enumerate(props[:4])]
    return {"items": items}


def parse_checklist(raw: Any) -> list[dict]:
    """The model's list, cleaned: known kinds only, names unique per kind (case-insensitive), texts
    clipped, photo counts within limits (the kind's defaults when missing), at most MAX_ITEMS."""
    raw = raw if isinstance(raw, dict) else {}
    entries = raw.get("items") if isinstance(raw.get("items"), list) else []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def text(value: Any, limit: int) -> str:
        return world_store._clip(value if isinstance(value, str) else "", limit)

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            kind = _kind(entry.get("kind"))
        except CastError:
            continue
        name = text(entry.get("name"), NAME_MAX)
        if not name or (kind, name.lower()) in seen:
            continue
        seen.add((kind, name.lower()))
        ideal, minimum = _counts(kind, entry.get("ideal_photos"), entry.get("min_photos"))
        out.append({"kind": kind, "name": name, "why": text(entry.get("why"), WHY_MAX),
                    "show": text(entry.get("show"), SHOW_MAX) or DEFAULTS[kind]["show"],
                    "ideal_photos": ideal, "min_photos": minimum, "optional": entry.get("optional") is True})
        if len(out) >= MAX_ITEMS:
            break
    return out


def apply_checklist(series_id: str, items: list[dict], *, story: str, actor: str | None = None) -> dict:
    """Make the list from the story as it was read (``story``: its sha256). Items that are on the new
    list stay (a story item takes the new why, what to show and photo counts); a story item that is not
    on it and has no photo goes; items with photos and items the owner added stay. One transaction."""
    now = _now()
    with connect() as conn, _tx(conn):
        world_store._series_row(conn, series_id)
        row = _story_row(conn, series_id)
        if row is None or story_sha(row["body"]) != story:
            raise CastConflict("The story changed while the list was being made; make the list again")
        current = _items(conn, series_id)
        wanted = {(i["kind"], i["name"].lower()): i for i in items}
        added: list[str] = []
        kept: list[str] = []
        removed: list[str] = []
        have: set[tuple[str, str]] = set()
        for item in current:
            key = (item["kind"], item["name"].lower())
            new = wanted.get(key)
            if new is not None and item["source"] == "story":
                conn.execute("UPDATE universe_checklist_items SET why = ?, show = ?, ideal_photos = ?, min_photos = ?, "
                             "optional = ?, updated_at = ? WHERE item_id = ?",
                             (new["why"], new["show"], new["ideal_photos"], new["min_photos"], int(new["optional"]),
                              now, item["item_id"]))
            if new is None and item["source"] == "story" and item["photo_count"] == 0:
                conn.execute("UPDATE universe_checklist_items SET status = 'removed', updated_at = ? WHERE item_id = ?",
                             (now, item["item_id"]))
                removed.append(item["name"])
                continue
            kept.append(item["name"])
            have.add(key)
        position = max((i["position"] for i in current), default=-1) + 1
        for new in items:
            key = (new["kind"], new["name"].lower())
            if key in have or len(have) >= MAX_ITEMS:
                continue
            conn.execute("INSERT INTO universe_checklist_items (item_id, series_id, kind, name, why, show, ideal_photos, "
                         "min_photos, optional, position, source, status, created_by, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'story', 'active', ?, ?, ?)",
                         (_new_id("rc"), series_id, new["kind"], new["name"], new["why"], new["show"],
                          new["ideal_photos"], new["min_photos"], int(new["optional"]), position, actor, now, now))
            position += 1
            have.add(key)
            added.append(new["name"])
        conn.execute("UPDATE universe_stories SET list_story_sha = ?, list_made_at = ? WHERE series_id = ?",
                     (story, now, series_id))
        audit(conn, actor, "universe_checklist_make", series_id=series_id,
              details={"added": added, "kept": kept, "removed": removed, "story_sha": story})
        result = _items(conn, series_id)
    return {"added": added, "kept": kept, "removed": removed, "items": result, "progress": progress(result)}
