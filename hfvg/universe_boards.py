"""Live mood boards (docs/WORLDS.md): what the worker makes for one board and where it is charged.

Pure planning, no spend; the paid call is hfvg.activities.universe_board through the guarded still
path of the character sheets, with the same provider adapter (HiggsfieldStillProvider) and the
server's still model (MODEL_PATH_STILL, only when the model caps registry says it is routed).

A live board is ``image_count`` paid still calls, one per picture. Each picture gets the board
prompt (hfvg.world_store.board_prompt) after its own focus line, and up to the model's reference
limit of the Universe's reference photos, taken in turn so that every photo reaches some picture.

Money: every call is charged to the Universe's own budget line LINE, under the ledger key
``universe-<series id>`` (never an episode id, so no episode's cap, stop or spend sees it), with a
per-job ledger row in the Universe owner's name: the dollar ledger, the credit ledger, the
dashboard's cost truth (stage "mood_boards") and the reconciler treat it like any other paid job.
The line is created on first use with cap LINE_CAP_USD_MICROS (the reference line's default) and
the usual stop threshold (EPISODE_STOP_PERCENT of the cap); it is never reset.
"""

from __future__ import annotations

from typing import Any

from hfvg import cast_store

LINE = "U1_mood_board"
LINE_CAP_USD_MICROS = 4_000_000
LEDGER_PREFIX = "universe-"
JOB_PREFIX = "uboard-"
RESOLUTION = "1k"
QUALITY = "medium"
ASPECT_RATIO = "1:1"
PICTURE_PROMPT_MAX = 2200
FOCUS = (
    "the characters together, as a group portrait",
    "the main place, seen wide",
    "the light and colour of a quiet moment",
    "textures, props and small details up close",
)


class BoardPlanError(cast_store.CastError):
    """A live board that cannot be planned (the still model cannot make it)."""
    status = 409


def ledger_key(series_id: str) -> str:
    """The key the Universe's mood board spend is booked under in the dollar and credit ledgers."""
    return f"{LEDGER_PREFIX}{series_id}"


def is_ledger_key(key: str | None) -> bool:
    return bool(key) and key.startswith(LEDGER_PREFIX)


def job_id_for(request_id: str, position: int) -> str:
    """``uboard-<request hex>-<position>``: a paid job id is never reused (each request is new)."""
    return f"{JOB_PREFIX}{request_id.split('_', 1)[-1]}-{position}"


def model_path() -> str:
    from hfvg.config import still_model_path
    return still_model_path()


def check_model(model: str, refs_needed: int) -> Any:
    """The still model's caps when the server routes it and it takes ``refs_needed`` references
    and the board's resolution, quality and aspect ratio."""
    from hfvg import model_caps
    from hfvg.cast_sheets import ROUTED_STATUSES
    from hfvg.config import max_refs_for
    from hfvg.defaults import check_value
    try:
        caps = model_caps.get_registry().resolve(model)
    except model_caps.UnknownModelError as e:
        raise BoardPlanError(f"The still model {model} is not in the model caps registry: {e}") from None
    if caps.status not in ROUTED_STATUSES:
        raise BoardPlanError(f"The still model {model} cannot make mood boards "
                             f"(status {caps.status}: not routed by the server's provider)")
    if refs_needed and max_refs_for(model) < refs_needed:
        raise BoardPlanError(f"The still model {model} takes {max_refs_for(model)} reference images; "
                             f"this picture needs {refs_needed}")
    for field, value in (("resolution", RESOLUTION), ("quality", QUALITY), ("aspect_ratio", ASPECT_RATIO)):
        why = check_value(caps, field, value)
        if why:
            raise BoardPlanError(f"Mood board {field} {value!r} is refused by the still model: {why}")
    return caps


def _prompt_limit(caps: Any) -> int:
    try:
        known = caps.limit("prompt_max_chars").value
    except (AttributeError, KeyError):
        known = None
    return min(PICTURE_PROMPT_MAX, known) if isinstance(known, int) and known > 0 else PICTURE_PROMPT_MAX


def picture_prompt(board_prompt: str, position: int, count: int, limit: int = PICTURE_PROMPT_MAX) -> str:
    """One picture's prompt: its focus first, then the board prompt (clipped to ``limit``)."""
    lead = (f"Picture {position + 1} of {count} of this mood board: {FOCUS[position % len(FOCUS)]}. "
            "One single picture, not a grid or a collage.")
    room = limit - len(lead) - 1
    body = board_prompt if len(board_prompt) <= room else board_prompt[: max(room - 1, 0)].rstrip() + "…"
    return f"{lead}\n{body}"


def picture_refs(photos: list[dict], position: int, per_picture: int) -> list[dict]:
    """The reference photos of one picture: ``per_picture`` of them, the next ones in turn."""
    if not photos or per_picture <= 0:
        return []
    start = position * per_picture
    picked: list[dict] = []
    for k in range(min(per_picture, len(photos))):
        photo = photos[(start + k) % len(photos)]
        if photo not in picked:
            picked.append(photo)
    return picked


def plan_jobs(plan: dict, *, request_id: str, series_id: str, owner_user_id: str | None) -> list[dict]:
    """The paid picture jobs of a live board (hfvg.world_store.plan_board's plan). Refused when the
    still model cannot make them."""
    from hfvg.config import max_refs_for
    model = model_path()
    caps = check_model(model, 0)
    photos = list(plan.get("photo_refs") or [])
    per_picture = min(max_refs_for(model), len(photos)) if photos else 0
    limit = _prompt_limit(caps)
    count = int(plan["image_count"])
    jobs = []
    for position in range(count):
        refs = picture_refs(photos, position, per_picture)
        jobs.append({
            "job_id": job_id_for(request_id, position), "request_id": request_id, "series_id": series_id,
            "position": position, "prompt": picture_prompt(plan["prompt"], position, count, limit),
            "refs": [str(cast_store.image_file(p["sha256"], p["ext"])) for p in refs],
            "ref_shas": [p["sha256"] for p in refs], "model_path": model, "resolution": RESOLUTION,
            "quality": QUALITY, "aspect_ratio": ASPECT_RATIO, "ledger_key": ledger_key(series_id),
            "owner_user_id": owner_user_id,
        })
    return jobs
