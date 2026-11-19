"""Universe routes (docs/WORLDS.md): the Universes a member may see, one Universe with its
characters, places, props and episodes, its profile and saved voices, its creation references, mood
board and bible, and changes to its library without going through an episode. Mounted by
api/main.py under the app-wide guard (``require_admin_auth``); every route here allows members.

Who may see a Universe: admins, its owner and whoever may change one of its episodes (the series
rule of the cast library, api/cast_routes.py). Everyone else gets the 404 a missing Universe gets,
so a refusal does not tell whether it exists. Who may change it: its owner or an admin (only an
admin while it has no owner). Its story bible, style notes, creation references, mood board
prompts and saved voices are shown only to them.

Library changes here are the episode routes' (the same checks, limits, rights attestation,
private storage and audit) on the library ("series", id). No character sheets are made here: a look
is locked from an episode in the Universe, where the paid sheet steps and their estimates live.
Deletes are refused while any episode of the Universe is being made.

The mood board is the only paid step of making a Universe: its estimate comes first and a board is
refused when the estimate is above the approved total. Dry-run mode (DRY_RUN=true) makes free
placeholder boards here. A live board's estimate is the image provider's own price for each picture
(hfvg.universe_boards plans them; reference photos go with each picture as the still model allows);
the API checks the approval, the room left on the Universe's mood board budget line and the balance
gate, then the worker makes the pictures (UniverseBoardWorkflow, on its own task queue) and checks
each picture's price, the balance gate and the budget line again before paying. A worker that does
not poll that queue (one older than live boards) gets the owner a plain 409 before anything is asked
or charged. The bible is written by the language model from the approved board and the references
(DRAFT_LLM_FAKE=1: a deterministic test bible, dry-run only).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Cookie, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hfvg import cast_store, prop_store, universe_boards, world_store

try:  # "api.cast_routes" in tests and the e2e backend; "cast_routes" when uvicorn runs from api/
    from api import cast_routes as cast_api
    from api import prop_routes as prop_api
except ImportError:
    import cast_routes as cast_api
    import prop_routes as prop_api

router = APIRouter()
logger = logging.getLogger(__name__)

CHARACTER_ID_RE = re.compile(r"^ch_[0-9a-f]{12}$")
NOTE_ID_RE = re.compile(r"^un_[0-9a-f]{12}$")
BOARD_ID_RE = re.compile(r"^mb_[0-9a-f]{12}$")
UNIVERSE_IMAGE_RE = re.compile(r"^(?:ui|bi)_[0-9a-f]{12}$")
VIDEO_FILES = ("phone", "master_subs", "master_nosubs")
# The task queue the API starts studio workflows on (live mood boards: its "-universe" queue).
BOARD_TASK_QUEUE = "hfvg-tasks"
BOARD_START_GRACE = timedelta(minutes=2)
# Far above a board's own limits (four pictures of at most 12 minutes each): a board no worker
# picks up (one started just as the worker was rolled back) ends instead of waiting forever.
BOARD_RUN_TIMEOUT = timedelta(hours=2)
ESTIMATE_REQUEST_ID = "mr_000000000000"
BOARD_WORKER_MISSING = ("Mood boards are made by the studio's worker, and the worker running now cannot make them yet. "
                        "Nothing was charged. Ask an admin to update the worker, then try again.")
BOARD_WORKER_UNREACHABLE = ("The studio's worker cannot be reached right now, so no mood board was started. Nothing "
                            "was charged; try again in a minute.")
BOARD_NOT_STARTED = ("The studio's worker could not be asked to make the mood board, so nothing was made or "
                     "charged. Try again in a minute.")
COVER_ROUTES = {"cast": "/api/cast/images/", "prop": "/api/props/photos/", "reference": "/api/worlds/images/",
                "board": "/api/worlds/images/"}

CharacterCreateRequest = cast_api.CharacterCreateRequest
CharacterUpdateRequest = cast_api.CharacterUpdateRequest
CharacterDeleteRequest = cast_api.CharacterDeleteRequest
PropCreateRequest = prop_api.PropCreateRequest
PropUpdateRequest = prop_api.PropUpdateRequest


def register(app) -> None:
    """Add these routes to the app itself, like the cast routes (api/cast_routes.register)."""
    for route in router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)


_call = cast_api._call


def _hidden(series_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Universe {series_id} not found")


# ------------------------------------------------------------------------------------- who may

async def _viewer(series_id: str, authorization: str | None, cookie: str | None) -> dict:
    """The series with the caller's (actor, user_id, is_admin, can_edit) for a caller who may see
    the Universe; the 404 a missing Universe gets otherwise."""
    actor, user_id, is_admin = await cast_api._actor(authorization, cookie)
    try:
        series = await asyncio.to_thread(cast_store.get_series, series_id)
    except cast_store.CastNotFound:
        raise _hidden(series_id) from None
    if not await cast_api._may_see_series(series, user_id, is_admin):
        raise _hidden(series_id)
    return {"series": series, "actor": actor, "user_id": user_id, "is_admin": is_admin,
            "can_edit": is_admin or cast_api._owns_series(series, user_id)}


async def _editor(series_id: str, authorization: str | None, cookie: str | None) -> dict:
    """For a change: the 404 of a Universe the caller may not see, then 403 unless the caller is
    its owner or an admin (the series rule of api/cast_routes._require_series_owner: a Universe
    without an owner changes only through an admin)."""
    who = await _viewer(series_id, authorization, cookie)
    if who["can_edit"]:
        return who
    name = who["series"]["name"]
    if cast_api._series_owner(who["series"]) is None:
        raise HTTPException(status_code=403, detail=f"Universe {name} has no owner; only an admin can change it")
    raise HTTPException(status_code=403, detail=(
        f"Universe {name} belongs to another member; only its owner or an admin can change it"))


async def _refuse_during_runs(series: dict, what: str) -> None:
    """409 while an episode of the Universe is being made: the run planned its references from it."""
    api = cast_api._api()
    for episode_id in series["episodes"]:
        run = await api._open_episode_run(episode_id)
        if run:
            raise HTTPException(status_code=409, detail=(
                f"Episode {episode_id} is being made right now (run {run['run_id']}); {what} after that run ends"))


async def _character_of(series_id: str, character_id: str) -> dict:
    if not CHARACTER_ID_RE.match(character_id):
        raise HTTPException(status_code=404, detail=f"Character {character_id} not found")
    character = await _call(cast_store.get_character, character_id)
    if (character["library_kind"], character["library_id"]) != ("series", series_id):
        raise HTTPException(status_code=404, detail=f"Character {character_id} is not in this Universe")
    return character


async def _item_of(series_id: str, prop_id: str) -> dict:
    if not prop_api.PROP_ID_RE.match(prop_id):
        raise HTTPException(status_code=404, detail=f"Prop or place {prop_id} not found")
    item = await _call(prop_store.get_item, prop_id)
    if (item["library_kind"], item["library_id"]) != ("series", series_id):
        raise HTTPException(status_code=404, detail=f"Prop or place {prop_id} is not in this Universe")
    return item


async def _episode_rows(episode_ids: list[str]) -> dict[str, tuple]:
    """(user_id, created_at) of each episode with a row, by the id the caller passed (the
    normalization of api.main.episode_owner)."""
    ids = {e: cast_api._api().episode_workflow_id(e) for e in episode_ids}
    rows = await _studio_rows_in("SELECT episode_id, user_id, created_at FROM episodes WHERE episode_id IN",
                                 sorted(set(ids.values())))
    by_id = {r[0]: (r[1], r[2]) for r in rows}
    return {e: by_id[ids[e]] for e in episode_ids if ids[e] in by_id}


async def _episode_owners(episode_ids: list[str]) -> dict[str, str | None]:
    """The owner of each episode (None: no owner), the rule of api.main.episode_owner in a fixed
    number of queries."""
    from hfvg.users import LEGACY_USER_ID
    rows = await _episode_rows(episode_ids)
    owners = {e: row[0] for e, row in rows.items()}
    return {e: None if owners.get(e) in (None, "", LEGACY_USER_ID) else owners[e] for e in episode_ids}


async def _studio_rows_in(sql: str, values: list[str], chunk: int = 500) -> list[tuple]:
    """Rows of ``sql`` + " (?, ?, ...)" over ``values``, a few hundred at a time."""
    rows: list[tuple] = []
    for start in range(0, len(values), chunk):
        part = values[start:start + chunk]
        rows += await _studio_rows(f"{sql} ({', '.join('?' for _ in part)})", tuple(part))
    return rows


async def _studio_rows(sql: str, params: tuple) -> list[tuple]:
    """Rows of the studio DB; none when the table does not exist yet (503 on other DB errors)."""
    import aiosqlite
    try:
        async with aiosqlite.connect(cast_api._api().DATABASE_PATH) as db:
            return list(await (await db.execute(sql, params)).fetchall())
    except aiosqlite.OperationalError as e:
        if "no such table" in str(e):
            return []
        raise HTTPException(status_code=503, detail=f"studio database error: {e}") from None


async def _user_names(user_ids: set[str]) -> dict[str, str | None]:
    rows = await _studio_rows_in("SELECT user_id, name FROM users WHERE user_id IN", sorted(u for u in user_ids if u))
    return {r[0]: r[1] for r in rows}


# ------------------------------------------------------------------------------------- views

def _image_urls(cover: dict | None) -> dict | None:
    if not cover:
        return None
    base = COVER_ROUTES[cover["kind"]] + cover["image_id"]
    return {**cover, "url": base, "thumb_url": f"{base}/thumb"}


def _primary_file(episode_id: str) -> str | None:
    """The first finished video file of the episode (the phone version, else a master), or None."""
    api = cast_api._api()
    for file_type in VIDEO_FILES:
        try:
            api.review_file_path(episode_id, file_type)
            return file_type
        except HTTPException:
            continue
    return None


def _primary_files(episode_ids: list[str]) -> dict[str, str | None]:
    return {e: _primary_file(e) for e in episode_ids}


def _profile_out(profile: dict, can_edit: bool) -> dict:
    out = {k: profile[k] for k in ("logline", "preset_id", "cover_image_id", "updated_at")}
    if can_edit:
        out.update({k: profile[k] for k in ("bible", "style_notes", "narrator_voice", "bible_board_id",
                                            "bible_generated_at")})
    return out


def _world_card(series: dict, summary: dict, can_edit: bool, owner_names: dict, files: dict) -> dict:
    owner = cast_api._series_owner(series)
    finished = sum(1 for e in series["episodes"] if files.get(e))
    return {"series_id": series["series_id"], "name": series["name"], "owner_user_id": owner,
            "owner_name": owner_names.get(owner) if owner else None, "created_at": series.get("created_at"),
            "can_edit": can_edit, "profile": _profile_out(summary["profile"], can_edit),
            "counts": {**summary["counts"], "episodes": len(series["episodes"]), "finished_episodes": finished},
            "cover": _image_urls(summary["cover"]), "episodes": list(series["episodes"]),
            "board_status": summary["board_status"] if can_edit else None}


def _primary_image(character: dict) -> dict | None:
    """The picture that stands for a character: its default version's master, else its main upload,
    else its first picture."""
    version = next((v for v in character["versions"] if v["version"] == character["default_version"]), None)
    images = (version or {}).get("images") or []
    pick = (next((i for i in images if i["role"] == cast_store.MASTER_ROLE), None)
            or next((i for i in images if i.get("is_primary")), None) or (images[0] if images else None))
    return {"image_id": pick["image_id"], "url": pick["url"], "thumb_url": pick["thumb_url"]} if pick else None


async def _character_view(character: dict, usage: dict, voices: dict, can_edit: bool) -> dict:
    view = cast_api._character_out(character)
    versions = []
    for version in view["versions"]:
        problems = (await _call(cast_store.verify_lock, character["character_id"], version["version"])
                    if version["locked"] else [])
        versions.append({**version, "problems": problems})
    view["versions"] = versions
    use = usage["characters"].get(character["slug"]) or {"episodes": 0, "versions": {}}
    view.update({"locked_versions": [v["version"] for v in versions if v["locked"]],
                 "primary_image": _primary_image(view),
                 "used_in": {"episodes": use["episodes"], "versions": {str(k): n for k, n in use["versions"].items()}}})
    if can_edit:
        view["voice"] = voices.get(character["character_id"])
    return view


def _item_view(item: dict, usage: dict) -> dict:
    view = prop_api._item_out(item)
    view["primary_photo"] = ({"image_id": view["photos"][0]["image_id"], "url": view["photos"][0]["url"],
                              "thumb_url": view["photos"][0]["thumb_url"]} if view["photos"] else None)
    view["used_in"] = {"episodes": usage["items"].get(item["slug"], 0)}
    return view


def _universe_image_out(image_id: str) -> dict:
    return {"url": f"/api/worlds/images/{image_id}", "thumb_url": f"/api/worlds/images/{image_id}/thumb"}


def _board_out(board: dict) -> dict:
    out = {k: v for k, v in board.items() if k != "images"}
    out["images"] = [{**i, **_universe_image_out(i["image_id"])} for i in board["images"]]
    return out


def _reference_out(photo: dict) -> dict:
    out = {k: photo.get(k) for k in ("image_id", "caption", "width", "height", "bytes", "mime", "original_filename",
                                     "attestation", "created_by", "created_at")}
    return {**out, **_universe_image_out(photo["image_id"]),
            "size_note": cast_store.size_note(photo.get("width"), photo.get("height"))}


async def _episodes_view(episode_ids: list[str], user_id: str | None, is_admin: bool) -> list[dict]:
    created = {e: row[1] for e, row in (await _episode_rows(episode_ids)).items()}
    owners = await _episode_owners(episode_ids)
    names = await _user_names({o for o in owners.values() if o})
    files = await asyncio.to_thread(_primary_files, episode_ids)
    out = [{"episode_id": e, "created_at": created.get(e), "owner_user_id": owners.get(e),
            "owner_name": names.get(owners.get(e)) if owners.get(e) else None,
            "can_change": is_admin or owners.get(e) is None or owners.get(e) == user_id,
            "finished": files.get(e) is not None, "primary_file": files.get(e)} for e in episode_ids]
    return sorted(out, key=lambda e: (e["created_at"] or "", e["episode_id"]), reverse=True)


async def _creation_view(series_id: str) -> dict:
    """The owner's creation steps. ``board_request``: the live board being made (its pictures so
    far) or the last one that failed, with its reason. ``board.unavailable``: why a live board
    cannot be started now (the worker), before anything is estimated or charged."""
    await _refresh_board_requests(series_id)
    notes = await _call(world_store.list_notes, series_id)
    photos = await _call(world_store.list_reference_photos, series_id)
    boards = await _call(world_store.list_boards, series_id, with_prompt=True)
    request = await _call(world_store.open_board_request, series_id)
    dry = cast_api._dry_run()
    problem = None if dry else await _board_worker_problem()
    return {
        "notes": notes, "photos": [_reference_out(p) for p in photos], "boards": [_board_out(b) for b in boards],
        "board": {"available": problem is None, "image_count": world_store.BOARD_IMAGES, "dry_run": dry,
                  "unavailable": problem[1] if problem else None},
        "board_request": request,
        "topics": list(world_store.NOTE_TOPICS),
        "limits": {"max_notes": world_store.MAX_NOTES, "note_max": world_store.NOTE_MAX,
                   "max_photos": world_store.MAX_REFERENCE_PHOTOS, "caption_max": world_store.CAPTION_MAX,
                   "feedback_max": world_store.FEEDBACK_MAX},
    }


async def _world_view(who: dict) -> dict:
    series, can_edit = who["series"], who["can_edit"]
    series_id = series["series_id"]
    library = ("series", series_id)
    characters = await _call(cast_store.list_characters, library)
    items = await _call(prop_store.list_items, library)
    usage = await _call(world_store.used_in, series["episodes"])
    summary = (await _call(world_store.summaries, [series_id]))[series_id]
    voices = (await _call(world_store.character_voices, [c["character_id"] for c in characters])
              if can_edit else {})
    episodes = await _episodes_view(series["episodes"], who["user_id"], who["is_admin"])
    owner = cast_api._series_owner(series)
    names = await _user_names({owner} if owner else set())
    boards = await _call(world_store.list_boards, series_id)
    approved = next((b for b in reversed(boards) if b["status"] == "approved"), None)
    card = _world_card(series, summary, can_edit, names, {e["episode_id"]: e["primary_file"] for e in episodes})
    view = {
        "world": card,
        "can_edit": can_edit,
        "characters": [await _character_view(c, usage, voices, can_edit) for c in characters],
        "items": [_item_view(i, usage) for i in items],
        "episodes": episodes,
        "look": ({"board_id": approved["board_id"], "version": approved["version"],
                  "images": [{**i, **_universe_image_out(i["image_id"])} for i in approved["images"]]}
                 if approved else None),
        "limits": {"max_upload_bytes": cast_store.max_upload_bytes(), "min_image_px": cast_store.min_image_px(),
                   "formats": ["PNG", "JPEG", "WebP"], "max_uploads_per_version": cast_store.MAX_UPLOADS_PER_VERSION,
                   "max_characters": cast_store.MAX_CHARACTERS_PER_LIBRARY, **prop_api.limits(),
                   "logline_max": world_store.LOGLINE_MAX, "bible_max": world_store.BIBLE_MAX,
                   "style_notes_max": world_store.STYLE_NOTES_MAX},
        "attestation": {"rights": list(cast_store.RIGHTS_VALUES), "likeness_gate": cast_store.LIKENESS_CONSENT},
    }
    if can_edit:
        view["creation"] = await _creation_view(series_id)
    return view


# ------------------------------------------------------------------------------------- models

class WorldCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=cast_store.NAME_MAX)
    logline: str = Field("", max_length=world_store.LOGLINE_MAX)
    preset_id: str | None = Field(None, max_length=60)


class ProfileRequest(BaseModel):
    logline: str | None = Field(None, max_length=world_store.LOGLINE_MAX)
    bible: str | None = Field(None, max_length=world_store.BIBLE_MAX)
    style_notes: str | None = Field(None, max_length=world_store.STYLE_NOTES_MAX)
    preset_id: str | None = Field(None, max_length=60)
    cover_image_id: str | None = Field(None, max_length=20)


class VoiceSaveRequest(BaseModel):
    """A voice picked with POST /api/voices/pick (voice_id null clears the saved voice)."""
    voice_id: str | None = Field(None, max_length=64)
    name: str = Field("", max_length=world_store.VOICE_NAME_MAX)
    description: str = Field("", max_length=world_store.VOICE_DESCRIPTION_MAX)
    preview_url: str | None = Field(None, max_length=world_store.PREVIEW_URL_MAX)


class NoteRequest(BaseModel):
    topic: str = Field(..., max_length=20)
    body: str = Field(..., min_length=1, max_length=world_store.NOTE_MAX + 200)


class NoteUpdateRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=world_store.NOTE_MAX + 200)


class BoardRequest(BaseModel):
    feedback: str = Field("", max_length=world_store.FEEDBACK_MAX + 100)


class BoardGenerateRequest(BoardRequest):
    approved_total_usd_micros: int = Field(..., ge=0)


# ------------------------------------------------------------------------------------- universes

@router.get("/api/worlds")
async def list_worlds(authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """The Universes the caller may see: profile, counts, cover and episodes of each (a fixed number
    of queries for the whole list)."""
    _, user_id, is_admin = await cast_api._actor(authorization, studio_admin_token)
    rows = await _call(cast_store.list_series)
    owners = await _episode_owners([e for r in rows for e in r["episodes"]])
    visible = [r for r in rows if is_admin or cast_api._owns_series(r, user_id)
               or any(owners.get(e) is None or owners.get(e) == user_id for e in r["episodes"])]
    summaries = await _call(world_store.summaries, [r["series_id"] for r in visible])
    files = await asyncio.to_thread(_primary_files, [e for r in visible for e in r["episodes"]])
    names = await _user_names({o for o in (cast_api._series_owner(r) for r in visible) if o})
    return {"worlds": [_world_card(r, summaries[r["series_id"]], is_admin or cast_api._owns_series(r, user_id),
                                   names, files) for r in visible]}

list_worlds.allow_members = True


@router.post("/api/worlds")
async def create_world(request: WorldCreateRequest, authorization: str | None = Header(None),
                       studio_admin_token: str | None = Cookie(None)):
    """A new Universe owned by the caller (a Bearer admin request makes one without an owner).
    Nothing is generated or charged."""
    actor, user_id, _ = await cast_api._actor(authorization, studio_admin_token)
    cast_api._check_text(request.name, request.logline)
    made = await _call(world_store.create_universe, request.name, user_id, logline=request.logline,
                       preset_id=request.preset_id, actor=actor)
    return {"world": {"series_id": made["series"]["series_id"], "name": made["series"]["name"],
                      "owner_user_id": cast_api._series_owner(made["series"]), "profile": made["profile"]}}

create_world.allow_members = True


@router.get("/api/worlds/{series_id}")
async def get_world(series_id: str, authorization: str | None = Header(None),
                    studio_admin_token: str | None = Cookie(None)):
    """The Universe page: profile, characters (versions, locks, problems of a lock, main picture,
    the episodes that use each), props and places, episodes, the approved mood board and, for its
    owner and admins, the saved voices and the creation flow."""
    return await _world_view(await _viewer(series_id, authorization, studio_admin_token))

get_world.allow_members = True


@router.post("/api/worlds/{series_id}/profile")
async def update_world_profile(series_id: str, request: ProfileRequest, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """Change the logline, story bible, style notes, default video type or cover (only the fields
    sent; null clears the video type or the cover)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    sent = {k: getattr(request, k) for k in request.model_fields_set}
    cast_api._check_text(sent.get("logline"), sent.get("bible"), sent.get("style_notes"))
    for key in ("logline", "bible", "style_notes"):
        if key in sent and sent[key] is None:
            sent[key] = ""
    profile = await _call(world_store.update_profile, series_id, **sent, actor=who["actor"])
    return {"profile": _profile_out(profile, True)}

