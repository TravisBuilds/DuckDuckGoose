"""Props and places routes: the things and settings that appear in an episode's shots, each with a
name, a short description and reference photos (hfvg/prop_store.py). Mounted by api/main.py under
the app-wide guard (``require_admin_auth``); every route here allows members.

The rules are the cast library's (docs/AUTHENTICATION.md, api/cast_routes.py): a member's POST under
``/api/episodes/{episode_id}/`` is checked against the episode's owner before the handler runs, and
a series library also needs the series' owner (only an admin while the series has no owner). The
library is seen by whoever may see the episode's cast; everyone else gets the 404 a missing item
gets. Photos get the cast upload checks (PNG, JPEG or WebP, decoded, size and pixel limits, the
rights attestation) and are served only through the authenticated photo routes below. Nothing here
generates or spends: the photos are references for the shots that pick them.
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Cookie, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hfvg import cast_store, prop_store
from hfvg.cast_prompt import MAX_PROPS_PER_SHOT

try:  # "api.cast_routes" in tests and the e2e backend; "cast_routes" when uvicorn runs from api/
    from api import cast_routes as cast_api
except ImportError:
    import cast_routes as cast_api

router = APIRouter()

PROP_ID_RE = re.compile(r"^pr_[0-9a-f]{12}$")
PHOTO_ID_RE = re.compile(r"^pi_[0-9a-f]{12}$")
PROPS_HIDDEN = "Only the people working on this episode (and admins) can see its props and places"


def register(app) -> None:
    """Add these routes to the app itself, like the cast routes (api/cast_routes.register)."""
    for route in router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)


def _photo_out(photo: dict) -> dict:
    out = {k: photo.get(k) for k in ("image_id", "prop_id", "status", "sha256", "ext", "mime", "width", "height",
                                     "bytes", "is_primary", "original_filename", "attestation", "created_by",
                                     "created_at")}
    out["url"] = f"/api/props/photos/{photo['image_id']}"
    out["thumb_url"] = f"/api/props/photos/{photo['image_id']}/thumb"
    out["size_note"] = cast_store.size_note(photo.get("width"), photo.get("height"))
    return out


def _item_out(item: dict) -> dict:
    return {**{k: v for k, v in item.items() if k != "photos"},
            "photos": [_photo_out(p) for p in item.get("photos") or []]}


def limits() -> dict:
    return {"max_upload_bytes": cast_store.max_upload_bytes(), **cast_store.size_limits(),
            "formats": ["PNG", "JPEG", "WebP"], "max_photos_per_item": prop_store.MAX_PHOTOS_PER_ITEM,
            "max_items": prop_store.MAX_ITEMS_PER_LIBRARY, "max_props_per_shot": MAX_PROPS_PER_SHOT,
            "name_max": prop_store.NAME_MAX, "description_max": prop_store.DESCRIPTION_MAX}


async def _item_in(library: tuple[str, str], prop_id: str) -> dict:
    if not PROP_ID_RE.match(prop_id):
        raise HTTPException(status_code=404, detail=f"Prop or place {prop_id} not found")
    item = await cast_api._call(prop_store.get_item, prop_id)
    if (item["library_kind"], item["library_id"]) != tuple(library):
        raise HTTPException(status_code=404, detail=f"Prop or place {prop_id} is not in this episode's library")
    return item


async def _writer(episode_id: str, authorization: str | None, cookie: str | None) -> tuple[str, str, tuple[str, str]]:
    """(episode id, audit actor, library) for a change: the episode's owner was checked by the guard,
    a series library needs the series' owner too."""
    episode_id = cast_api._episode_id(episode_id)
    actor, _, _ = await cast_api._actor(authorization, cookie)
    library = await cast_api._library(episode_id)
    await cast_api._require_library_writer(library, authorization, cookie)
    return episode_id, actor, library


async def _read_photo(image_file: UploadFile) -> bytes:
    limit = cast_store.max_upload_bytes()
    data = await image_file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"That file is too large: pictures can be at most "
                                                    f"{limit // (1024 * 1024)} MB")
    return data


class PropCreateRequest(BaseModel):
    kind: str = Field(..., max_length=10)
    name: str = Field(..., min_length=1, max_length=prop_store.NAME_MAX)
    description: str = Field("", max_length=prop_store.DESCRIPTION_MAX)
    slug: str | None = Field(None, max_length=32)


class PropUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=prop_store.NAME_MAX)
    description: str | None = Field(None, max_length=prop_store.DESCRIPTION_MAX)


# ------------------------------------------------------------------------------------- library


@router.get("/api/episodes/{episode_id}/props")
async def get_episode_props(episode_id: str, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """The episode's props and places (its series' when it is in a series) with their photos, the
    upload limits and rights questions, and whether the caller may change them."""
    episode_id = cast_api._episode_id(episode_id)
    library = await cast_api._library(episode_id)
    await cast_api._require_viewer(library, authorization, studio_admin_token, PROPS_HIDDEN)
    items = await cast_api._call(prop_store.list_items, library)
    series = await cast_api._call(cast_store.get_series, library[1]) if library[0] == "series" else None
    return {
        "episode_id": episode_id,
        "library": {"kind": library[0], "id": library[1]},
        "series": series,
        "items": [_item_out(i) for i in items],
        "limits": limits(),
        "attestation": {"rights": list(cast_store.RIGHTS_VALUES), "likeness_gate": cast_store.LIKENESS_CONSENT},
        "can_edit": await cast_api._can_edit(episode_id, library, authorization, studio_admin_token),
    }

get_episode_props.allow_members = True


@router.post("/api/episodes/{episode_id}/props")
async def create_prop(episode_id: str, request: PropCreateRequest, authorization: str | None = Header(None),
                      studio_admin_token: str | None = Cookie(None)):
    """Add a prop (a thing in the shot) or a place (where the shot happens) without a photo yet."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    cast_api._check_text(request.name, request.description)
    item = await cast_api._call(prop_store.create_item, library, request.kind, request.name, request.description,
                                slug=request.slug or None, actor=actor, episode_id=episode_id)
    return {"item": _item_out(item)}

create_prop.allow_members = True


@router.post("/api/episodes/{episode_id}/props/with-photo")
async def create_prop_with_photo(
    episode_id: str,
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
    """Add a prop or a place from a photo, in one step: the kind, name and description with the photo
    and its rights attestation. Nothing is kept when any of it is refused."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    cast_api._check_text(name, description)
    attestation = await cast_api._call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                                       cast_api._flag(likeness_consent), note)
    data = await _read_photo(image_file)
    item = await cast_api._call(prop_store.create_item_with_upload, library, kind, name, description, data,
                                filename=image_file.filename, attestation=attestation, actor=actor,
                                episode_id=episode_id)
    return {"item": _item_out(item)}

create_prop_with_photo.allow_members = True


