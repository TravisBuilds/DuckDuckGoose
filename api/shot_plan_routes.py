"""Shot plan routes: each shot's cast, props and place, and its prompts, edited before the shot plan
is approved (docs/PROPS.md). Mounted by api/main.py under the app-wide guard (``require_admin_auth``);
both routes allow members.

A change is written to the episode's shots.json, the file the run parses. Before a run it is the plan
the next run starts with. While a run waits at shot plan approval (G1, not approved yet) the
workflow's ``replan_shots`` update puts it in force: re-parsed, checked, preflighted and priced again
(hfvg/plan_resync.plan_replan). When the workflow refuses, the previous shots.json is put back and
nothing changes. At any other point of a run the plan is locked (409). Every changed field is kept in
the edit history (hfvg/shot_plan_edits.py).

Who may change it: the episode write rule (its owner or an admin; any member while it has no owner),
checked by the guard before these handlers run. Picking from a series' cast and props changes the
episode, not the series library, so no series rule applies. Who may see it: whoever may see the
episode's cast (the 404 a missing cast gets for everyone else). Nothing here spends.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Cookie, Header, HTTPException
from pydantic import BaseModel, Field

from hfvg import cast_store, paths, prop_store, shot_plan_edits
from hfvg.cast_prompt import MAX_CAST_PER_SHOT, MAX_PROPS_PER_SHOT, parse_cast_request, parse_props_request

try:  # "api.cast_routes" in tests and the e2e backend; "cast_routes" when uvicorn runs from api/
    from api import cast_routes as cast_api
except ImportError:
    import cast_routes as cast_api

router = APIRouter()

PROMPT_MAX = 6000
NOTE_MAX = 300
HISTORY_PER_SHOT = 20
SHOT_PLAN_HIDDEN = "Only the people working on this episode (and admins) can see its shot plan"
CHANGE_FIELDS = ("prompt", "end_prompt", "clip_prompt", "cast", "props", "place")
_locks: dict[str, asyncio.Lock] = {}


def register(app) -> None:
    """Add these routes to the app itself, like the cast routes (api/cast_routes.register)."""
    for route in router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)


class ShotChange(BaseModel):
    """Only the fields sent change. An empty end or clip prompt removes it (the still prompt is used);
    an empty cast, props or place removes the pick."""
    prompt: str | None = Field(None, max_length=PROMPT_MAX)
    end_prompt: str | None = Field(None, max_length=PROMPT_MAX)
    clip_prompt: str | None = Field(None, max_length=PROMPT_MAX)
    cast: list[Any] | None = Field(None, max_length=MAX_CAST_PER_SHOT)
    props: list[str] | None = Field(None, max_length=MAX_PROPS_PER_SHOT)
    place: str | None = Field(None, max_length=64)


class ShotPlanRequest(BaseModel):
    shots: dict[str, ShotChange] = Field(..., min_length=1, max_length=200)
    note: str = Field("", max_length=NOTE_MAX)


def _shots_path(episode_id: str):
    return paths.episode_dir(episode_id) / "shots.json"


def _lock(episode_id: str) -> asyncio.Lock:
    return _locks.setdefault(episode_id, asyncio.Lock())


async def _may_change(episode_id: str, authorization: str | None, cookie: str | None) -> tuple[bool, str, str | None]:
    """(may change the episode, audit actor, display name): the guard's rule, for the view."""
    actor, user_id, is_admin = await cast_api._actor(authorization, cookie)
    api = cast_api._api()
    name = None
    if user_id:
        row = await api._studio_row("SELECT name FROM users WHERE user_id = ?", (user_id,))
        name = row[0] if row and row[0] else None
    if is_admin:
        name = name or "Admin"
        return True, actor, name
    owner = await api.episode_owner(episode_id)
    return owner is None or owner == user_id, actor, name


async def _phase(episode_id: str) -> dict:
    """Whether the plan can change now: "open" (no run), "g1" (a run waits for its approval) or
    "locked", with the run id and the reason it is locked. A run that cannot answer counts as locked."""
    api = cast_api._api()
    run = await api._open_episode_run(episode_id)
    if not run:
        return {"state": "open", "run_id": None, "reason": None, "replanning": False}
    try:
        handle = api.temporal_client.get_workflow_handle(api.episode_workflow_id(episode_id), run_id=run["run_id"])
        state = await handle.query("get_state")
    except Exception:
        state = None
    state = state if isinstance(state, dict) else {}
    approved = (bool((state.get("approvals") or {}).get("g1_shot_plan"))
                or "G1_SHOT_PLAN" in (state.get("passed_gates") or []))
    if state.get("current_gate") == "G1_SHOT_PLAN" and not approved and not run.get("reopened"):
        return {"state": "g1", "run_id": run["run_id"], "reason": None, "replanning": bool(state.get("replanning"))}
    if approved or run.get("reopened"):
        reason = ("This run's shot plan is approved, so it stays as it is. Changes can be made when the run "
                  "has ended, for the next run.")
    else:
        reason = "The run is still preparing the shot plan. You can change it when it waits for your approval."
    return {"state": "locked", "run_id": run["run_id"], "reason": reason, "replanning": False}