update_world_profile.allow_members = True


# ------------------------------------------------------------------------------------- characters

@router.post("/api/worlds/{series_id}/cast")
async def create_world_character(series_id: str, request: CharacterCreateRequest,
                                 authorization: str | None = Header(None),
                                 studio_admin_token: str | None = Cookie(None)):
    """Add a character to the Universe."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.name, request.role, request.description, request.outfit)
    character = await _call(cast_store.create_character, ("series", series_id), request.name, request.role,
                            request.description, request.slug or None, who["actor"], outfit=request.outfit)
    return {"character": cast_api._character_out(character)}

create_world_character.allow_members = True


@router.post("/api/worlds/{series_id}/cast/with-picture")
async def create_world_character_with_picture(
    series_id: str,
    image_file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=cast_store.NAME_MAX),
    role: str = Form("", max_length=cast_store.ROLE_MAX),
    description: str = Form("", max_length=cast_store.DESCRIPTION_MAX),
    rights: str = Form(""),
    depicts_real_person: str = Form("false"),
    likeness_consent: str = Form("false"),
    note: str = Form(""),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Add a character from a reference picture in one step (the episode route's checks)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(name, role, description)
    attestation = await _call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                              cast_api._flag(likeness_consent), note)
    data = await prop_api._read_photo(image_file)
    character = await _call(cast_store.create_character_with_upload, ("series", series_id), name, role, description,
                            data, filename=image_file.filename, attestation=attestation, actor=who["actor"])
    return {"character": cast_api._character_out(character)}

create_world_character_with_picture.allow_members = True


@router.post("/api/worlds/{series_id}/cast/{character_id}")
async def update_world_character(series_id: str, character_id: str, request: CharacterUpdateRequest,
                                 authorization: str | None = Header(None),
                                 studio_admin_token: str | None = Cookie(None)):
    """Edit a character's name, role or description."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    cast_api._check_text(request.name, request.role, request.description)
    character = await _call(cast_store.update_character, character_id, name=request.name, role=request.role,
                            description=request.description, actor=who["actor"])
    return {"character": cast_api._character_out(character)}