@router.post("/api/episodes/{episode_id}/props/{prop_id}")
async def update_prop(episode_id: str, prop_id: str, request: PropUpdateRequest,
                      authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Edit the name or description (the kind stays: delete and add again to change it)."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    await _item_in(library, prop_id)
    cast_api._check_text(request.name, request.description)
    item = await cast_api._call(prop_store.update_item, prop_id, name=request.name, description=request.description,
                                actor=actor, episode_id=episode_id)
    return {"item": _item_out(item)}

update_prop.allow_members = True


@router.post("/api/episodes/{episode_id}/props/{prop_id}/delete")
async def delete_prop(episode_id: str, prop_id: str, authorization: str | None = Header(None),
                      studio_admin_token: str | None = Cookie(None)):
    """Delete a prop or place and its photos (the files too, once nothing uses them). Refused while a
    run of an episode using the library is in progress: the run planned its references from it."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    await _item_in(library, prop_id)
    await cast_api._refuse_during_runs(library, episode_id, "its props and places can be deleted")
    return await cast_api._call(prop_store.delete_item, prop_id, actor, episode_id=episode_id)

delete_prop.allow_members = True


# ------------------------------------------------------------------------------------- photos


@router.post("/api/episodes/{episode_id}/props/{prop_id}/photos")
async def upload_prop_photo(
    episode_id: str,
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
    """Add a reference photo: the cast picture checks and limits, the rights attestation, private
    storage, deduplicated by content hash, recorded in the audit log."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    await _item_in(library, prop_id)
    attestation = await cast_api._call(cast_store.validate_attestation, rights, cast_api._flag(depicts_real_person),
                                       cast_api._flag(likeness_consent), note)
    data = await _read_photo(image_file)
    photo = await cast_api._call(prop_store.add_photo, prop_id, data, filename=image_file.filename,
                                 attestation=attestation, actor=actor, primary=cast_api._flag(primary),
                                 episode_id=episode_id)
    duplicate = photo.pop("duplicate", False)
    return {"photo": _photo_out(photo), "duplicate": duplicate}

upload_prop_photo.allow_members = True


async def _photo_of(item: dict, image_id: str) -> dict:
    if not PHOTO_ID_RE.match(image_id):
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found")
    photo = await cast_api._call(prop_store.get_photo, image_id)
    if photo["prop_id"] != item["prop_id"]:
        raise HTTPException(status_code=404, detail=f"Image {image_id} is not a photo of {item['name']}")
    return photo


@router.post("/api/episodes/{episode_id}/props/{prop_id}/photos/{image_id}/primary")
async def set_prop_photo_primary(episode_id: str, prop_id: str, image_id: str,
                                 authorization: str | None = Header(None),
                                 studio_admin_token: str | None = Cookie(None)):
    """Make a photo the main one (used first)."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    item = await _item_in(library, prop_id)
    await _photo_of(item, image_id)
    photo = await cast_api._call(prop_store.set_primary, prop_id, image_id, actor, episode_id=episode_id)
    return {"photo": _photo_out(photo)}

set_prop_photo_primary.allow_members = True


@router.post("/api/episodes/{episode_id}/props/{prop_id}/photos/{image_id}/delete")
async def delete_prop_photo(episode_id: str, prop_id: str, image_id: str, authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """Delete a photo (the file too, once nothing uses it). Refused during a run, like a delete of
    the prop or place."""
    episode_id, actor, library = await _writer(episode_id, authorization, studio_admin_token)
    item = await _item_in(library, prop_id)
    await _photo_of(item, image_id)
    await cast_api._refuse_during_runs(library, episode_id, "its photos can be deleted")
    return await cast_api._call(prop_store.delete_photo, image_id, actor, prop_id=prop_id, episode_id=episode_id)

delete_prop_photo.allow_members = True


async def _photo_for(image_id: str, authorization: str | None, cookie: str | None) -> dict:
    """The photo when the caller may see its library; otherwise the 404 a missing photo gets (checked
    before its file, so nothing is read or made for a caller who may not see it)."""
    if not PHOTO_ID_RE.match(image_id):
        raise HTTPException(status_code=404, detail="Image not found")
    photo = await cast_api._call(prop_store.get_photo, image_id)
    _, user_id, is_admin = await cast_api._actor(authorization, cookie)
    library = await cast_api._call(prop_store.photo_library, image_id)
    if library is None or not await cast_api._may_see(library, user_id, is_admin):
        raise HTTPException(status_code=404, detail=f"Image {image_id} not found")
    return photo


@router.get("/api/props/photos/{image_id}")
async def get_prop_photo(image_id: str, authorization: str | None = Header(None),
                         studio_admin_token: str | None = Cookie(None)):
    """A prop or place photo, for whoever may see its library (never served as a static file)."""
    photo = await _photo_for(image_id, authorization, studio_admin_token)
    path = prop_store.photo_path(photo)
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(status_code=404, detail="Image file not found")
    return FileResponse(path, media_type=photo["mime"], headers=cast_api.PRIVATE_HEADERS)

get_prop_photo.allow_members = True


@router.get("/api/props/photos/{image_id}/thumb")
async def get_prop_photo_thumb(image_id: str, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """A small JPEG of a photo for the Props & places panel (same rule as the photo)."""
    photo = await _photo_for(image_id, authorization, studio_admin_token)
    if not await asyncio.to_thread(prop_store.photo_path(photo).is_file):
        raise HTTPException(status_code=404, detail="Image file not found")
    thumb = await cast_api._call(cast_store.ensure_thumb, photo["sha256"], photo["ext"])
    return FileResponse(thumb, media_type="image/jpeg", headers=cast_api.PRIVATE_HEADERS)

get_prop_photo_thumb.allow_members = True
