"""Cast library routes: series, characters, versions (outfits), reference uploads, character sheets
and who is in frame in each shot. Mounted by api/main.py under the same app-wide guard
(``require_admin_auth``); every route here allows members.

Who may change what (docs/AUTHENTICATION.md): a member's POST under ``/api/episodes/{episode_id}/``
is checked against the episode's owner before the handler runs (the app-wide guard); a change to a
series' cast also needs the series' owner (or an admin, or a series without an owner). Members can
read every cast, as they read every episode. Images are served only through the authenticated image
routes below, never as static files.

Paid sheet images go through ``CastSheetWorkflow``: the creator sees the estimate and approves it,
then the activity re-checks the provider estimate against the approval, the balance gate, the caps
and the ledgers (hfvg/activities/cast_sheet.py). Dry-run mode (DRY_RUN=true) makes free placeholder
images.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

from fastapi import APIRouter, Cookie, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hfvg import cast_store, paths
from hfvg.cast_prompt import MAX_CAST_PER_SHOT, parse_cast_request

router = APIRouter()

IMAGE_ID_RE = re.compile(r"^ci_[0-9a-f]{12}$")
UPLOAD_PATH_RE = re.compile(r"^/api/episodes/[^/]+/cast/[^/]+/versions/[^/]+/images/?$")
FORM_OVERHEAD_BYTES = 64 * 1024
TRUE_VALUES = ("1", "true", "yes", "on")


_main = None


def bind(main_module) -> None:
    """Called by the API module once its helpers exist. It is handed over rather than imported: it
    loads as "api.main" (tests, e2e backend) or as "main" (uvicorn started from api/, the image)."""
    global _main
    _main = main_module


def _api():
    if _main is None:
        raise RuntimeError("cast routes are used before the API module bound them")
    return _main


def register(app) -> None:
    """Add these routes to the app itself: plain routes under the app-wide guard and dependencies,
    exactly like the routes declared in api/main.py (and enumerated by its route auth tests)."""
    for route in router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)


async def _call(fn, *args, **kwargs):
    """Run a (blocking, SQLite) cast store call off the event loop; store errors become HTTP errors."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except cast_store.CastError as e:
        raise HTTPException(status_code=e.status, detail=str(e)) from None