def _character_row(character: dict) -> dict:
    versions = character.get("versions") or []
    version = next((v for v in versions if v["version"] == character.get("default_version")), None)
    version = version or (versions[0] if versions else {})
    images = [i for i in version.get("images") or [] if i.get("status") == "active"]
    image = next((i for i in images if i.get("is_primary")), images[0] if images else None)
    return {"slug": character["slug"], "name": character["name"], "role": character.get("role"),
            "thumb_url": f"/api/cast/images/{image['image_id']}/thumb" if image else None}


def _prop_row(item: dict) -> dict:
    photos = prop_store.reference_photos(item)
    return {"prop_id": item["prop_id"], "slug": item["slug"], "kind": item["kind"], "name": item["name"],
            "description": item.get("description") or "", "photos": len(photos),
            "thumb_url": f"/api/props/photos/{photos[0]['image_id']}/thumb" if photos else None}


def _read_raw(episode_id: str) -> tuple[dict, dict] | None:
    path = _shots_path(episode_id)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw, {item["id"]: item for item in raw["shots"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"shots.json cannot be read: {e}") from None


def _picks(item: dict) -> dict:
    """The shot's cast (the shots.json entries as written, so an outfit pick survives a save), props
    and place; None for a list that cannot be read."""
    sid = item.get("id")
    try:
        parse_cast_request(item.get("cast"), sid)
        cast = list(item.get("cast") or [])
    except ValueError:
        cast = None
    try:
        props, place = parse_props_request(item.get("props"), item.get("place"), sid)
    except ValueError:
        props, place = None, None
    return {"cast": cast, "props": props, "place": place}


def _plan(episode_id: str, history: list[dict]) -> dict:
    from hfvg.cast_plan import preview_episode_cast
    read = _read_raw(episode_id)
    if read is None:
        return {"uploaded": False, "shots": [], "errors": [], "notes": []}
    raw, _ = read
    try:
        report = preview_episode_cast(episode_id, raw, live=not cast_api._dry_run())
    except ValueError as e:
        report = {"shots": {}, "errors": [f"shots.json cannot be planned: {e}"], "notes": []}
    by_shot: dict[str, list[dict]] = {}
    for row in history:
        rows = by_shot.setdefault(row["shot_id"], [])
        if len(rows) < HISTORY_PER_SHOT:
            rows.append(row)
    shots, claimed = [], set()
    for item in raw["shots"]:
        sid = item["id"]
        row = report["shots"].get(sid, {})
        errors = [e for e in report["errors"] if e.startswith(f"shot {sid}:")]
        claimed.update(errors)
        shots.append({"shot_id": sid, "prompt": str(item.get("prompt") or ""),
                      "end_prompt": item.get("end_prompt"), "clip_prompt": item.get("clip_prompt"),
                      "end_frame": bool(item.get("end_frame")), "model": item.get("model"),
                      "duration": item.get("duration"), **_picks(item),
                      "refs": [r["label"] for r in row.get("refs") or []], "notes": row.get("notes") or [],
                      "errors": errors, "history": by_shot.get(sid, [])})
    return {"uploaded": True, "shots": shots, "errors": [e for e in report["errors"] if e not in claimed],
            "notes": report.get("notes") or []}


async def _view(episode_id: str, library: tuple[str, str], may_change: bool) -> dict:
    phase = await _phase(episode_id)
    characters = await cast_api._call(cast_store.list_characters, library)
    items = await cast_api._call(prop_store.list_items, library)
    history = await asyncio.to_thread(shot_plan_edits.list_edits, episode_id)
    plan = await asyncio.to_thread(_plan, episode_id, history)
    return {
        "episode_id": episode_id,
        "library": {"kind": library[0], "id": library[1]},
        "phase": phase,
        "may_change": may_change,
        "can_edit": may_change and phase["state"] in ("open", "g1") and plan["uploaded"],
        "characters": [_character_row(c) for c in characters],
        "props": [_prop_row(i) for i in items],
        **plan,
        "limits": {"max_cast_per_shot": MAX_CAST_PER_SHOT, "max_props_per_shot": MAX_PROPS_PER_SHOT,
                   "prompt_max": PROMPT_MAX, "note_max": NOTE_MAX},
    }


@router.get("/api/episodes/{episode_id}/shot-plan")
async def get_shot_plan(episode_id: str, authorization: str | None = Header(None),
                        studio_admin_token: str | None = Cookie(None)):
    """Every shot with its prompts, its cast, props and place, what they plan to (refs, notes,
    errors) and its edit history, the cast and props to pick from, and whether the plan can change
    now (``phase``) and by this caller (``can_edit``)."""
    episode_id = cast_api._episode_id(episode_id)
    library = await cast_api._library(episode_id)
    await cast_api._require_viewer(library, authorization, studio_admin_token, SHOT_PLAN_HIDDEN)
    may_change, _, _ = await _may_change(episode_id, authorization, studio_admin_token)
    return await _view(episode_id, library, may_change)

get_shot_plan.allow_members = True


def _text(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _apply(raw: dict, items: dict, request: ShotPlanRequest) -> list[dict]:
    """Apply the request to the shots.json ``raw`` (in place); the edits ([{"shot_id", "field", "old",
    "new"}], only fields whose value changed). Raises HTTPException(400) for a request that cannot be
    read; whether the picks exist is _validate's (the run's own planning)."""
    unknown = sorted(set(request.shots) - set(items))
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown shots: {unknown}")
    edits: list[dict] = []
    for sid, change in request.shots.items():
        item = items[sid]
        sent = change.model_fields_set
        new: dict[str, Any] = {}
        if "prompt" in sent:
            if not _text(change.prompt):
                raise HTTPException(status_code=400, detail=f"Shot {sid}: the picture prompt cannot be empty")
            new["prompt"] = change.prompt.strip()
        for field in ("end_prompt", "clip_prompt"):
            if field in sent:
                new[field] = _text(getattr(change, field))
        cast_api._check_text(new.get("prompt"), new.get("end_prompt"), new.get("clip_prompt"))
        if "cast" in sent:
            try:
                parse_cast_request(change.cast or [], sid)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            new["cast"] = list(change.cast or []) or None
        if "props" in sent or "place" in sent:
            props = list(change.props or []) if "props" in sent else item.get("props")
            place = _text(change.place) if "place" in sent else item.get("place")
            try:
                prop_slugs, place_slug = parse_props_request(props, place, sid)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e)) from None
            if "props" in sent:
                new["props"] = prop_slugs or None
            if "place" in sent:
                new["place"] = place_slug
        for field, value in new.items():
            old = item.get(field)
            if (old or None) == value:
                continue
            edits.append({"shot_id": sid, "field": field, "old": old, "new": value})
            if value is None:
                item.pop(field, None)
            else:
                item[field] = value
    return edits