update_world_character.allow_members = True


@router.post("/api/worlds/{series_id}/cast/{character_id}/delete")
async def delete_world_character(series_id: str, character_id: str, request: CharacterDeleteRequest,
                                 authorization: str | None = Header(None),
                                 studio_admin_token: str | None = Cookie(None)):
    """Delete a character, its pictures and its saved voice (a locked one only by an admin with force
    and a reason). Refused while an episode of the Universe is being made."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    if request.force and not who["is_admin"]:
        raise HTTPException(status_code=403, detail="Only an admin can remove a character with locked sheets")
    await _refuse_during_runs(who["series"], "its characters can be deleted")
    return await _call(cast_store.delete_character, character_id, who["actor"], force=request.force,
                       reason=request.reason)

delete_world_character.allow_members = True


@router.post("/api/worlds/{series_id}/cast/{character_id}/versions/{version}/images")
async def upload_world_character_image(
    series_id: str,
    character_id: str,
    version: int,
    image_file: UploadFile = File(...),
    rights: str = Form(""),
    depicts_real_person: str = Form("false"),
    likeness_consent: str = Form("false"),
    note: str = Form(""),
    primary: str = Form("false"),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Upload a reference picture of a character version (the episode route's checks and limits)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    attestation = await _call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                              cast_api._flag(likeness_consent), note)
    data = await prop_api._read_photo(image_file)
    image = await _call(cast_store.add_upload, character_id, version, data, filename=image_file.filename,
                        attestation=attestation, actor=who["actor"], primary=cast_api._flag(primary))
    duplicate = image.pop("duplicate", False)
    return {"image": cast_api._image_out(image), "duplicate": duplicate}

upload_world_character_image.allow_members = True


async def _image_of(character_id: str, version: int, image_id: str) -> dict:
    if not cast_api.IMAGE_ID_RE.match(image_id):
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found")
    image = await _call(cast_store.get_image, image_id)
    if image["character_id"] != character_id or image["version"] != version:
        raise HTTPException(status_code=404, detail=f"Image {image_id} is not part of this version")
    return image


@router.post("/api/worlds/{series_id}/cast/{character_id}/versions/{version}/images/{image_id}/primary")
async def set_world_character_image_primary(series_id: str, character_id: str, version: int, image_id: str,
                                            authorization: str | None = Header(None),
                                            studio_admin_token: str | None = Cookie(None)):
    """Make an upload the version's main reference."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    await _image_of(character_id, version, image_id)
    image = await _call(cast_store.set_primary, character_id, version, image_id, who["actor"])
    return {"image": cast_api._image_out(image)}