def _flag(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUE_VALUES


def _dry_run() -> bool:
    return os.getenv("DRY_RUN", "true").lower() == "true"


def _usd(micros: int) -> str:
    return f"${micros / 1_000_000:.2f}"


# ------------------------------------------------------------------------------------- auth


async def _actor(authorization: str | None, cookie: str | None) -> tuple[str, str | None, bool]:
    """(audit actor, user id, is admin) of the caller."""
    user_id, is_admin = await _api()._episode_actor(authorization, cookie)
    return user_id or ("admin" if is_admin else "member"), user_id, is_admin


def _series_owner(series: dict) -> str | None:
    from hfvg.users import LEGACY_USER_ID
    owner = series.get("owner_user_id")
    return None if owner in (None, "", LEGACY_USER_ID) else owner


async def _require_series_owner(series_id: str, authorization: str | None, cookie: str | None) -> dict:
    """403 unless the caller may change the series' cast: an admin, its owner, or anyone while it has none."""
    series = await _call(cast_store.get_series, series_id)
    _, user_id, is_admin = await _actor(authorization, cookie)
    owner = _series_owner(series)
    if not is_admin and owner is not None and owner != user_id:
        raise HTTPException(status_code=403, detail=(
            f"Series {series['name']} belongs to another member; only its owner or an admin can change it"))
    return series


async def _require_library_writer(library: tuple[str, str], authorization: str | None, cookie: str | None) -> None:
    """The episode owner check already ran (app-wide guard); a series library also needs its owner."""
    if library[0] == "series":
        await _require_series_owner(library[1], authorization, cookie)


async def _can_edit(episode_id: str, library: tuple[str, str], authorization: str | None,
                    cookie: str | None) -> bool:
    _, user_id, is_admin = await _actor(authorization, cookie)
    if is_admin:
        return True
    owner = await _api().episode_owner(episode_id)
    if owner is not None and owner != user_id:
        return False
    if library[0] == "series":
        series = await _call(cast_store.get_series, library[1])
        series_owner = _series_owner(series)
        return series_owner is None or series_owner == user_id
    return True


# ------------------------------------------------------------------------------------- helpers


def _episode_id(raw: str) -> str:
    api = _api()
    episode_id = api.episode_workflow_id(raw)
    api.validate_episode_id(episode_id)
    return episode_id


async def _library(episode_id: str) -> tuple[str, str]:
    return await _call(cast_store.library_for_episode, episode_id)


async def _character_in(library: tuple[str, str], character_id: str) -> dict:
    character = await _call(cast_store.get_character, character_id)
    if (character["library_kind"], character["library_id"]) != tuple(library):
        raise HTTPException(status_code=404, detail=f"Character {character_id} is not in this episode's cast")
    return character


async def _refuse_during_runs(library: tuple[str, str], episode_id: str, what: str) -> None:
    """409 while the episode (or, for a series cast, any episode of the series) has a run in progress:
    the run planned its references from this cast."""
    episodes = [episode_id]
    if library[0] == "series":
        series = await _call(cast_store.get_series, library[1])
        episodes += [e for e in series["episodes"] if e != episode_id]
    api = _api()
    for ep in episodes:
        run = await api._open_episode_run(ep)
        if run:
            raise HTTPException(status_code=409, detail=(
                f"Episode {ep} is being made right now (run {run['run_id']}); {what} after that run ends"))


def _image_out(image: dict) -> dict:
    out = {k: image.get(k) for k in ("image_id", "character_id", "version", "role", "status", "sha256", "ext",
                                     "mime", "width", "height", "bytes", "is_primary", "placeholder", "source",
                                     "job_id", "original_filename", "attestation", "created_by", "created_at")}
    out["url"] = f"/api/cast/images/{image['image_id']}"
    out["thumb_url"] = f"/api/cast/images/{image['image_id']}/thumb"
    return out


def _character_out(character: dict) -> dict:
    versions = []
    for v in character["versions"]:
        versions.append({**v, "images": [_image_out(i) for i in v["images"] if i["status"] == "active"]})
    return {**character, "versions": versions}


def _check_text(*texts: str | None) -> None:
    """The words the studio refuses to generate (content policy) are refused in character text too."""
    from hfvg.content_policy import hard_stop_terms, precheck_banned_words
    joined = " ".join(t for t in texts if isinstance(t, str))
    bad = list(dict.fromkeys([*hard_stop_terms(joined), *precheck_banned_words(joined)]))
    if bad:
        raise HTTPException(status_code=400, detail=(
            "Please reword: the studio cannot generate " + ", ".join(repr(w) for w in bad[:5])))


async def _live_mode(episode_id: str) -> bool:
    row = await _api()._studio_row("SELECT live_mode FROM episodes WHERE episode_id = ?", (episode_id,))
    return bool(row and row[0])


def _still_cap() -> int | None:
    from hfvg.cast_plan import still_ref_cap
    try:
        return still_ref_cap()
    except Exception:
        return None


def _shots_view(episode_id: str) -> dict:
    """The uploaded shots with their cast and what each shot's cast plans to (no files written)."""
    from hfvg.cast_plan import preview_episode_cast
    path = paths.episode_dir(episode_id) / "shots.json"
    if not path.is_file():
        return {"uploaded": False, "shots": [], "plan": None, "error": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        report = preview_episode_cast(episode_id, raw, live=not _dry_run())
    except (OSError, ValueError) as e:
        return {"uploaded": True, "shots": [], "plan": None, "error": f"shots.json cannot be read: {e}"}
    shots = []
    for item in raw["shots"]:
        sid = item["id"]
        row = report["shots"].get(sid, {})
        shots.append({"shot_id": sid, "prompt": str(item.get("prompt") or "")[:240],
                      "characters": item.get("characters") or [], "cast": row.get("cast") or [],
                      "allow_text": item.get("allow_text"), "text_policy": row.get("text_policy"),
                      "refs": row.get("refs") or [], "notes": row.get("notes") or []})
    return {"uploaded": True, "shots": shots, "error": None,
            "plan": {"errors": report["errors"], "notes": report["notes"], "text_policy": report["text_policy"]}}


def _sheet_info(live_mode: bool) -> dict:
    from hfvg import cast_sheets
    info: dict[str, Any] = {
        "dry_run": _dry_run(), "live_mode": live_mode, "model_path": None, "model_error": None,
        "text_options": list(cast_sheets.TEXT_OPTIONS), "reference_options": list(cast_sheets.REF_OPTIONS),
        "default_text_options": cast_sheets.DEFAULT_TEXT_OPTIONS,
        "default_reference_options": cast_sheets.DEFAULT_REF_OPTIONS,
        "views": list(cast_sheets.TURNAROUND_VIEWS), "resolution": cast_sheets.SHEET_RESOLUTION,
    }
    try:
        info["model_path"] = cast_sheets.sheet_model_path()
        cast_sheets.check_sheet_model(info["model_path"], 1)
    except cast_store.CastError as e:
        info["model_error"] = str(e)
    return info


async def _refresh_sheet_jobs(characters: list[dict]) -> None:
    """Queued/running sheet jobs whose workflow has ended are marked failed (a crashed worker or a
    cancelled run leaves them behind; the reconciler settles any money held for them)."""
    from temporalio.service import RPCError, RPCStatusCode
    api = _api()
    if not api.temporal_client:
        return
    stale: dict[str, list[str]] = {}
    for character in characters:
        for version in character["versions"]:
            for job in version["sheet_jobs"]:
                if job["status"] in ("queued", "running") and job.get("workflow_id"):
                    stale.setdefault(job["workflow_id"], []).append(job["job_id"])
    for workflow_id, job_ids in stale.items():
        try:
            desc = await api.temporal_client.get_workflow_handle(workflow_id).describe()
            ended = api._run_ended(desc.status)
        except RPCError as e:
            ended = e.status == RPCStatusCode.NOT_FOUND
        except Exception:
            ended = False
        if ended:
            for job_id in job_ids:
                job = await _call(_sheet_job, job_id)
                if job and job["status"] in ("queued", "running"):
                    await _call(cast_store.update_sheet_job, job_id, status="failed",
                                error="The generation stopped before this image was made")


def _sheet_job(job_id: str) -> dict | None:
    with cast_store.connect() as conn:
        row = conn.execute("SELECT * FROM cast_sheet_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------------------------------------------- upload limit


async def _send_too_large(send, limit: int) -> None:
    body = json.dumps({"detail": f"That file is too large: pictures can be at most "
                                 f"{cast_store.max_upload_bytes() // (1024 * 1024)} MB"}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class CastUploadLimit:
    """413 for a cast image upload whose body is larger than CAST_UPLOAD_MAX_BYTES (plus the form's
    own overhead) before the multipart form is parsed or written anywhere: the declared length is
    checked first, then the body is counted as it arrives (chunked uploads have no length)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or not UPLOAD_PATH_RE.match(scope["path"]):
            await self.app(scope, receive, send)
            return
        try:
            limit = cast_store.max_upload_bytes() + FORM_OVERHEAD_BYTES
        except cast_store.CastError:
            limit = cast_store.DEFAULT_MAX_BYTES + FORM_OVERHEAD_BYTES
        length = dict(scope.get("headers") or []).get(b"content-length")
        if length is not None and length.isdigit() and int(length) > limit:
            await _send_too_large(send, limit)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            if len(body) > limit:
                await _send_too_large(send, limit)
                return
            if not message.get("more_body", False):
                break
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


# ------------------------------------------------------------------------------------- models


class SeriesCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=cast_store.NAME_MAX)


class EpisodeSeriesRequest(BaseModel):
    series_id: str | None = Field(None, max_length=40)


class CharacterCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=cast_store.NAME_MAX)
    role: str = Field("", max_length=cast_store.ROLE_MAX)
    description: str = Field("", max_length=cast_store.DESCRIPTION_MAX)
    outfit: str = Field("", max_length=cast_store.OUTFIT_MAX)
    slug: str | None = Field(None, max_length=32)


class CharacterUpdateRequest(BaseModel):
    name: str | None = Field(None, max_length=cast_store.NAME_MAX)
    role: str | None = Field(None, max_length=cast_store.ROLE_MAX)
    description: str | None = Field(None, max_length=cast_store.DESCRIPTION_MAX)


class CharacterDeleteRequest(BaseModel):
    force: bool = False
    reason: str = Field("", max_length=300)


class CastImportEntry(BaseModel):
    code: str | None = Field(None, max_length=16)
    slug: str | None = Field(None, max_length=32)
    name: str = Field(..., min_length=1, max_length=cast_store.NAME_MAX)
    role: str = Field("", max_length=cast_store.ROLE_MAX)
    description: str = Field("", max_length=cast_store.DESCRIPTION_MAX)


class CastImportRequest(BaseModel):
    characters: list[CastImportEntry] = Field(..., max_length=cast_store.MAX_CHARACTERS_PER_LIBRARY)


class VersionCreateRequest(BaseModel):
    label: str = Field("", max_length=cast_store.LABEL_MAX)
    outfit: str = Field("", max_length=cast_store.OUTFIT_MAX)
    derive_from_version: int | None = Field(None, ge=1)


class VersionUpdateRequest(BaseModel):
    label: str | None = Field(None, max_length=cast_store.LABEL_MAX)
    outfit: str | None = Field(None, max_length=cast_store.OUTFIT_MAX)


class SheetRequest(BaseModel):
    stage: str = Field(..., pattern="^(options|turnaround)$")
    count: int | None = Field(None, ge=1, le=4)
    source: str = Field("auto", pattern="^(auto|upload|text)$")
    views: list[str] | None = Field(None, max_length=5)


class SheetGenerateRequest(SheetRequest):
    approved_total_usd_micros: int = Field(..., ge=0)


class PickRequest(BaseModel):
    image_id: str = Field(..., max_length=20)


class RejectRequest(BaseModel):
    stage: str = Field(..., pattern="^(options|turnaround|all)$")


class ApproveRequest(BaseModel):
    allow_placeholder: bool = False


class ShotCastRequest(BaseModel):
    shots: dict[str, list[str | dict[str, Any]]] = Field(default_factory=dict)
    allow_text: dict[str, bool | None] = Field(default_factory=dict)


# ------------------------------------------------------------------------------------- series


@router.get("/api/series")
async def list_series(authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Every series (members can read all of them) and whether the caller may change each one."""
    _, user_id, is_admin = await _actor(authorization, studio_admin_token)
    rows = await _call(cast_store.list_series)
    for row in rows:
        owner = _series_owner(row)
        row["can_edit"] = is_admin or owner is None or owner == user_id
    return {"series": rows}

list_series.allow_members = True


@router.post("/api/series")
async def create_series(request: SeriesCreateRequest, authorization: str | None = Header(None),
                        studio_admin_token: str | None = Cookie(None)):
    """A new series owned by the caller (a Bearer admin request creates one without an owner)."""
    actor, user_id, _ = await _actor(authorization, studio_admin_token)
    _check_text(request.name)
    return {"series": await _call(cast_store.create_series, request.name, user_id, actor)}

create_series.allow_members = True


@router.get("/api/series/{series_id}")
async def get_series_cast(series_id: str, authorization: str | None = Header(None),
                          studio_admin_token: str | None = Cookie(None)):
    """A series with its characters (the create wizard maps drafted characters to them)."""
    _, user_id, is_admin = await _actor(authorization, studio_admin_token)
    series = await _call(cast_store.get_series, series_id)
    owner = _series_owner(series)
    characters = await _call(cast_store.list_characters, ("series", series_id))
    return {"series": series, "characters": [_character_out(c) for c in characters],
            "can_edit": is_admin or owner is None or owner == user_id}

get_series_cast.allow_members = True


@router.post("/api/episodes/{episode_id}/series")
async def set_episode_series(episode_id: str, request: EpisodeSeriesRequest,
                             authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Put the episode in a series (its own characters move into the series cast) or take it out
    (series_id null; the series keeps its characters). Needs the series' owner as well."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    if request.series_id:
        await _require_series_owner(request.series_id, authorization, studio_admin_token)
    current = await _library(episode_id)
    await _refuse_during_runs(current, episode_id, "its series can change")
    return await _call(cast_store.link_episode, episode_id, request.series_id or None, actor)

set_episode_series.allow_members = True


# ------------------------------------------------------------------------------------- cast


@router.get("/api/episodes/{episode_id}/cast")
async def get_episode_cast(episode_id: str, authorization: str | None = Header(None),
                           studio_admin_token: str | None = Cookie(None)):
    """Everything the Cast panel shows: the episode's cast library (its series' or its own), each
    character with versions, images and sheet jobs, the shots with their cast and what each shot's
    cast plans to, the still model's reference limit and the sheet options."""
    episode_id = _episode_id(episode_id)
    library = await _library(episode_id)
    characters = await _call(cast_store.list_characters, library)
    await _refresh_sheet_jobs(characters)
    characters = await _call(cast_store.list_characters, library)
    series = await _call(cast_store.get_series, library[1]) if library[0] == "series" else None
    return {
        "episode_id": episode_id,
        "library": {"kind": library[0], "id": library[1]},
        "series": series,
        "characters": [_character_out(c) for c in characters],
        "shots": await asyncio.to_thread(_shots_view, episode_id),
        "still_max_refs": _still_cap(),
        "sheet": _sheet_info(await _live_mode(episode_id)),
        "limits": {"max_upload_bytes": cast_store.max_upload_bytes(), "min_image_px": cast_store.min_image_px(),
                   "formats": ["PNG", "JPEG", "WebP"], "max_uploads_per_version": cast_store.MAX_UPLOADS_PER_VERSION,
                   "max_characters": cast_store.MAX_CHARACTERS_PER_LIBRARY, "max_cast_per_shot": MAX_CAST_PER_SHOT},
        "attestation": {"rights": list(cast_store.RIGHTS_VALUES), "likeness_gate": cast_store.LIKENESS_CONSENT},
        "can_edit": await _can_edit(episode_id, library, authorization, studio_admin_token),
    }

get_episode_cast.allow_members = True


@router.post("/api/episodes/{episode_id}/cast")
async def create_cast_character(episode_id: str, request: CharacterCreateRequest,
                                authorization: str | None = Header(None),
                                studio_admin_token: str | None = Cookie(None)):
    """Add a character to the episode's cast (its series' cast when it is in a series)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    _check_text(request.name, request.role, request.description, request.outfit)
    character = await _call(cast_store.create_character, library, request.name, request.role, request.description,
                            request.slug or None, actor, episode_id=episode_id, outfit=request.outfit)
    return {"character": _character_out(character)}

create_cast_character.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/import")
async def import_cast(episode_id: str, request: CastImportRequest, authorization: str | None = Header(None),
                      studio_admin_token: str | None = Cookie(None)):
    """The create wizard's cast: one character per drafted character. A character whose id is already
    in the cast is reused (the wizard can be retried). Returns the id each entry got."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    for entry in request.characters:
        _check_text(entry.name, entry.role, entry.description)
    out = []
    for entry in request.characters:
        existing = await _call(cast_store.find_character, library, entry.slug) if entry.slug else None
        if existing:
            character, status = existing, "existing"
        else:
            character = await _call(cast_store.create_character, library, entry.name, entry.role, entry.description,
                                    entry.slug or None, actor, episode_id=episode_id)
            status = "created"
        out.append({"code": entry.code, "slug": character["slug"], "character_id": character["character_id"],
                    "name": character["name"], "default_version": character["default_version"], "status": status})
    return {"library": {"kind": library[0], "id": library[1]}, "characters": out}

import_cast.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/shots")
async def set_shot_cast(episode_id: str, request: ShotCastRequest, authorization: str | None = Header(None),
                        studio_admin_token: str | None = Cookie(None)):
    """Who is in frame in each shot (shots.json ``cast``) and per-shot ``allow_text``. Only the named
    shots change; an empty list removes the shot's cast. Refused while a run is in progress."""
    from hfvg.shot_data import ShotDataError, parse_shots
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    await _api()._refuse_upload_during_run(episode_id)
    path = paths.episode_dir(episode_id) / "shots.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="This episode has no shot list yet; upload shots.json first")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        items = {item["id"]: item for item in raw["shots"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"shots.json cannot be read: {e}") from None
    unknown = sorted((set(request.shots) | set(request.allow_text)) - set(items))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown shots: {unknown}")
    library = await _library(episode_id)
    known = {c["slug"] for c in await _call(cast_store.list_characters, library)}
    for sid, entries in request.shots.items():
        try:
            parsed = parse_cast_request(entries, sid)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        missing = sorted({p["character"] for p in parsed} - known)
        if missing:
            raise HTTPException(status_code=400, detail=f"shot {sid}: {missing} are not in this episode's cast")
        if entries:
            items[sid]["cast"] = entries
        else:
            items[sid].pop("cast", None)
    for sid, allow in request.allow_text.items():
        if allow is None:
            items[sid].pop("allow_text", None)
        else:
            items[sid]["allow_text"] = allow
    try:
        parse_shots(raw)
    except (ValueError, ShotDataError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid shots.json: {e}") from None
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    await _call(cast_store.log_event, actor, "shot_cast_update", episode_id=episode_id,
                details={"shots": {sid: request.shots[sid] for sid in request.shots},
                         "allow_text": request.allow_text})
    return {"episode_id": episode_id, "shots": await asyncio.to_thread(_shots_view, episode_id)}

set_shot_cast.allow_members = True


@router.get("/api/episodes/{episode_id}/cast/{character_id}")
async def get_cast_character(episode_id: str, character_id: str, authorization: str | None = Header(None),
                             studio_admin_token: str | None = Cookie(None)):
    """One character with its versions, images, sheet jobs and audit trail."""
    episode_id = _episode_id(episode_id)
    character = await _character_in(await _library(episode_id), character_id)
    audit = await _call(cast_store.list_audit, character_id=character_id, limit=200)
    return {"character": _character_out(character), "audit": audit}

get_cast_character.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}")
async def update_cast_character(episode_id: str, character_id: str, request: CharacterUpdateRequest,
                                authorization: str | None = Header(None),
                                studio_admin_token: str | None = Cookie(None)):
    """Edit the name, role or description (a locked version keeps the description it was locked with)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    _check_text(request.name, request.role, request.description)
    character = await _call(cast_store.update_character, character_id, name=request.name, role=request.role,
                            description=request.description, actor=actor, episode_id=episode_id)
    return {"character": _character_out(character)}

update_cast_character.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/delete")
async def delete_cast_character(episode_id: str, character_id: str, request: CharacterDeleteRequest,
                                authorization: str | None = Header(None),
                                studio_admin_token: str | None = Cookie(None)):
    """Delete a character and its pictures. Locked sheets are protected: only an admin with force and
    a reason removes a character that has one."""
    episode_id = _episode_id(episode_id)
    actor, _, is_admin = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    if request.force and not is_admin:
        raise HTTPException(status_code=403, detail="Only an admin can remove a character with locked sheets")
    await _refuse_during_runs(library, episode_id, "its characters can be deleted")
    return await _call(cast_store.delete_character, character_id, actor, force=request.force,
                       reason=request.reason, episode_id=episode_id)

delete_cast_character.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions")
async def create_cast_version(episode_id: str, character_id: str, request: VersionCreateRequest,
                              authorization: str | None = Header(None),
                              studio_admin_token: str | None = Cookie(None)):
    """A new version (outfit). Deriving from a locked version uses its locked master as the identity
    reference of the new sheet (never scene stills)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    _check_text(request.label, request.outfit)
    version = await _call(cast_store.create_version, character_id, label=request.label, outfit=request.outfit,
                          derive_from_version=request.derive_from_version, actor=actor, episode_id=episode_id)
    return {"version": version, "character": _character_out(await _call(cast_store.get_character, character_id))}

create_cast_version.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}")
async def update_cast_version(episode_id: str, character_id: str, version: int, request: VersionUpdateRequest,
                              authorization: str | None = Header(None),
                              studio_admin_token: str | None = Cookie(None)):
    """Edit an unlocked version's label or outfit."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    _check_text(request.label, request.outfit)
    updated = await _call(cast_store.update_version, character_id, version, label=request.label,
                          outfit=request.outfit, actor=actor, episode_id=episode_id)
    return {"version": updated}

update_cast_version.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images")
async def upload_cast_image(
    episode_id: str,
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
    """Upload a reference picture (PNG, JPEG or WebP; decoded and checked, at least CAST_MIN_IMAGE_PX
    on the short side, at most CAST_UPLOAD_MAX_BYTES). Needs the rights attestation: the picture is
    original or the creator owns the rights, and a real person only with their likeness consent.
    Stored privately, deduplicated by content hash, recorded in the cast audit log."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    attestation = await _call(cast_store.validate_attestation, rights, _flag(depicts_real_person),
                              _flag(likeness_consent), note)
    limit = cast_store.max_upload_bytes()
    data = await image_file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail=f"That file is too large: pictures can be at most "
                                                    f"{limit // (1024 * 1024)} MB")
    image = await _call(cast_store.add_upload, character_id, version, data, filename=image_file.filename,
                        attestation=attestation, actor=actor, primary=_flag(primary), episode_id=episode_id)
    duplicate = image.pop("duplicate", False)
    return {"image": _image_out(image), "duplicate": duplicate}

upload_cast_image.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images/{image_id}/primary")
async def set_cast_image_primary(episode_id: str, character_id: str, version: int, image_id: str,
                                 authorization: str | None = Header(None),
                                 studio_admin_token: str | None = Cookie(None)):
    """Make an upload the version's main reference (used first)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    image = await _call(cast_store.set_primary, character_id, version, image_id, actor, episode_id=episode_id)
    return {"image": _image_out(image)}

set_cast_image_primary.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images/{image_id}/delete")
async def delete_cast_image(episode_id: str, character_id: str, version: int, image_id: str,
                            authorization: str | None = Header(None),
                            studio_admin_token: str | None = Cookie(None)):
    """Delete an upload or an unlocked sheet image (the file too, once nothing uses it). Pictures of a
    locked version are protected."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    image = await _call(cast_store.get_image, image_id)
    if image["character_id"] != character_id or image["version"] != version:
        raise HTTPException(status_code=404, detail=f"Image {image_id} is not part of this version")
    await _refuse_during_runs(library, episode_id, "its pictures can be deleted")
    return await _call(cast_store.delete_image, image_id, actor, character_id=character_id, episode_id=episode_id)

delete_cast_image.allow_members = True


# ------------------------------------------------------------------------------------- sheets


async def _plan_sheet(episode_id: str, character: dict, version: int, request: SheetRequest) -> tuple[dict, bool]:
    from hfvg.cast_sheets import plan_jobs
    dry = _dry_run()
    if not dry and not await _live_mode(episode_id):
        raise HTTPException(status_code=409, detail=(
            f"Paid sheet images are charged to {episode_id}, which is not in live mode yet; start it live first "
            "(in dry-run mode the studio makes free placeholder sheets)"))
    plan = await _call(plan_jobs, character, version, request.stage, episode_id=episode_id, count=request.count,
                       source=request.source, views=request.views, live=not dry)
    return plan, dry


async def _price_jobs(jobs: list[dict], dry: bool) -> list[int]:
    """Per-image price in usd_micros: the provider's own estimate (fail closed), or the dry-run figure."""
    from hfvg.estimates import still_call_usd_micros
    if dry:
        return [still_call_usd_micros() for _ in jobs]
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    provider = None
    prices: dict[tuple, int] = {}
    out = []
    try:
        provider = HiggsfieldStillProvider(model_path=jobs[0]["model_path"])
        for job in jobs:
            key = (job["prompt"], job["resolution"], job["quality"], job["aspect_ratio"])
            if key not in prices:
                usd, _credits = await provider.estimate_usd_and_credits(
                    prompt=job["prompt"], resolution=job["resolution"], quality=job["quality"],
                    aspect_ratio=job["aspect_ratio"])
                if isinstance(usd, bool) or not isinstance(usd, int) or usd <= 0:
                    raise ValueError(f"no usable price ({usd!r})")
                prices[key] = usd
            out.append(prices[key])
    except Exception as e:
        raise HTTPException(status_code=503, detail=(
            f"The image service did not give a price, so nothing was generated: {e}")) from None
    finally:
        if provider is not None:
            await provider.close()
    return out


def _estimate_out(plan: dict, prices: list[int], dry: bool, episode_id: str) -> dict:
    total = sum(prices)
    jobs = plan["jobs"]
    return {
        "stage": plan["stage"], "mode": plan["mode"], "model_path": plan["model_path"], "count": len(jobs),
        "views": [j["view"] for j in jobs], "per_image_usd_micros": prices, "total_usd_micros": total,
        "resolution": jobs[0]["resolution"] if jobs else None, "dry_run": dry,
        "summary": (f"{len(jobs)} placeholder image(s), free in dry-run mode" if dry else
                    f"{len(jobs)} image(s), about {_usd(total)}, charged to {episode_id}'s reference budget"),
    }


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/estimate")
async def estimate_sheet(episode_id: str, character_id: str, version: int, request: SheetRequest,
                         authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """What a sheet step will make and cost (nothing is generated or charged)."""
    episode_id = _episode_id(episode_id)
    character = await _character_in(await _library(episode_id), character_id)
    plan, dry = await _plan_sheet(episode_id, character, version, request)
    prices = await _price_jobs(plan["jobs"], dry)
    return _estimate_out(plan, prices, dry, episode_id)

estimate_sheet.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/generate")
async def generate_sheet(episode_id: str, character_id: str, version: int, request: SheetGenerateRequest,
                         authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Make the sheet step the creator approved: 2-4 master options (from the uploaded picture or from
    the description) or the turnaround (front, 3/4, side, back and a head sheet) from the picked
    master. Refused (409) when the estimate is now above the approved total; the worker checks the
    provider estimate, the balance gate and the caps again before each paid image."""
    from hfvg.workflows.cast_sheet import CastSheetWorkflow, cast_sheet_workflow_id
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    character = await _character_in(library, character_id)
    api = _api()
    if not api.temporal_client:
        raise HTTPException(status_code=503, detail="Temporal client not initialized")
    await _refresh_sheet_jobs([character])
    if await _call(cast_store.active_sheet_jobs, character_id, version):
        raise HTTPException(status_code=409, detail="Sheet images for this version are being made; wait for them")
    plan, dry = await _plan_sheet(episode_id, character, version, request)
    prices = await _price_jobs(plan["jobs"], dry)
    total = sum(prices)
    approved = request.approved_total_usd_micros
    if total > approved:
        raise HTTPException(status_code=409, detail=(
            f"The estimate is now {_usd(total)} (you approved {_usd(approved)}); review it and approve again"))
    if not dry:
        from hfvg.balance_monitor import HIGGSFIELD_AUTO_TOPUP_ENABLED, BalanceMonitor
        passed, reason = await BalanceMonitor(api.DATABASE_PATH).check_preflight_balance(
            provider="higgsfield", estimate_usd_micros=total, auto_topup_enabled=HIGGSFIELD_AUTO_TOPUP_ENABLED)
        if not passed:
            raise HTTPException(status_code=409, detail=f"Balance check failed: {reason}")
    # each image may cost up to its share of the approved total; the worker refuses a higher estimate
    headroom = approved - total
    jobs = [{**job, "approved_usd_micros": price + (headroom * price // total if total else 0)}
            for job, price in zip(plan["jobs"], prices)]
    workflow_id = cast_sheet_workflow_id(jobs[0]["job_id"])
    for job in jobs:
        await _call(cast_store.record_sheet_job, {**job, "workflow_id": workflow_id, "requested_by": actor,
                                                  "estimated_usd_micros": job["approved_usd_micros"]})
    request_input = {"episode_id": episode_id, "character_id": character_id, "version": version,
                     "stage": plan["stage"], "jobs": jobs}
    try:
        await api.temporal_client.start_workflow(CastSheetWorkflow.run, request_input, id=workflow_id,
                                                 task_queue="hfvg-tasks")
    except Exception as e:
        for job in jobs:
            await _call(cast_store.update_sheet_job, job["job_id"], status="failed", error=f"not started: {e}")
        raise HTTPException(status_code=503, detail=f"Could not start the sheet generation: {e}") from None
    await _call(cast_store.log_event, actor, "sheet_request", episode_id=episode_id, character_id=character_id,
                details={"version": version, "stage": plan["stage"], "mode": plan["mode"],
                         "jobs": [j["job_id"] for j in jobs], "total_usd_micros": total,
                         "approved_total_usd_micros": approved, "dry_run": dry})
    return {"workflow_id": workflow_id, "jobs": [{"job_id": j["job_id"], "view": j["view"]} for j in jobs],
            **_estimate_out(plan, prices, dry, episode_id)}

generate_sheet.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/pick")
async def pick_sheet_master(episode_id: str, character_id: str, version: int, request: PickRequest,
                            authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Pick one master option; the turnaround is built from it (an earlier turnaround is discarded)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    return await _call(cast_store.pick_master, character_id, version, request.image_id, actor, episode_id=episode_id)

pick_sheet_master.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/reject")
async def reject_sheet(episode_id: str, character_id: str, version: int, request: RejectRequest,
                       authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Throw away the master options, the turnaround or the whole sheet (before it is locked)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    await _character_in(library, character_id)
    return await _call(cast_store.reject_sheet, character_id, version, request.stage, actor, episode_id=episode_id)

reject_sheet.allow_members = True


@router.post("/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/approve")
async def approve_sheet(episode_id: str, character_id: str, version: int, request: ApproveRequest,
                        authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Approve and lock the version: its sheet images become the identity references of every still
    with this character, and nothing in the version can change any more. A sheet of dry-run
    placeholders can be locked only in dry-run mode (live plans never use placeholders)."""
    episode_id = _episode_id(episode_id)
    actor, _, _ = await _actor(authorization, studio_admin_token)
    library = await _library(episode_id)
    await _require_library_writer(library, authorization, studio_admin_token)
    character = await _character_in(library, character_id)
    await _refresh_sheet_jobs([character])
    if await _call(cast_store.active_sheet_jobs, character_id, version):
        raise HTTPException(status_code=409, detail="Sheet images for this version are still being made")
    if request.allow_placeholder and not _dry_run():
        raise HTTPException(status_code=409, detail="Placeholder sheets can be locked only in dry-run mode")
    locked = await _call(cast_store.approve_version, character_id, version, actor,
                         allow_placeholder=request.allow_placeholder, episode_id=episode_id)
    return {"version": locked, "character": _character_out(await _call(cast_store.get_character, character_id))}

approve_sheet.allow_members = True


# ------------------------------------------------------------------------------------- images


async def _image_for(image_id: str) -> dict:
    if not IMAGE_ID_RE.match(image_id):
        raise HTTPException(status_code=404, detail="Image not found")
    return await _call(cast_store.get_image, image_id)


PRIVATE_HEADERS = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}


@router.get("/api/cast/images/{image_id}")
async def get_cast_image(image_id: str, authorization: str | None = Header(None),
                         studio_admin_token: str | None = Cookie(None)):
    """A cast image (members and admins only; never served as a static file)."""
    image = await _image_for(image_id)
    path = cast_store.image_path(image)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Image file not found")
    return FileResponse(path, media_type=image["mime"], headers=PRIVATE_HEADERS)

get_cast_image.allow_members = True


@router.get("/api/cast/images/{image_id}/thumb")
async def get_cast_image_thumb(image_id: str, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """A small JPEG of a cast image for the Cast panel."""
    image = await _image_for(image_id)
    if not cast_store.image_path(image).is_file():
        raise HTTPException(status_code=404, detail="Image file not found")
    thumb = await _call(cast_store.ensure_thumb, image["sha256"], image["ext"])
    return FileResponse(thumb, media_type="image/jpeg", headers=PRIVATE_HEADERS)

get_cast_image_thumb.allow_members = True
