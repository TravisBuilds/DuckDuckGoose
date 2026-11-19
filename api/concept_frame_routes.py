"""First frames routes (docs/CONCEPT-FRAMES.md): each shot's start frame options, made and picked
before filming. Mounted by api/main.py under the app-wide guard (``require_admin_auth``); every route
allows members.

Who may make and pick: the episode write rule (its owner or an admin; any member while it has no
owner), checked by the guard before these handlers run. Who may see: whoever may see the episode's
cast (the 404 a missing cast gets for everyone else); the shot's words and story beat only whoever
may read its prompts (cast_api._may_see_prompts).

When: only before a run starts (the shot plan's phase "open"). The pick is written into shots.json as
the shot's pre-approved start still, which a run reads when it starts and keeps fixed from then on.

Money: in dry run the options are free placeholders made here. Live: the episode must be in live
mode; a worker must poll the first frames task queue (409 when the running worker predates them);
the provider prices each option (503 when it gives no price: nothing is made); the total must be
within what the owner approved (409) and within the episode's draft pictures budget line (409); the
recorded balance must be fresh and the balance gate must pass (409). Then the round is recorded and
ConceptFrameWorkflow makes it, checking each option's price, the balance gate and the budget again
before paying. There is no override for any of these checks.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hfvg import cast_store, concept_frames

try:  # "api.cast_routes" in tests and the e2e backend; "cast_routes" when uvicorn runs from api/
    from api import cast_routes as cast_api
    from api import shot_plan_routes as plan_api
except ImportError:
    import cast_routes as cast_api
    import shot_plan_routes as plan_api

router = APIRouter()
logger = logging.getLogger(__name__)

# The task queue the API starts studio workflows on (first frames: its "-frames" queue).
TASK_QUEUE = "hfvg-tasks"
START_GRACE = timedelta(minutes=2)
# Far above a round's own limits (four pictures of at most 12 minutes each): a round no worker picks
# up ends instead of waiting forever.
RUN_TIMEOUT = timedelta(hours=2)
ESTIMATE_SET_ID = "cf_000000000000"
FRAMES_HIDDEN = "Only the people working on this episode (and admins) can see its first frames"
NOT_OPEN = ("First frames are picked before a video starts filming, and this episode has a run going now. Pick them "
            "for the next run once it ends.")
NOT_LIVE = ("Paid first frames are charged to this episode, which is not in live mode yet. Switch it to live mode "
            "first (in dry-run mode the studio makes free placeholders).")
WORKER_MISSING = ("First frames are made by the studio's worker, and the worker running now cannot make them yet. "
                  "Nothing was charged. Ask an admin to update the worker, then try again.")
WORKER_UNREACHABLE = ("The studio's worker cannot be reached right now, so no first frames were started. Nothing was "
                      "charged; try again in a minute.")
NOT_STARTED = ("The studio's worker could not be asked to make the first frames, so nothing was made or charged. "
               "Try again in a minute.")


def register(app) -> None:
    """Add these routes to the app itself, like the cast routes (api/cast_routes.register)."""
    for route in router.routes:
        app.add_api_route(route.path, route.endpoint, methods=sorted(route.methods), name=route.name)


class FrameRequest(BaseModel):
    """``change``: what to change from the last round, in plain words (kept for later rounds);
    ``keep_changes``: false starts again from the shot as it is, without the earlier changes."""
    change: str = Field("", max_length=concept_frames.CHANGE_MAX + 50)
    keep_changes: bool = True


class FrameMakeRequest(FrameRequest):
    approved_total_usd_micros: int = Field(..., ge=0)


class FramePickRequest(BaseModel):
    option_id: str = Field(..., max_length=40)


def _shot_id(raw: str) -> str:
    if not concept_frames.SHOT_ID_RE.match(raw or ""):
        raise HTTPException(status_code=404, detail=f"Shot {raw} is not in this episode's shot list")
    return raw


def _live() -> bool:
    return not cast_api._dry_run()


async def _editor(episode_id: str, authorization: str | None, cookie: str | None) -> dict:
    """The caller who may change the episode (the guard's rule), with the actor and display name."""
    may_change, actor, name = await plan_api._may_change(episode_id, authorization, cookie)
    if not may_change:
        raise HTTPException(status_code=403, detail="Only this episode's owner or an admin can change its first frames")
    return {"actor": actor, "name": name}


async def _require_open(episode_id: str) -> None:
    phase = await plan_api._phase(episode_id)
    if phase["state"] != "open":
        raise HTTPException(status_code=409, detail=NOT_OPEN)


async def _refresh(episode_id: str) -> None:
    """A live round whose workflow ended without finishing it (a crashed worker, a cancelled run) is
    finished here with the options it made (the reconciler settles any money still held). A workflow
    Temporal does not know counts as ended only once the round is older than START_GRACE (it is
    recorded just before its workflow starts)."""
    from temporalio.service import RPCError, RPCStatusCode
    api = cast_api._api()
    if not api.temporal_client:
        return
    for row in await cast_api._call(concept_frames.making_sets, episode_id):
        if not row["workflow_id"]:
            continue
        try:
            desc = await api.temporal_client.get_workflow_handle(row["workflow_id"]).describe()
            ended = api._run_ended(desc.status)
        except RPCError as e:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(row["created_at"])
            ended = e.status == RPCStatusCode.NOT_FOUND and age > START_GRACE
        except Exception:  # noqa: BLE001 - unknown: keep waiting
            ended = False
        if ended:
            await cast_api._call(concept_frames.finish_set, row["set_id"], None, "api")


async def _worker_problem() -> tuple[int, str] | None:
    """Why a live round cannot be started now, as (status, plain message); None while a worker polls
    the first frames task queue. Nothing is asked of a provider or charged before this passes."""
    from hfvg.workflows.concept_frame import frames_task_queue, worker_polls
    client = cast_api._api().temporal_client
    if client is None:
        return 503, WORKER_UNREACHABLE
    try:
        polling = await worker_polls(client, TASK_QUEUE)
    except Exception as e:  # noqa: BLE001 - the owner gets the plain message, the log the reason
        logger.warning("Could not ask Temporal which workers poll the first frames queue: %s", e)
        return 503, WORKER_UNREACHABLE
    if not polling:
        logger.warning("No worker polls %s: the running worker predates first frames", frames_task_queue(TASK_QUEUE))
        return 409, WORKER_MISSING
    return None


async def _require_live_ready(episode_id: str) -> None:
    if not await cast_api._live_mode(episode_id):
        raise HTTPException(status_code=409, detail=NOT_LIVE)
    problem = await _worker_problem()
    if problem:
        raise HTTPException(status_code=problem[0], detail=problem[1])


async def _budget(episode_id: str) -> dict:
    """The episode's draft pictures line: cap, stop and what is left before the stop (the line is
    created with these figures when the first paid picture of the episode is reserved)."""
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(cast_api._api().DATABASE_PATH)
    await ledger.init_db()
    try:
        line = await ledger.get_line_status(episode_id, concept_frames.LINE)
    except ValueError:
        cap, stop = (await asyncio.to_thread(ledger.target_line_caps_and_stops, episode_id)).get(
            concept_frames.LINE, (0, 0))
        return {"cap_usd_micros": cap, "stop_usd_micros": stop, "spent_usd_micros": 0, "reserved_usd_micros": 0,
                "available_usd_micros": stop}
    return {"cap_usd_micros": line["budget_cap"], "stop_usd_micros": line["stop_threshold"],
            "spent_usd_micros": line["spent"], "reserved_usd_micros": line["reserved"],
            "available_usd_micros": max(line["available"], 0)}


def _dry_prices(count: int) -> list[int]:
    from hfvg.estimates import still_call_usd_micros
    return [still_call_usd_micros() for _ in range(count)]


def _estimate_out(plan: dict, prices: list[int], dry: bool, budget: dict | None = None) -> dict:
    total = sum(prices)
    count = len(prices)
    if dry:
        summary = f"{count} placeholder pictures, free in dry-run mode"
    else:
        summary = (f"{count} pictures, about {cast_api._usd(total)}, charged to this episode's budget for draft "
                   f"pictures ({cast_api._usd(budget['available_usd_micros'])} left)")
    return {"shot_id": plan["shot_id"], "round": plan["round"], "count": count, "per_image_usd_micros": prices,
            "total_usd_micros": total, "dry_run": dry, "changes": plan["changes"], **plan["summary"],
            "notes": plan["notes"], "budget": budget, "summary": summary}


def _estimate_above(total: int, approved: int) -> HTTPException:
    now, then = cast_api._usd_pair(total, approved)
    return HTTPException(status_code=409, detail=(
        f"The estimate is now {now} (you approved {then}); review it and approve again"))


async def _plan(episode_id: str, shot_id: str, request: FrameRequest) -> dict:
    cast_api._check_text(request.change)
    return await cast_api._call(concept_frames.plan_round, episode_id, shot_id, change=request.change,
                                keep_changes=request.keep_changes, live=_live())


async def _view_meta(episode_id: str, authorization: str | None, cookie: str | None) -> dict:
    may_change, _, _ = await plan_api._may_change(episode_id, authorization, cookie)
    phase = await plan_api._phase(episode_id)
    return {"may_change": may_change, "phase": phase["state"],
            "locked_reason": None if phase["state"] == "open" else NOT_OPEN,
            "can_change": may_change and phase["state"] == "open"}


@router.get("/api/episodes/{episode_id}/concept-frames")
async def get_concept_frames(episode_id: str, authorization: str | None = Header(None),
                             studio_admin_token: str | None = Cookie(None)):
    """Every shot with what its first frame shows (characters, place, props, references), its rounds
    of options and its pick, and whether the caller may make and pick them now (``can_change``). The
    shot's words and story beat only for a caller who may read the episode's prompts."""
    episode_id = cast_api._episode_id(episode_id)
    library = await cast_api._library(episode_id)
    await cast_api._require_viewer(library, authorization, studio_admin_token, FRAMES_HIDDEN)
    _, user_id, is_admin = await cast_api._actor(authorization, studio_admin_token)
    with_words = await cast_api._may_see_prompts(episode_id, user_id, is_admin)
    await _refresh(episode_id)
    view = await cast_api._call(concept_frames.frames_view, episode_id, live=_live(), with_words=with_words)
    return {**view, **await _view_meta(episode_id, authorization, studio_admin_token)}

get_concept_frames.allow_members = True


@router.post("/api/episodes/{episode_id}/concept-frames/{shot_id}/estimate")
async def estimate_concept_frames(episode_id: str, shot_id: str, request: FrameRequest,
                                  authorization: str | None = Header(None),
                                  studio_admin_token: str | None = Cookie(None)):
    """What the next round of options for a shot makes and costs (nothing is generated or charged).
    Live: the provider's own price for each option, and what is left on the episode's draft
    pictures budget."""
    episode_id, shot_id = cast_api._episode_id(episode_id), _shot_id(shot_id)
    await _editor(episode_id, authorization, studio_admin_token)
    await _require_open(episode_id)
    await _refresh(episode_id)
    if not _live():
        plan = await _plan(episode_id, shot_id, request)
        return _estimate_out(plan, _dry_prices(plan["count"]), True)
    await _require_live_ready(episode_id)
    plan = await _plan(episode_id, shot_id, request)
    jobs = concept_frames.plan_jobs(plan, set_id=ESTIMATE_SET_ID)
    prices = await cast_api._price_jobs(jobs, False)
    return _estimate_out(plan, prices, False, await _budget(episode_id))

estimate_concept_frames.allow_members = True


async def _start_live(episode_id: str, plan: dict, who: dict, approved: int) -> dict:
    """Ask the worker for a round the owner approved: the estimate within the approval and the
    budget line, the recorded balance (GC.01) and the balance gate, then the round and its options,
    then ConceptFrameWorkflow."""
    from hfvg.balance_monitor import HIGGSFIELD_AUTO_TOPUP_ENABLED, BalanceMonitor
    from hfvg.budget import BudgetLedger, balance_freshness
    from hfvg.workflows.concept_frame import (
        ConceptFrameWorkflow,
        concept_frame_workflow_id,
        frames_task_queue,
    )
    set_id = concept_frames.new_set_id()
    jobs = concept_frames.plan_jobs(plan, set_id=set_id)
    prices = await cast_api._price_jobs(jobs, False)
    total = sum(prices)
    if total > approved:
        raise _estimate_above(total, approved)
    budget = await _budget(episode_id)
    if total > budget["available_usd_micros"]:
        raise HTTPException(status_code=409, detail=(
            f"This episode's budget for draft pictures has {cast_api._usd(budget['available_usd_micros'])} left before "
            f"its stop, and these first frames cost about {cast_api._usd(total)}. Nothing was charged; ask an admin "
            "about the budget."))
    api = cast_api._api()
    fresh = balance_freshness(await BudgetLedger(api.DATABASE_PATH).get_balance_status())
    if not fresh.get("ok"):
        raise HTTPException(status_code=409, detail=f"Nothing was charged: {fresh['reason']}")
    passed, reason = await BalanceMonitor(api.DATABASE_PATH).check_preflight_balance(
        provider="higgsfield", estimate_usd_micros=total, auto_topup_enabled=HIGGSFIELD_AUTO_TOPUP_ENABLED)
    if not passed:
        raise HTTPException(status_code=409, detail=f"Balance check failed: {reason}")
    # each option may cost up to its share of the approved total; the worker refuses a higher estimate
    headroom = approved - total
    jobs = [{**job, "estimated_usd_micros": price,
             "approved_usd_micros": price + (headroom * price // total if total else 0)}
            for job, price in zip(jobs, prices)]
    workflow_id = concept_frame_workflow_id(set_id)
    made = await cast_api._call(concept_frames.start_set, plan, jobs, set_id=set_id, workflow_id=workflow_id,
                                estimate_usd_micros=total, approved_total_usd_micros=approved,
                                requested_by=who["actor"])
    try:
        await api.temporal_client.start_workflow(
            ConceptFrameWorkflow.run,
            {"set_id": set_id, "episode_id": episode_id, "shot_id": plan["shot_id"], "jobs": jobs},
            id=workflow_id, task_queue=frames_task_queue(TASK_QUEUE), execution_timeout=RUN_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - nothing ran: the round ends here, the log keeps the reason
        logger.error("Could not start first frames %s: %s", workflow_id, e)
        await cast_api._call(concept_frames.finish_set, set_id, NOT_STARTED, who["actor"])
        raise HTTPException(status_code=503, detail=NOT_STARTED) from None
    return {"round": made, "estimate": _estimate_out(plan, prices, False, budget)}


@router.post("/api/episodes/{episode_id}/concept-frames/{shot_id}")
async def make_concept_frames(episode_id: str, shot_id: str, request: FrameMakeRequest,
                              authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Make the round of options the owner approved the estimate of (the first, another try, or one
    with a change in plain words). Refused (409) when the estimate is now above the approved total.
    Dry run: placeholder options, made here. Live: after the budget line and balance checks the
    worker makes them one by one (ConceptFrameWorkflow), checking each option's price against the
    approval, the balance gate and the budget line again before paying; the page follows it."""
    episode_id, shot_id = cast_api._episode_id(episode_id), _shot_id(shot_id)
    who = await _editor(episode_id, authorization, studio_admin_token)
    await _require_open(episode_id)
    await _refresh(episode_id)
    approved = request.approved_total_usd_micros
    if _live():
        await _require_live_ready(episode_id)
        plan = await _plan(episode_id, shot_id, request)
        made = await _start_live(episode_id, plan, who, approved)
    else:
        plan = await _plan(episode_id, shot_id, request)
        prices = _dry_prices(plan["count"])
        total = sum(prices)
        if total > approved:
            raise _estimate_above(total, approved)
        made = {"round": await cast_api._call(concept_frames.make_dry_set, plan, estimate_usd_micros=total,
                                              approved_total_usd_micros=approved, requested_by=who["actor"]),
                "estimate": _estimate_out(plan, prices, True)}
    shot = await cast_api._call(concept_frames.shot_view, episode_id, shot_id, live=_live())
    return {**made, "shot": shot}

make_concept_frames.allow_members = True


@router.post("/api/episodes/{episode_id}/concept-frames/{shot_id}/pick")
async def pick_concept_frame(episode_id: str, shot_id: str, request: FramePickRequest,
                             authorization: str | None = Header(None), studio_admin_token: str | None = Cookie(None)):
    """Use one option as the shot's start frame: copied into the episode and written into its shot
    list as the shot's pre-approved start still (the run starts the shot from it and does not pay
    for that still). Only before a run starts; refused for an option made from an earlier version of
    the shot and for a start frame set in the shot list by hand."""
    episode_id, shot_id = cast_api._episode_id(episode_id), _shot_id(shot_id)
    who = await _editor(episode_id, authorization, studio_admin_token)
    async with plan_api._lock(episode_id):
        await _require_open(episode_id)
        shot = await cast_api._call(concept_frames.pick, episode_id, shot_id, request.option_id,
                                    actor=who["actor"], approved_by=who["name"] or who["actor"], live=_live())
    await cast_api._api()._audit(episode_id, "concept_frame_pick", f"First frame of {shot_id} picked: "
                                 f"{request.option_id}")
    return {"shot": shot}

pick_concept_frame.allow_members = True


@router.post("/api/episodes/{episode_id}/concept-frames/{shot_id}/unpick")
async def unpick_concept_frame(episode_id: str, shot_id: str, authorization: str | None = Header(None),
                               studio_admin_token: str | None = Cookie(None)):
    """Stop using the first frame picked here: the run makes the shot's start still again."""
    episode_id, shot_id = cast_api._episode_id(episode_id), _shot_id(shot_id)
    who = await _editor(episode_id, authorization, studio_admin_token)
    async with plan_api._lock(episode_id):
        await _require_open(episode_id)
        shot = await cast_api._call(concept_frames.unpick, episode_id, shot_id, actor=who["actor"], live=_live())
    await cast_api._api()._audit(episode_id, "concept_frame_unpick", f"First frame of {shot_id} taken off")
    return {"shot": shot}

unpick_concept_frame.allow_members = True


async def _picture(episode_id: str, option_id: str, authorization: str | None, cookie: str | None) -> dict:
    episode_id = cast_api._episode_id(episode_id)
    library = await cast_api._library(episode_id)
    await cast_api._require_viewer(library, authorization, cookie, FRAMES_HIDDEN)
    picture = await cast_api._call(concept_frames.option_picture, episode_id, option_id)
    if not await asyncio.to_thread(picture["path"].is_file):
        raise HTTPException(status_code=404, detail="Image file not found")
    return picture


@router.get("/api/episodes/{episode_id}/concept-frames/images/{option_id}")
async def get_concept_frame_image(episode_id: str, option_id: str, authorization: str | None = Header(None),
                                  studio_admin_token: str | None = Cookie(None)):
    """An option's picture, for whoever may see the episode's cast (never served as a static file)."""
    picture = await _picture(episode_id, option_id, authorization, studio_admin_token)
    return FileResponse(picture["path"], media_type=picture["mime"], headers=cast_api.PRIVATE_HEADERS)

get_concept_frame_image.allow_members = True


@router.get("/api/episodes/{episode_id}/concept-frames/images/{option_id}/thumb")
async def get_concept_frame_thumb(episode_id: str, option_id: str, authorization: str | None = Header(None),
                                  studio_admin_token: str | None = Cookie(None)):
    """A small JPEG of an option's picture (same rule as the picture)."""
    picture = await _picture(episode_id, option_id, authorization, studio_admin_token)
    thumb = await cast_api._call(cast_store.ensure_thumb, picture["sha256"], picture["ext"])
    return FileResponse(thumb, media_type="image/jpeg", headers=cast_api.PRIVATE_HEADERS)

get_concept_frame_thumb.allow_members = True