set_world_character_image_primary.allow_members = True


@router.post("/api/worlds/{series_id}/cast/{character_id}/versions/{version}/images/{image_id}/delete")
async def delete_world_character_image(series_id: str, character_id: str, version: int, image_id: str,
                                       authorization: str | None = Header(None),
                                       studio_admin_token: str | None = Cookie(None)):
    """Delete an upload or an unlocked sheet picture. Refused while an episode of the Universe is
    being made."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    await _image_of(character_id, version, image_id)
    await _refuse_during_runs(who["series"], "its pictures can be deleted")
    return await _call(cast_store.delete_image, image_id, who["actor"], character_id=character_id)

delete_world_character_image.allow_members = True


@router.post("/api/worlds/{series_id}/cast/{character_id}/voice")
async def save_character_voice(series_id: str, character_id: str, request: VoiceSaveRequest,
                               authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """Save the voice picked for a character (or clear it with voice_id null). New episodes in the
    Universe start with it; no provider is called."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _character_of(series_id, character_id)
    cast_api._check_text(request.description)
    voice = await _call(world_store.set_character_voice, character_id, voice_id=request.voice_id or None,
                        voice_name=request.name, description=request.description, preview_url=request.preview_url,
                        actor=who["actor"])
    return {"character_id": character_id, "voice": voice}

save_character_voice.allow_members = True


