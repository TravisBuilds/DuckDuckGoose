"""Character sheet jobs: what each paid image of a character sheet asks the still model for (pure
planning, no spend; the paid call is hfvg.activities.cast_sheet through the guarded still path).

Flow for one character version (an outfit):

1. ``options``: 2-4 front full-body master options from the description (text), or 1-4 from the
   version's primary reference upload (the upload is the identity reference), or, for a new outfit
   derived from a locked version, from that version's locked master (never from scene stills).
2. The creator picks one option: it becomes the master (hfvg.cast_store.pick_master).
3. ``turnaround``: front, 3/4, side and back full-body views plus a head sheet, each from the master.
4. The creator approves: the version is locked (hfvg.cast_store.approve_version) and its sheet
   images become the identity refs of every still with the character (hfvg.cast_plan).

Before the lock any stage may be rejected and generated again; each try is a new attempt (new job
ids), so a paid job id is never reused. Only the server's still model (MODEL_PATH_STILL, the model
the REST provider routes) is used, and only when the model caps registry says it is routed.
"""

from __future__ import annotations

import hashlib
import io
import json
from typing import Any

from hfvg import cast_store
from hfvg.cast_prompt import NO_TEXT_CLAUSE
from hfvg.content_policy import hard_stop_terms, precheck_banned_words

STAGES = ("options", "turnaround")
TEXT_OPTIONS = (2, 4)
REF_OPTIONS = (1, 4)
DEFAULT_TEXT_OPTIONS = 3
DEFAULT_REF_OPTIONS = 2
TURNAROUND_VIEWS = ("turnaround_front", "turnaround_three_quarter", "turnaround_side", "turnaround_back",
                    "head_sheet")
SHEET_RESOLUTION = "2k"
SHEET_QUALITY = "medium"
BODY_ASPECTS = ("2:3", "3:4", "9:16", "1:1")
HEAD_ASPECTS = ("3:2", "4:3", "16:9", "1:1")
# Statuses of a model the server really routes stills through (hfvg.preflight refuses the rest).
ROUTED_STATUSES = ("active_default_still", "active_ref_generation", "active_routed", "active")
STYLE_MAX = 200
PLACEHOLDER_BODY = (768, 1152)
PLACEHOLDER_HEAD = (1152, 768)

BACKDROP = ("Plain light grey seamless background, even soft studio lighting, no props, no other "
            "characters.")
BODY_FRAME = ("Full body from head to feet, standing upright in a neutral relaxed pose, arms slightly "
              "away from the body, the whole figure centered in frame.")
VIEW_TEXT = {
    "turnaround_front": "Seen straight from the front, facing the camera.",
    "turnaround_three_quarter": "Seen from a three-quarter front angle, body turned about 45 degrees.",
    "turnaround_side": "Seen in full profile from the side.",
    "turnaround_back": "Seen from directly behind.",
}
HEAD_SHEET_TEXT = ("A head sheet: four head-and-shoulders views of the same character side by side, evenly "
                   "spaced: front, three-quarter, profile and back, neutral expression.")


class SheetPlanError(cast_store.CastError):
    """A sheet request that cannot be planned (wrong stage, no master, model cannot do it...)."""


def sheet_model_path() -> str:
    from hfvg.config import still_model_path
    return still_model_path()


def check_sheet_model(model_path: str, refs_needed: int) -> Any:
    """The model's caps when the server routes it for stills and it takes ``refs_needed`` refs."""
    from hfvg import model_caps
    from hfvg.config import max_refs_for
    try:
        caps = model_caps.get_registry().resolve(model_path)
    except model_caps.UnknownModelError as e:
        raise SheetPlanError(f"The still model {model_path} is not in the model caps registry: {e}") from None
    if caps.status not in ROUTED_STATUSES:
        raise SheetPlanError(f"The still model {model_path} cannot be used for character sheets "
                             f"(status {caps.status}: not routed by the server's provider)")
    if refs_needed and max_refs_for(model_path) < refs_needed:
        raise SheetPlanError(f"The still model {model_path} takes {max_refs_for(model_path)} reference images; "
                             f"this step needs {refs_needed}")
    return caps


def pick_aspect(caps: Any, preferred: tuple[str, ...]) -> str:
    from hfvg.defaults import check_value
    for value in preferred:
        if check_value(caps, "aspect_ratio", value) is None:
            return value
    raise SheetPlanError(f"The still model accepts none of the aspect ratios {list(preferred)}")