def _validate(episode_id: str, raw: dict) -> None:
    """What the run's parse would refuse: an invalid shots.json, or cast, props and places it cannot plan."""
    from hfvg.cast_plan import cast_problems
    from hfvg.shot_data import ShotDataError, parse_shots
    try:
        parse_shots(raw)
    except (ValueError, ShotDataError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid shot plan: {e}") from None
    problems = cast_problems(episode_id, raw)
    if problems:
        raise HTTPException(status_code=400, detail="; ".join(problems[:10]))


def _write(episode_id: str, text: str) -> None:
    path = _shots_path(episode_id)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _reason(note: str, name: str | None, edits: list[dict]) -> str:
    if note.strip():
        return note.strip()
    fields: dict[str, list[str]] = {}
    for edit in edits:
        fields.setdefault(edit["shot_id"], []).append(edit["field"])
    changed = ", ".join(f"{sid} {'/'.join(f)}" for sid, f in fields.items())
    return f"Shot plan edited by {name or 'a member'}: {changed}"


@router.post("/api/episodes/{episode_id}/shot-plan")
async def update_shot_plan(episode_id: str, request: ShotPlanRequest, authorization: str | None = Header(None),
                           studio_admin_token: str | None = Cookie(None)):
    """Change some shots' prompts and their cast, props and place. Before a run: saved for the next
    run. While a run waits at shot plan approval: put in force by the workflow (refused -> 409 and the
    previous shots.json is back). At any other point of a run: 409. Returns the new view and the run's
    re-plan result (its preflight errors hold the approval until they are fixed)."""
    episode_id = cast_api._episode_id(episode_id)
    library = await cast_api._library(episode_id)
    _, actor, name = await _may_change(episode_id, authorization, studio_admin_token)
    async with _lock(episode_id):
        read = await asyncio.to_thread(_read_raw, episode_id)
        if read is None:
            raise HTTPException(status_code=404, detail="This episode has no shot list yet; upload shots.json first")
        raw, items = read
        before = await asyncio.to_thread(_shots_path(episode_id).read_text, encoding="utf-8")
        edits = _apply(raw, items, request)
        if not edits:
            return {"changed": [], "result": None, **await _view(episode_id, library, True)}
        await asyncio.to_thread(_validate, episode_id, raw)
        phase = await _phase(episode_id)
        if phase["state"] == "locked":
            raise HTTPException(status_code=409,
                                detail=f"The shot plan of {episode_id} cannot change now: {phase['reason']}")
        reason = _reason(request.note, name, edits)
        await asyncio.to_thread(_write, episode_id, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")
        result = None
        if phase["state"] == "g1":
            try:
                result = await cast_api._api()._execute_episode_update(episode_id, "replan_shots", [reason],
                                                                       "Shot plan change")
            except BaseException:
                await asyncio.to_thread(_write, episode_id, before)
                raise
        await asyncio.to_thread(shot_plan_edits.record, episode_id, edits, actor=actor, actor_name=name,
                                run_id=phase["run_id"], note=request.note.strip() or None)
        await cast_api._api()._audit(episode_id, "shot_plan_edit", reason[:1000])
        view = await _view(episode_id, library, True)
    return {"changed": [{"shot_id": e["shot_id"], "field": e["field"]} for e in edits], "result": result, **view}

update_shot_plan.allow_members = True