@router.post("/api/worlds/{series_id}/narrator-voice")
async def save_narrator_voice(series_id: str, request: VoiceSaveRequest, authorization: str | None = Header(None),
                              studio_admin_token: str | None = Cookie(None)):
    """Save the Universe's narrator voice (or clear it with voice_id null); no provider is called."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.description)
    voice = await _call(world_store.set_narrator_voice, series_id, voice_id=request.voice_id or None,
                        voice_name=request.name, description=request.description, preview_url=request.preview_url,
                        actor=who["actor"])
    return {"series_id": series_id, "voice": voice}

save_narrator_voice.allow_members = True


# ------------------------------------------------------------------------------------- props and places

@router.post("/api/worlds/{series_id}/props")
async def create_world_prop(series_id: str, request: PropCreateRequest, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """Add a prop or a place to the Universe."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.name, request.description)
    item = await _call(prop_store.create_item, ("series", series_id), request.kind, request.name,
                       request.description, slug=request.slug or None, actor=who["actor"])
    return {"item": prop_api._item_out(item)}

create_world_prop.allow_members = True


@router.post("/api/worlds/{series_id}/props/with-photo")
async def create_world_prop_with_photo(
    series_id: str,
    image_file: UploadFile = File(...),
    kind: str = Form(..., max_length=10),
    name: str = Form(..., min_length=1, max_length=prop_store.NAME_MAX),
    description: str = Form("", max_length=prop_store.DESCRIPTION_MAX),
    rights: str = Form(""),
    depicts_real_person: str = Form("false"),
    likeness_consent: str = Form("false"),
    note: str = Form(""),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Add a prop or a place from a photo in one step (the episode route's checks)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(name, description)
    attestation = await _call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                              cast_api._flag(likeness_consent), note)
    data = await prop_api._read_photo(image_file)
    item = await _call(prop_store.create_item_with_upload, ("series", series_id), kind, name, description, data,
                       filename=image_file.filename, attestation=attestation, actor=who["actor"])
    return {"item": prop_api._item_out(item)}

create_world_prop_with_photo.allow_members = True


@router.post("/api/worlds/{series_id}/props/{prop_id}")
async def update_world_prop(series_id: str, prop_id: str, request: PropUpdateRequest,
                            authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Edit a prop's or place's name or description."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _item_of(series_id, prop_id)
    cast_api._check_text(request.name, request.description)
    item = await _call(prop_store.update_item, prop_id, name=request.name, description=request.description,
                       actor=who["actor"])
    return {"item": prop_api._item_out(item)}

update_world_prop.allow_members = True


@router.post("/api/worlds/{series_id}/props/{prop_id}/delete")
async def delete_world_prop(series_id: str, prop_id: str, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """Delete a prop or place and its photos. Refused while an episode of the Universe is being made."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _item_of(series_id, prop_id)
    await _refuse_during_runs(who["series"], "its props and places can be deleted")
    return await _call(prop_store.delete_item, prop_id, who["actor"])

delete_world_prop.allow_members = True


@router.post("/api/worlds/{series_id}/props/{prop_id}/photos")
async def upload_world_prop_photo(
    series_id: str,
    prop_id: str,
    image_file: UploadFile = File(...),
    rights: str = Form(""),
    depicts_real_person: str = Form("false"),
    likeness_consent: str = Form("false"),
    note: str = Form(""),
    primary: str = Form("false"),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """Add a photo of a prop or place (the episode route's checks, at most 8 per item)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    await _item_of(series_id, prop_id)
    attestation = await _call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                              cast_api._flag(likeness_consent), note)
    data = await prop_api._read_photo(image_file)
    photo = await _call(prop_store.add_photo, prop_id, data, filename=image_file.filename, attestation=attestation,
                        actor=who["actor"], primary=cast_api._flag(primary))
    duplicate = photo.pop("duplicate", False)
    return {"photo": prop_api._photo_out(photo), "duplicate": duplicate}

upload_world_prop_photo.allow_members = True


@router.post("/api/worlds/{series_id}/props/{prop_id}/photos/{image_id}/primary")
async def set_world_prop_photo_primary(series_id: str, prop_id: str, image_id: str,
                                       authorization: str | None = Header(None),
                                       studio_admin_token: str | None = Cookie(None)):
    """Make a photo the main one."""
    who = await _editor(series_id, authorization, studio_admin_token)
    item = await _item_of(series_id, prop_id)
    await prop_api._photo_of(item, image_id)
    photo = await _call(prop_store.set_primary, prop_id, image_id, who["actor"])
    return {"photo": prop_api._photo_out(photo)}

set_world_prop_photo_primary.allow_members = True


@router.post("/api/worlds/{series_id}/props/{prop_id}/photos/{image_id}/delete")
async def delete_world_prop_photo(series_id: str, prop_id: str, image_id: str,
                                  authorization: str | None = Header(None),
                                  studio_admin_token: str | None = Cookie(None)):
    """Delete a photo. Refused while an episode of the Universe is being made."""
    who = await _editor(series_id, authorization, studio_admin_token)
    item = await _item_of(series_id, prop_id)
    await prop_api._photo_of(item, image_id)
    await _refuse_during_runs(who["series"], "its photos can be deleted")
    return await _call(prop_store.delete_photo, image_id, who["actor"], prop_id=prop_id)

delete_world_prop_photo.allow_members = True


# ------------------------------------------------------------------------------------- creation: references

@router.post("/api/worlds/{series_id}/notes")
async def add_world_note(series_id: str, request: NoteRequest, authorization: str | None = Header(None),
                         studio_admin_token: str | None = Cookie(None)):
    """A reference note about the premise, the characters, the places or the tone."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.body)
    return {"note": await _call(world_store.add_note, series_id, request.topic, request.body, who["actor"])}

add_world_note.allow_members = True


@router.post("/api/worlds/{series_id}/notes/{note_id}")
async def update_world_note(series_id: str, note_id: str, request: NoteUpdateRequest,
                            authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    who = await _editor(series_id, authorization, studio_admin_token)
    if not NOTE_ID_RE.match(note_id):
        raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
    cast_api._check_text(request.body)
    return {"note": await _call(world_store.update_note, series_id, note_id, request.body, who["actor"])}

update_world_note.allow_members = True


@router.post("/api/worlds/{series_id}/notes/{note_id}/delete")
async def delete_world_note(series_id: str, note_id: str, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    who = await _editor(series_id, authorization, studio_admin_token)
    if not NOTE_ID_RE.match(note_id):
        raise HTTPException(status_code=404, detail=f"Note {note_id} not found")
    return await _call(world_store.delete_note, series_id, note_id, who["actor"])

delete_world_note.allow_members = True


@router.post("/api/worlds/{series_id}/photos")
async def upload_world_reference_photo(
    series_id: str,
    image_file: UploadFile = File(...),
    caption: str = Form("", max_length=world_store.CAPTION_MAX),
    rights: str = Form(""),
    depicts_real_person: str = Form("false"),
    likeness_consent: str = Form("false"),
    note: str = Form(""),
    authorization: str | None = Header(None),
    studio_admin_token: str | None = Cookie(None),
):
    """A reference photo for the mood board (the cast picture checks and the rights attestation)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(caption)
    attestation = await _call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                              cast_api._flag(likeness_consent), note)
    data = await prop_api._read_photo(image_file)
    photo = await _call(world_store.add_reference_photo, series_id, data, filename=image_file.filename,
                        attestation=attestation, caption=caption, actor=who["actor"])
    return {"photo": _reference_out(photo), "duplicate": photo["duplicate"]}

upload_world_reference_photo.allow_members = True


@router.post("/api/worlds/{series_id}/photos/{image_id}/delete")
async def delete_world_reference_photo(series_id: str, image_id: str, authorization: str | None = Header(None),
                                       studio_admin_token: str | None = Cookie(None)):
    who = await _editor(series_id, authorization, studio_admin_token)
    if not image_id.startswith("ui_") or not UNIVERSE_IMAGE_RE.match(image_id):
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found")
    await _refresh_board_requests(series_id)
    return await _call(world_store.delete_reference_photo, series_id, image_id, who["actor"])

delete_world_reference_photo.allow_members = True


# ------------------------------------------------------------------------------------- creation: mood board

async def _board_worker_problem() -> tuple[int, str] | None:
    """Why a live board cannot be started now, as (status, plain message); None while a worker polls
    the mood board task queue. Nothing is asked of a provider or charged before this passes."""
    from hfvg.workflows.universe_board import worker_polls
    client = cast_api._api().temporal_client
    if client is None:
        return 503, BOARD_WORKER_UNREACHABLE
    try:
        polling = await worker_polls(client, BOARD_TASK_QUEUE)
    except Exception as e:  # noqa: BLE001 - the owner gets the plain message, the log the reason
        logger.warning("Could not ask Temporal which workers poll the mood board queue: %s", e)
        return 503, BOARD_WORKER_UNREACHABLE
    if not polling:
        logger.warning("No worker polls %s: the running worker predates live mood boards",
                       f"{BOARD_TASK_QUEUE}-universe")
        return 409, BOARD_WORKER_MISSING
    return None


async def _require_board_worker() -> None:
    problem = await _board_worker_problem()
    if problem:
        raise HTTPException(status_code=problem[0], detail=problem[1])


async def _refresh_board_requests(series_id: str) -> None:
    """A live board whose workflow ended without finishing it (a crashed worker, a cancelled run) is
    finished here: a board of the pictures it made, else failed (the reconciler settles any
    money still held for its pictures). A workflow Temporal does not know counts as ended only once
    the request is older than BOARD_START_GRACE (it is recorded just before its workflow starts)."""
    from datetime import datetime, timezone

    from temporalio.service import RPCError, RPCStatusCode
    api = cast_api._api()
    if not api.temporal_client:
        return
    for request in await _call(world_store.making_board_requests, series_id):
        try:
            desc = await api.temporal_client.get_workflow_handle(request["workflow_id"]).describe()
            ended = api._run_ended(desc.status)
        except RPCError as e:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(request["created_at"])
            ended = e.status == RPCStatusCode.NOT_FOUND and age > BOARD_START_GRACE
        except Exception:  # noqa: BLE001 - unknown: keep waiting
            ended = False
        if ended:
            await _call(world_store.finish_board_request, request["request_id"], None, "api")


def _dry_prices() -> list[int]:
    from hfvg.estimates import still_call_usd_micros
    return [still_call_usd_micros() for _ in range(world_store.BOARD_IMAGES)]


def _board_estimate(plan: dict, prices: list[int]) -> dict:
    """A dry-run board's estimate: placeholder pictures, free."""
    total = sum(prices)
    return {"version": plan["version"], "count": len(prices), "per_image_usd_micros": prices,
            "total_usd_micros": total, "dry_run": True, "redo": bool(plan["answers_feedback"]),
            "summary": f"{len(prices)} placeholder images, free in dry-run mode"}


async def _board_budget(series_id: str) -> dict:
    """The Universe's mood board budget line: cap, stop and what is left before the stop. The line
    is created with these figures when the first live picture is paid for."""
    from hfvg.budget import BudgetLedger, compute_stop_usd_micros, get_budget_settings
    ledger = BudgetLedger(cast_api._api().DATABASE_PATH)
    await ledger.init_db()
    try:
        line = await ledger.get_line_status(universe_boards.ledger_key(series_id), universe_boards.LINE)
    except ValueError:
        cap = universe_boards.LINE_CAP_USD_MICROS
        stop = compute_stop_usd_micros(cap, get_budget_settings().stop_percent)
        return {"line": universe_boards.LINE, "cap_usd_micros": cap, "stop_usd_micros": stop, "spent_usd_micros": 0,
                "reserved_usd_micros": 0, "available_usd_micros": stop}
    return {"line": universe_boards.LINE, "cap_usd_micros": line["budget_cap"],
            "stop_usd_micros": line["stop_threshold"], "spent_usd_micros": line["spent"],
            "reserved_usd_micros": line["reserved"], "available_usd_micros": max(line["available"], 0)}


async def _live_jobs(series_id: str, plan: dict, who: dict, request_id: str) -> tuple[list[dict], list[int]]:
    """The paid picture jobs of a live board and the provider's own price of each (fail closed)."""
    from hfvg.cast_sheets import prompt_problems
    owner = cast_api._series_owner(who["series"]) or who["user_id"]
    jobs = await _call(universe_boards.plan_jobs, plan, request_id=request_id, series_id=series_id,
                       owner_user_id=owner)
    problems = sorted({p for job in jobs for p in prompt_problems(job["prompt"])})
    if problems:
        raise HTTPException(status_code=400, detail=(
            "The mood board words are refused by the content policy (" + ", ".join(problems)
            + "); change the notes, captions or feedback"))
    return jobs, await cast_api._price_jobs(jobs, False)


def _live_estimate(plan: dict, jobs: list[dict], prices: list[int], budget: dict) -> dict:
    total = sum(prices)
    return {"version": plan["version"], "count": len(prices), "per_image_usd_micros": prices,
            "total_usd_micros": total, "dry_run": False, "redo": bool(plan["answers_feedback"]),
            "model_path": jobs[0]["model_path"], "references_per_picture": len(jobs[0]["refs"]), "budget": budget,
            "summary": (f"{len(prices)} pictures, about {cast_api._usd(total)}, charged to this Universe's mood "
                        f"board budget ({cast_api._usd(budget['available_usd_micros'])} left)")}


def _estimate_above(total: int, approved: int) -> HTTPException:
    return HTTPException(status_code=409, detail=(
        f"The estimate is now {cast_api._usd(total)} (you approved {cast_api._usd(approved)}); "
        "review it and approve again"))


@router.post("/api/worlds/{series_id}/board/estimate")
async def estimate_world_board(series_id: str, request: BoardRequest, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """What the next mood board makes and costs (nothing is generated or charged). Live: the
    provider's own price for each picture, and what is left on the Universe's mood board budget."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.feedback)
    await _refresh_board_requests(series_id)
    plan = await _call(world_store.plan_board, series_id, request.feedback)
    if cast_api._dry_run():
        return _board_estimate(plan, _dry_prices())
    await _require_board_worker()
    jobs, prices = await _live_jobs(series_id, plan, who, ESTIMATE_REQUEST_ID)
    return _live_estimate(plan, jobs, prices, await _board_budget(series_id))

estimate_world_board.allow_members = True


async def _start_live_board(series_id: str, plan: dict, who: dict, approved: int) -> dict:
    """Ask the worker for a live board the owner approved: the estimate within the approval and the
    budget line, the recorded balance (GC.01) and the balance gate, then the request and its jobs,
    then UniverseBoardWorkflow."""
    from hfvg.balance_monitor import HIGGSFIELD_AUTO_TOPUP_ENABLED, BalanceMonitor
    from hfvg.budget import BudgetLedger, balance_freshness
    from hfvg.workflows.universe_board import (
        UniverseBoardWorkflow,
        universe_board_workflow_id,
        universe_task_queue,
    )
    await _require_board_worker()
    request_id = cast_store._new_id("mr")
    jobs, prices = await _live_jobs(series_id, plan, who, request_id)
    total = sum(prices)
    if total > approved:
        raise _estimate_above(total, approved)
    budget = await _board_budget(series_id)
    if total > budget["available_usd_micros"]:
        raise HTTPException(status_code=409, detail=(
            f"This Universe's mood board budget has {cast_api._usd(budget['available_usd_micros'])} left before its "
            f"stop, and this board costs about {cast_api._usd(total)}. Nothing was charged; ask an admin about the "
            "budget."))
    api = cast_api._api()
    fresh = balance_freshness(await BudgetLedger(api.DATABASE_PATH).get_balance_status())
    if not fresh.get("ok"):
        raise HTTPException(status_code=409, detail=f"Nothing was charged: {fresh['reason']}")
    passed, reason = await BalanceMonitor(api.DATABASE_PATH).check_preflight_balance(
        provider="higgsfield", estimate_usd_micros=total, auto_topup_enabled=HIGGSFIELD_AUTO_TOPUP_ENABLED)
    if not passed:
        raise HTTPException(status_code=409, detail=f"Balance check failed: {reason}")
    # each picture may cost up to its share of the approved total; the worker refuses a higher estimate
    headroom = approved - total
    jobs = [{**job, "estimated_usd_micros": price, "approved_usd_micros": price + (headroom * price // total if total else 0)}
            for job, price in zip(jobs, prices)]
    workflow_id = universe_board_workflow_id(request_id)
    made = await _call(world_store.start_board_request, series_id, plan, jobs, request_id=request_id,
                       workflow_id=workflow_id, estimate_usd_micros=total, approved_total_usd_micros=approved,
                       model_path=jobs[0]["model_path"], requested_by=who["actor"])
    try:
        await api.temporal_client.start_workflow(
            UniverseBoardWorkflow.run, {"request_id": request_id, "series_id": series_id, "jobs": jobs},
            id=workflow_id, task_queue=universe_task_queue(BOARD_TASK_QUEUE), execution_timeout=BOARD_RUN_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - nothing ran: the request ends here, the log keeps the reason
        logger.error("Could not start mood board %s: %s", workflow_id, e)
        await _call(world_store.finish_board_request, request_id, BOARD_NOT_STARTED, who["actor"])
        raise HTTPException(status_code=503, detail=BOARD_NOT_STARTED) from None
    return {"board": None, "request": made, "estimate": _live_estimate(plan, jobs, prices, budget)}


@router.post("/api/worlds/{series_id}/board")
async def generate_world_board(series_id: str, request: BoardGenerateRequest, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """Make the mood board the creator approved the estimate of: the first one, or a redo that
    answers the feedback on the latest. Refused (409) when the estimate is now above the approved
    total. Dry run: placeholder pictures, made here. Live: after the budget line and balance checks
    the worker makes it picture by picture (UniverseBoardWorkflow) and checks each picture's price
    against the approval, the balance gate and the budget line again before paying; the page follows
    it, and the pictures made become the next board (with the reason when it stopped early)."""
    who = await _editor(series_id, authorization, studio_admin_token)
    cast_api._check_text(request.feedback)
    await _refresh_board_requests(series_id)
    plan = await _call(world_store.plan_board, series_id, request.feedback)
    approved = request.approved_total_usd_micros
    if not cast_api._dry_run():
        return await _start_live_board(series_id, plan, who, approved)
    prices = _dry_prices()
    total = sum(prices)
    if total > approved:
        raise _estimate_above(total, approved)
    images = await asyncio.to_thread(world_store.placeholder_board_images,
                                     f"{series_id}:{plan['version']}:{plan['answers_feedback']}", plan["photo_files"])
    board = await _call(world_store.create_board, series_id, plan, estimate_usd_micros=total,
                        approved_total_usd_micros=approved, dry_run=True, images=images, placeholder=True,
                        requested_by=who["actor"])
    return {"board": _board_out(board), "estimate": _board_estimate(plan, prices)}

generate_world_board.allow_members = True


@router.post("/api/worlds/{series_id}/board/{board_id}/approve")
async def approve_world_board(series_id: str, board_id: str, authorization: str | None = Header(None),
                              studio_admin_token: str | None = Cookie(None)):
    """Approve the latest mood board; the Universe bible is written from it."""
    who = await _editor(series_id, authorization, studio_admin_token)
    if not BOARD_ID_RE.match(board_id):
        raise HTTPException(status_code=404, detail=f"Mood board {board_id} not found")
    await _refresh_board_requests(series_id)
    return {"board": _board_out(await _call(world_store.approve_board, series_id, board_id, who["actor"]))}

approve_world_board.allow_members = True


# ------------------------------------------------------------------------------------- creation: bible

async def _bible_from_model(inputs: dict) -> Any:
    if os.getenv("DRAFT_LLM_FAKE", "0") == "1":
        if not cast_api._dry_run():
            raise HTTPException(status_code=500, detail=(
                "DRAFT_LLM_FAKE is a test-only flag and cannot be used when DRY_RUN=false"))
        return world_store.fake_bible(inputs)
    from hfvg.config import config
    if not config.OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail=(
            "Story writing assistant not configured. Set OPENAI_API_KEY environment variable."))
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
    try:
        response = await client.chat.completions.create(
            model=config.STORY_SHAPING_MODEL, messages=world_store.bible_messages(inputs), temperature=0.7,
            response_format={"type": "json_object"})
        return json.loads(response.choices[0].message.content or "{}")
    except Exception as e:  # noqa: BLE001 - nothing was added; the owner can try again
        raise HTTPException(status_code=502, detail=f"The story writing assistant could not write the bible: {e}") \
            from None


def _policy_ok(*texts: str | None) -> bool:
    try:
        cast_api._check_text(*texts)
    except HTTPException:
        return False
    return True


def _keep_allowed(bible: dict) -> tuple[dict, list[str]]:
    """The bible without entries the studio cannot generate (content policy), and their names."""
    dropped: list[str] = []
    out = dict(bible)
    for key in ("characters", "places", "props"):
        kept = []
        for entry in bible[key]:
            if _policy_ok(entry["name"], entry.get("role"), entry["description"]):
                kept.append(entry)
            else:
                dropped.append(entry["name"])
        out[key] = kept
    for key in ("bible", "style_notes"):
        if not _policy_ok(bible[key]):
            out[key] = ""
            dropped.append("story bible" if key == "bible" else "style notes")
    return out, dropped


@router.post("/api/worlds/{series_id}/bible")
async def write_world_bible(series_id: str, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """Write the Universe bible from the approved mood board and the references: its characters,
    places and props join the Universe (names it has keep theirs) and fill an empty story bible and
    style notes. Once per approved board."""
    who = await _editor(series_id, authorization, studio_admin_token)
    inputs = await _call(world_store.bible_inputs, series_id)
    bible, dropped = _keep_allowed(world_store.parse_bible(await _bible_from_model(inputs)))
    result = await _call(world_store.apply_bible, series_id, bible, board_id=inputs["board_id"], actor=who["actor"])
    return {"bible": {**result, "dropped": dropped}}

write_world_bible.allow_members = True


# ------------------------------------------------------------------------------------- images

async def _universe_image(image_id: str, authorization: str | None, cookie: str | None) -> dict:
    """A reference photo or mood board image for its Universe's owner and admins; for the others
    who may see the Universe only its cover and the images of an approved mood board. Everyone else
    gets the 404 a missing image gets (checked before its file)."""
    if not UNIVERSE_IMAGE_RE.match(image_id):
        raise HTTPException(status_code=404, detail="Image not found")
    getter = world_store.get_reference_photo if image_id.startswith("ui_") else world_store.get_board_image
    image = await _call(getter, image_id)
    hidden = HTTPException(status_code=404, detail=f"Image {image_id} not found")
    try:
        who = await _viewer(image["series_id"], authorization, cookie)
    except HTTPException:
        raise hidden from None
    if who["can_edit"] or image.get("board_status") == "approved":
        return image
    profile = await _call(world_store.get_profile, image["series_id"])
    if profile["cover_image_id"] != image_id:
        raise hidden
    return image


@router.get("/api/worlds/images/{image_id}")
async def get_world_image(image_id: str, authorization: str | None = Header(None),
                          studio_admin_token: str | None = Cookie(None)):
    """A reference photo or mood board image, for whoever may see its Universe (never static)."""
    image = await _universe_image(image_id, authorization, studio_admin_token)
    path = cast_store.image_file(image["sha256"], image["ext"])
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail="Image file not found")
    return FileResponse(path, media_type=image["mime"], headers=cast_api.PRIVATE_HEADERS)

get_world_image.allow_members = True


@router.get("/api/worlds/images/{image_id}/thumb")
async def get_world_image_thumb(image_id: str, authorization: str | None = Header(None),
                                studio_admin_token: str | None = Cookie(None)):
    """A small JPEG of a reference photo or mood board image (same rule as the image)."""
    image = await _universe_image(image_id, authorization, studio_admin_token)
    if not await asyncio.to_thread(cast_store.image_file(image["sha256"], image["ext"]).is_file):
        raise HTTPException(status_code=404, detail="Image file not found")
    thumb = await _call(cast_store.ensure_thumb, image["sha256"], image["ext"])
    return FileResponse(thumb, media_type="image/jpeg", headers=cast_api.PRIVATE_HEADERS)

get_world_image_thumb.allow_members = True


# ------------------------------------------------------------------------------------- drafts

async def draft_universe(world_id: str, vo_mode: str | None, authorization: str | None, cookie: str | None) -> dict:
    """The Universe context of a draft in a Universe (POST /api/episodes/draft-from-idea world_id):
    world_store.draft_context, with the story bible and style notes only for its owner and admins.
    The 404 a missing Universe gets for a caller who may not see it."""
    who = await _viewer(world_id, authorization, cookie)
    return await _call(world_store.draft_context, world_id, vo_mode=vo_mode, include_private=who["can_edit"])


# ------------------------------------------------------------------------------------- an episode's Universe

def _version_entry(character: dict, number: int) -> dict:
    version = next((v for v in character["versions"] if v["version"] == number), None)
    locked = [v["version"] for v in character["versions"] if v["locked"]]
    images = [cast_api._image_out(i) for i in (version or {}).get("images") or [] if i["status"] == "active"]
    pick = (next((i for i in images if i["role"] == cast_store.MASTER_ROLE), None)
            or next((i for i in images if i.get("is_primary")), None) or (images[0] if images else None))
    return {"character_id": character["character_id"], "slug": character["slug"], "name": character["name"],
            "version": number, "exists": version is not None, "locked": bool(version and version["locked"]),
            "latest_locked": max(locked) if locked else None,
            "problems": cast_store.verify_lock(character["character_id"], number) if version and version["locked"]
            else [], "image": {"url": pick["url"], "thumb_url": pick["thumb_url"]} if pick else None}


def _strip(episode_id: str, series_id: str) -> dict:
    """What the episode uses from its Universe: each character version, place and prop its shots
    pick (unknown ids left out), with what the badges need."""
    library = ("series", series_id)
    characters = {c["slug"]: c for c in cast_store.list_characters(library)}
    items = {i["slug"]: i for i in prop_store.list_items(library)}
    used: list[tuple[str, int]] = []
    places: list[str] = []
    props: list[str] = []
    for shot in world_store.shot_usage(episode_id):
        for slug, version in shot["characters"]:
            character = characters.get(slug)
            if character is None:
                continue
            key = (slug, version or character["default_version"])
            if key not in used:
                used.append(key)
        if shot["place"] in items and shot["place"] not in places:
            places.append(shot["place"])
        for slug in shot["props"]:
            if slug in items and slug not in props:
                props.append(slug)

    def item_entry(slug: str) -> dict:
        view = prop_api._item_out(items[slug])
        photo = view["photos"][0] if view["photos"] else None
        return {"prop_id": view["prop_id"], "slug": slug, "kind": view["kind"], "name": view["name"],
                "photos": len(view["photos"]),
                "image": {"url": photo["url"], "thumb_url": photo["thumb_url"]} if photo else None}

    return {"characters": [_version_entry(characters[slug], number) for slug, number in used],
            "places": [item_entry(s) for s in places], "props": [item_entry(s) for s in props]}


@router.get("/api/episodes/{episode_id}/universe")
async def get_episode_universe(episode_id: str, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """Who made the episode and whether the caller may act on it; for a caller who may see its cast,
    the Universe it is in (or none), what it uses from it and the characters whose look still has
    to be locked before it starts (and before its shot plan is approved)."""
    episode_id = cast_api._episode_id(episode_id)
    _, user_id, is_admin = await cast_api._actor(authorization, studio_admin_token)
    owner = await cast_api._api().episode_owner(episode_id)
    names = await _user_names({owner} if owner else set())
    out: dict[str, Any] = {"episode_id": episode_id, "owner": {"user_id": owner, "name": names.get(owner)},
                           "can_act": is_admin or owner is None or owner == user_id,
                           "linked": None, "universe": None, "strip": None, "lock": None}
    library = await cast_api._library(episode_id)
    if not await cast_api._may_see(library, user_id, is_admin):
        return out
    out["linked"] = library[0] == "series"
    if library[0] != "series":
        return out
    series = await _call(cast_store.get_series, library[1])
    out["universe"] = {"series_id": series["series_id"], "name": series["name"]}
    out["strip"] = await asyncio.to_thread(_strip, episode_id, series["series_id"])
    out["lock"] = {"required": True, "unlocked": await _call(world_store.unlocked_cast, episode_id)}
    return out

get_episode_universe.allow_members = True