def check_params(caps: Any, aspect_ratio: str) -> None:
    from hfvg.defaults import check_value
    for field, value in (("resolution", SHEET_RESOLUTION), ("quality", SHEET_QUALITY),
                         ("aspect_ratio", aspect_ratio)):
        why = check_value(caps, field, value)
        if why:
            raise SheetPlanError(f"Sheet {field} {value!r} is refused by the still model: {why}")


def episode_style(episode_id: str) -> str:
    """The episode prompt kit's ``style`` line (one line, at most STYLE_MAX characters; '' if none)."""
    from hfvg.paths import episode_dir
    path = episode_dir(episode_id) / "prompt_kit.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    style = data.get("style") if isinstance(data, dict) else None
    if not isinstance(style, str):
        return ""
    style = " ".join(style.split())
    return style[:STYLE_MAX].rsplit(" ", 1)[0] if len(style) > STYLE_MAX else style


def _style_sentence(style: str) -> str:
    return f"Visual style: {style.rstrip('.')}." if style else ""


def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def master_prompt(clause: str, mode: str, style: str, outfit: str = "") -> str:
    if mode == "text":
        lead = "Character design reference of an original character."
    elif mode == "upload":
        lead = ("Character design reference. The reference image shows this character: keep their identity "
                "exactly (same face, proportions, colors and design details).")
    else:
        lead = ("Character design reference. The reference image is this character's approved master: keep "
                "the same face, body proportions, colors and identity"
                + (f", and change only the outfit to: {outfit.rstrip('.')}." if outfit else "."))
    return _join(lead, clause, BODY_FRAME, BACKDROP, _style_sentence(style), NO_TEXT_CLAUSE)


def view_prompt(view: str, clause: str, style: str) -> str:
    lead = ("Character turnaround reference. The reference image is this character's approved master: keep "
            "the identity exactly (same face, proportions, colors and outfit).")
    if view == "head_sheet":
        return _join(lead, clause, HEAD_SHEET_TEXT, BACKDROP, _style_sentence(style), NO_TEXT_CLAUSE)
    return _join(lead, clause, VIEW_TEXT[view], BODY_FRAME, BACKDROP, _style_sentence(style), NO_TEXT_CLAUSE)


def prompt_problems(prompt: str) -> list[str]:
    """Content-policy words in a sheet prompt (the character text is checked when it is written too)."""
    out = [f"hard-stop term {t!r}" for t in hard_stop_terms(prompt)]
    out += [f"banned word {w!r}" for w in precheck_banned_words(prompt)]
    return out


def _active(version: dict, role: str) -> list[dict]:
    return [i for i in version.get("images") or [] if i.get("role") == role and i.get("status") == "active"]


def options_mode(version: dict, source: str) -> tuple[str, list[dict]]:
    """("outfit" | "upload" | "text", [reference image rows]) of an options request."""
    if version.get("derived_from_version"):
        return "outfit", []
    uploads = sorted(_active(version, cast_store.UPLOAD_ROLE), key=lambda i: not i.get("is_primary"))
    if source == "upload":
        if not uploads:
            raise SheetPlanError("Upload a reference image first, or generate the sheet from the description")
        return "upload", uploads[:1]
    if source == "text":
        return "text", []
    if source != "auto":
        raise SheetPlanError("source must be auto, upload or text")
    return ("upload", uploads[:1]) if uploads else ("text", [])


def _derived_master(character: dict, version: dict) -> dict:
    src = next((v for v in character["versions"] if v["version"] == version["derived_from_version"]), None)
    if not src or not src.get("locked"):
        raise SheetPlanError("The version this outfit is derived from is not locked")
    masters = _active(src, cast_store.MASTER_ROLE)
    if not masters:
        raise SheetPlanError("The locked version this outfit is derived from has no master image")
    return masters[0]


def job_id_for(character_id: str, version: int, view: str, attempt: int) -> str:
    return f"castsheet-{character_id}-v{version}-{view}-a{attempt}"


def plan_jobs(character: dict, version_no: int, stage: str, *, episode_id: str, count: int | None = None,
              source: str = "auto", views: list[str] | None = None, live: bool) -> dict[str, Any]:
    """The jobs of one sheet request: ``{"stage", "mode", "model_path", "jobs": [...]}``. Each job:
    job_id, character_id, version, stage, view (the image role it makes), attempt, prompt, refs
    (local files), ref_shas, aspect_ratio, resolution, quality, model_path, episode_id."""
    if stage not in STAGES:
        raise SheetPlanError(f"stage must be one of {list(STAGES)}")
    version = next((v for v in character["versions"] if v["version"] == version_no), None)
    if version is None:
        raise cast_store.CastNotFound(f"Version {version_no} of {character['name']} not found")
    if version.get("locked"):
        raise cast_store.CastConflict(f"Version {version_no} is locked; create a new version for a new outfit")
    model_path = sheet_model_path()
    clause = version["clause"]
    style = episode_style(episode_id)
    if stage == "options":
        mode, ref_rows = options_mode(version, source)
        if mode == "outfit":
            ref_rows = [_derived_master(character, version)]
        lo, hi = TEXT_OPTIONS if mode == "text" else REF_OPTIONS
        n = count if count is not None else (DEFAULT_TEXT_OPTIONS if mode == "text" else DEFAULT_REF_OPTIONS)
        if isinstance(n, bool) or not isinstance(n, int) or not lo <= n <= hi:
            raise SheetPlanError(f"Ask for {lo} to {hi} master options")
        specs = [(cast_store.OPTION_ROLE, master_prompt(clause, mode, style, version.get("outfit") or ""))] * n
        aspects = [BODY_ASPECTS] * n
    else:
        masters = _active(version, cast_store.MASTER_ROLE)
        if not masters:
            raise SheetPlanError("Pick a master option first; the turnaround is built from it")
        mode, ref_rows = "master", masters[:1]
        wanted = list(views or TURNAROUND_VIEWS)
        unknown = [v for v in wanted if v not in TURNAROUND_VIEWS]
        if unknown or not wanted or len(set(wanted)) != len(wanted):
            raise SheetPlanError(f"views must be distinct values of {list(TURNAROUND_VIEWS)}")
        specs = [(v, view_prompt(v, clause, style)) for v in wanted]
        aspects = [HEAD_ASPECTS if v == "head_sheet" else BODY_ASPECTS for v in wanted]
    if live and any(r.get("placeholder") for r in ref_rows):
        raise SheetPlanError("The reference for this step is a dry-run placeholder; generate it live first")
    caps = check_sheet_model(model_path, len(ref_rows))
    refs = [str(cast_store.image_file(r["sha256"], r["ext"])) for r in ref_rows]
    jobs = []
    attempts: dict[str, int] = {}
    for (view, prompt), prefs in zip(specs, aspects):
        problems = prompt_problems(prompt)
        if problems:
            raise SheetPlanError("The character text uses words the studio cannot generate: " + ", ".join(problems))
        aspect_ratio = pick_aspect(caps, prefs)
        check_params(caps, aspect_ratio)
        if view not in attempts:
            attempts[view] = cast_store.next_attempt(character["character_id"], version_no, view)
        else:
            attempts[view] += 1
        attempt = attempts[view]
        jobs.append({
            "job_id": job_id_for(character["character_id"], version_no, view, attempt),
            "character_id": character["character_id"], "version": version_no, "stage": stage, "view": view,
            "attempt": attempt, "prompt": prompt, "refs": refs, "ref_shas": [r["sha256"] for r in ref_rows],
            "aspect_ratio": aspect_ratio, "resolution": SHEET_RESOLUTION, "quality": SHEET_QUALITY,
            "model_path": model_path, "episode_id": episode_id,
        })
    return {"stage": stage, "mode": mode, "model_path": model_path, "jobs": jobs}


def placeholder_image(job: dict) -> bytes:
    """A deterministic dry-run stand-in for a sheet image (a plain figure shape, no text)."""
    from PIL import Image, ImageDraw
    digest = hashlib.sha256(job["job_id"].encode()).digest()
    size = PLACEHOLDER_HEAD if job["view"] == "head_sheet" else PLACEHOLDER_BODY
    bg = (200 + digest[0] % 40, 200 + digest[1] % 40, 200 + digest[2] % 40)
    fg = (60 + digest[3] % 120, 60 + digest[4] % 120, 60 + digest[5] % 120)
    img = Image.new("RGB", size, bg)
    draw = ImageDraw.Draw(img)
    w, h = size
    if job["view"] == "head_sheet":
        for i in range(4):
            cx = w * (2 * i + 1) // 8
            draw.ellipse((cx - w // 14, h // 3, cx + w // 14, h // 3 + w // 7), fill=fg)
    else:
        draw.ellipse((w * 3 // 8, h // 10, w * 5 // 8, h // 10 + w // 4), fill=fg)
        draw.rectangle((w * 5 // 16, h // 10 + w // 4, w * 11 // 16, h * 7 // 10), fill=fg)
        draw.rectangle((w * 3 // 8, h * 7 // 10, w * 15 // 32, h * 19 // 20), fill=fg)
        draw.rectangle((w * 17 // 32, h * 7 // 10, w * 5 // 8, h * 19 // 20), fill=fg)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
