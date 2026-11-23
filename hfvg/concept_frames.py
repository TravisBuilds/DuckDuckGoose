"""First frames (docs/CONCEPT-FRAMES.md): the picture a shot starts from, picked before filming.

For one shot the studio writes the picture prompt itself from what the episode already knows: the
shot's story beat (BEATMAP.md action, purpose and time of day), its picture words (shots.json), its
characters, place and props with their descriptions and reference pictures (hfvg.cast_plan: the
same picks and the same reference limit as the run), the Universe's logline and the episode's style
line. A prompt adapter, chosen from the server's still model (MODEL_PATH_STILL), lays these out the
way that model reads best. Nobody picks a model and no view names one.

A round makes ``options_per_round()`` options (CONCEPT_FRAME_OPTIONS, default 3, 1 to 4), each with a
slightly different camera idea. The owner picks one, asks for another round, or adds a change in
plain words ("make it night", "closer on her face") that every later round writes into the prompt
(the last CHANGES_KEPT of them, until cleared). The picked option is copied into the episode
(``concept_frames/`` next to shots.json) and written as the shot's ``pre_approved_still`` (path,
sha256, approved_by): the run starts the shot from it and skips that still's spend
(hfvg.pre_approved). Only before a run starts (the API checks), only an option made from the shot
as it is now, and never over a start frame someone set in shots.json by hand.

Money: in dry run the API makes free placeholder options. Live, every option is one paid still job
made by the worker (hfvg.activities.concept_frame) on the guarded path of the character sheets:
provider estimate (fail closed), the owner's approved total, the balance gate, credit and dollar
holds on the episode's draft stills line (LINE), an idempotent submit, a job ledger row in the
episode owner's name and the reconciler for a call whose result is not known.

Tables from hfvg.migrations.CONCEPT_FRAMES_MIGRATION. Sync sqlite3 like the cast store; the API
calls these through a worker thread.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from hfvg import cast_store, paths
from hfvg.cast_store import (
    CastConflict,
    CastError,
    CastNotFound,
    _new_id,
    _now,
    _tx,
    audit,
    connect,
)

logger = logging.getLogger(__name__)

LINE = "L2_drafts"
JOB_PREFIX = "cframe-"
FRAMES_DIR = "concept_frames"
OPTIONS_ENV = "CONCEPT_FRAME_OPTIONS"
OPTIONS_DEFAULT = 3
OPTIONS_MIN = 1
OPTIONS_MAX = 4
CHANGE_MAX = 200
CHANGES_KEPT = 4
ROUNDS_MAX = 20
BEAT_MAX = 500
LOGLINE_MAX = 200
PLACEHOLDER_LONG_SIDE = 1024
PICTURE_MAX_BYTES = 40 * 1024 * 1024
OPTION_STATUSES = ("queued", "running", "completed", "failed", "refused", "pending_reconcile", "skipped")
SET_ID_RE = re.compile(r"^cf_[0-9a-f]{12}$")
OPTION_ID_RE = re.compile(r"^cframe-[0-9a-f]{12}-[0-3]$")
SHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

BEING_MADE = "First frames for this shot are being made; wait for them to finish"
STOPPED = "The studio's worker stopped before every option was made."
CHANGED = "This shot changed since these options were made, so they no longer match it. Make new ones to pick from."
NOT_OURS = ("This shot's start frame was set in its shot list, not picked here, so it stays. Remove it from the "
            "shot list first to pick one here.")
TOO_MANY_ROUNDS = f"This shot has had {ROUNDS_MAX} rounds of first frames; pick one of them"

# Each option of a round tries its own camera idea on the same moment (none of them fights a
# framing change the owner asked for).
VARIANTS = (
    "",
    "Use a slightly different camera angle on the same moment.",
    "Show the same moment a breath later.",
    "Use a slightly different composition of the same moment.",
)

# Plain words the owner may use in a change, and the picture terms they mean (written next to the
# owner's own words, which always come first).
CHANGE_TERMS = (
    (r"\b(night|night-?time|midnight)\b", "time of day: night, moonlight and practical lights"),
    (r"\b(sunset|golden hour|dusk|evening)\b", "light: warm low sun, golden hour"),
    (r"\b(sunrise|dawn|morning)\b", "light: soft early-morning light"),
    (r"\b(daytime|daylight|midday|noon)\b", "time of day: daytime, natural daylight"),
    (r"\b(closer|close-?up|zoom in|tighter)\b", "framing: closer, a close-up"),
    (r"\b(wider|zoom out|pull back|further away|wide shot)\b", "framing: wider, more of the place in view"),
    (r"\b(from above|overhead|top-?down|high angle)\b", "camera: high angle, looking down"),
    (r"\b(from below|low angle|looking up)\b", "camera: low angle, looking up"),
    (r"\b(rain|rainy|raining)\b", "weather: rain"),
    (r"\b(fog|foggy|mist|misty)\b", "weather: fog and mist"),
    (r"\b(snow|snowy|snowing)\b", "weather: falling snow"),
    (r"\b(warmer|warm colou?rs?)\b", "colour: warmer tones"),
    (r"\b(cooler|colder|cool colou?rs?)\b", "colour: cooler tones"),
    (r"\b(brighter|lighter)\b", "light: brighter"),
    (r"\b(darker|moodier|moody)\b", "light: darker and moodier"),
    (r"\b(smil(e|es|ing))\b", "expression: smiling"),
)
TIME_WORDS = re.compile(r"\b(night|midnight|sunset|golden hour|dusk|evening|sunrise|dawn|morning|daytime|"
                        r"daylight|midday|noon)\b", re.IGNORECASE)

# What the owner's changes ask for, in the few words the page's description of a shot uses; for each
# kind the latest change that names one wins.
CHANGE_LOOKS = (
    ("framing", r"\b(closer|close-?up|zoom in|tighter)\b", "close-up"),
    ("framing", r"\b(wider|zoom out|pull back|further away|wide shot)\b", "wide"),
    ("angle", r"\b(from above|overhead|top-?down|high angle)\b", "from above"),
    ("angle", r"\b(from below|low angle|looking up)\b", "from below"),
    ("weather", r"\b(rain|rainy|raining)\b", "in the rain"),
    ("weather", r"\b(fog|foggy|mist|misty)\b", "in fog"),
    ("weather", r"\b(snow|snowy|snowing)\b", "in snow"),
    ("colour", r"\b(warmer|warm colou?rs?)\b", "warmer colours"),
    ("colour", r"\b(cooler|colder|cool colou?rs?)\b", "cooler colours"),
    ("light", r"\b(brighter|lighter)\b", "brighter"),
    ("light", r"\b(darker|moodier|moody)\b", "darker"),
    ("expression", r"\b(smil(e|es|ing))\b", "smiling"),
)
CLOSE_ON = re.compile(r"\b(?:closer|close-?up|zoom in|tighter)\s+(?:on|of)\s+((?:her|his|their|its|the)\s+[a-z]+)",
                      re.IGNORECASE)
# A beat that starts with a camera label ("cinematic shot: ...", "reaction close-up: ...", "whip pans:
# ...") is picture wording first; the page shows the moment after the label. A label of camera words,
# or one in lower case (a name or "Note:" is capitalised), is left out.
CAMERA_LABEL = re.compile(r"^\s*([^:.!?]{1,48}):\s+(?=\S)")
CAMERA_WORDS = frozenset((
    "aerial", "angle", "cinematic", "close", "close-up", "close-ups", "closeup", "coverage", "crane", "dolly",
    "establishing", "framing", "grade", "handheld", "insert", "inserts", "interiors", "exteriors", "locked-off",
    "macro", "medium", "mediums", "montage", "ots", "overhead", "pan", "pov", "push-in", "reveal", "shot", "shots",
    "slow-mo", "static", "still", "tilt", "tracking", "two-shot", "two-shots", "wide", "wides", "zoom"))
# Beatmap rooms that name no place (the test drafts' "Scene").
NO_ROOM = frozenset(("", "-", "–", "scene", "set", "location", "n/a", "none", "various", "tbd"))
NO_PHOTO_NOTE = re.compile(r"^(.+) has no (?:reference images|photo) yet: only the description is used$")
NAMES_SHOWN = 3


class FramePlanError(CastError):
    """First frames that cannot be planned now (the shot or the still model cannot make them)."""
    status = 409


def options_per_round() -> int:
    """How many options a round makes: CONCEPT_FRAME_OPTIONS (default 3), kept within 1 to 4."""
    raw = os.getenv(OPTIONS_ENV, "").strip()
    try:
        count = int(raw) if raw else OPTIONS_DEFAULT
    except ValueError:
        logger.warning("%s=%r is not a number; using %d", OPTIONS_ENV, raw, OPTIONS_DEFAULT)
        count = OPTIONS_DEFAULT
    return max(OPTIONS_MIN, min(OPTIONS_MAX, count))


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").replace("★", " ").replace("⇥", " ").split())


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 0)].rstrip() + "…"


def clean_change(value: Any) -> str | None:
    """A change in plain words, on one line (None when empty). Refused when too long."""
    text = _one_line(value)
    if not text:
        return None
    if len(text) > CHANGE_MAX:
        raise CastError(f"Keep the change under {CHANGE_MAX} characters")
    return text


def change_line(text: str) -> str:
    """The owner's change, followed by the picture terms its words name."""
    terms = [term for pattern, term in CHANGE_TERMS if re.search(pattern, text, re.IGNORECASE)]
    return text + (f" ({'; '.join(terms)})" if terms else "")


# ------------------------------------------------------------------------------------- the brief

@dataclass
class Brief:
    """What one shot's first frame shows, read from the episode (no prompt wording yet)."""
    shot_id: str
    scene: str
    beat: str = ""
    action: str = ""
    time_of_day: str = ""
    room: str = ""
    characters: list[dict] = field(default_factory=list)
    place: dict | None = None
    props: list[dict] = field(default_factory=list)
    refs: list[dict] = field(default_factory=list)
    aspect_ratio: str = "9:16"
    no_text: bool = False
    style: str = ""
    logline: str = ""
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def sha(self) -> str:
        """What the options were made from: an option picked later must match the shot as it is."""
        data = {"scene": self.scene, "beat": self.beat, "tod": self.time_of_day, "room": self.room,
                "characters": self.characters, "place": self.place, "props": self.props,
                "refs": [r["id"] for r in self.refs], "aspect_ratio": self.aspect_ratio, "no_text": self.no_text,
                "style": self.style, "logline": self.logline}
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def shots_path(episode_id: str) -> Path:
    from hfvg.shot_data import shots_json_path
    return shots_json_path(episode_id)


def _read_shots(episode_id: str) -> dict:
    path = shots_path(episode_id)
    if not path.is_file():
        raise CastNotFound("This episode has no shot list yet")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise CastError(f"The shot list cannot be read: {e}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("shots"), list):
        raise CastError("The shot list cannot be read: it has no shots")
    return raw


def _beats(episode_id: str) -> dict[str, dict]:
    from hfvg.episode_parser import parse_beatmap
    path = paths.episode_dir(episode_id) / "BEATMAP.md"
    if not path.is_file():
        return {}
    try:
        return {s["shot_id"]: s for s in parse_beatmap(path)}
    except (OSError, ValueError) as e:
        logger.info("first frames %s: the beatmap is not read (%s)", episode_id, e)
        return {}


def _universe_logline(library: tuple[str, str]) -> str:
    if library[0] != "series":
        return ""
    from hfvg import world_store
    try:
        return _clip(_one_line(world_store.get_profile(library[1]).get("logline")), LOGLINE_MAX)
    except CastError:
        return ""


def episode_context(episode_id: str) -> dict:
    """What every shot's brief reads once: the shot list, the beatmap, the style line, the logline."""
    from hfvg.cast_sheets import episode_style
    from hfvg.shot_data import ShotDataError, episode_profile, parse_shots
    raw = _read_shots(episode_id)
    try:
        plans = parse_shots(raw, episode_profile(episode_id))
    except (ShotDataError, ValueError) as e:
        raise CastError(f"The shot list cannot be read: {e}") from None
    library = cast_store.library_for_episode(episode_id)
    return {"episode_id": episode_id, "raw": raw, "items": {i["id"]: i for i in raw["shots"]}, "plans": plans,
            "beats": _beats(episode_id), "style": episode_style(episode_id), "logline": _universe_logline(library),
            "library": library}


def _explicit_refs(ctx: dict, sid: str, keys: list[str]) -> tuple[list[dict], list[str]]:
    """The shot's own refs.json pictures stored here, and notes on the ones left out."""
    from hfvg.shot_data import ShotDataError, pre_approved_still_paths, refs_json_path, resolve_refs
    if not keys:
        return [], []
    if not refs_json_path(ctx["episode_id"]).is_file():
        return [], ["This shot's own reference files are not uploaded yet, so they are left out"]
    try:
        resolved = resolve_refs(ctx["episode_id"], {sid: keys},
                                pre_approved_still_paths(ctx["episode_id"], ctx["plans"]))[sid]
    except ShotDataError as e:
        return [], [f"This shot's own reference files cannot be read ({e}), so they are left out"]
    out, notes = [], []
    for value in resolved["refs"]:
        if value.startswith(("http://", "https://")):
            notes.append("A reference picture stored online is left out; first frames use pictures stored here")
            continue
        path = Path(value)
        out.append({"id": f"file:{_file_sha(path)}", "path": str(path), "label": "a reference picture for this shot"})
    if resolved["missing_refs"]:
        notes.append(f"{len(resolved['missing_refs'])} of this shot's reference pictures are missing")
    return out, notes


def _lock_problems(ctx: dict, sid: str) -> list[str]:
    """A Universe episode's characters keep one locked look: first frames wait for it."""
    if ctx["library"][0] != "series":
        return []
    from hfvg import world_store
    unlocked = world_store.unlocked_cast(ctx["episode_id"], {"shots": [ctx["items"][sid]]})
    return [world_store.lock_message(unlocked, "frame")] if unlocked else []


def shot_brief(ctx: dict, sid: str, *, live: bool, materialize: bool = False) -> Brief:
    """One shot's brief. ``materialize``: build the composite reference sheets (only when making)."""
    from hfvg.cast_plan import plan_cast, resolve_cast_key, split_keys
    from hfvg.cast_prompt import no_text_required
    if sid not in ctx["plans"]:
        raise CastNotFound(f"Shot {sid} is not in this episode's shot list")
    plan = copy.deepcopy(ctx["plans"][sid])
    explicit_keys, _ = split_keys(list(plan.get("ref_keys") or []))
    plan.update({"ref_keys": explicit_keys, "refs": [], "missing_refs": []})
    explicit, notes = _explicit_refs(ctx, sid, explicit_keys)
    report = plan_cast(ctx["episode_id"], {sid: plan}, live=live, materialize=materialize)
    problems = [e.split(": ", 1)[1] if e.startswith(f"shot {sid}: ") else e for e in report["errors"]]
    row = report["shots"].get(sid) or {}
    notes += list(row.get("notes") or [])
    refs = list(explicit)
    missing = 0
    for ref in row.get("refs") or []:
        path = resolve_cast_key(ref["key"])
        if path is None or (materialize and not path.is_file()):
            missing += 1
            continue
        refs.append({"id": ref["key"], "path": str(path), "label": ref["label"]})
    if missing:
        notes.append(f"{missing} reference picture(s) from the cast and props are missing")
    meta = plan["shot_meta"]
    things = meta.get("props") or []
    place = next(({"name": t["name"], "clause": t["clause"]} for t in things if t.get("kind") == "place"), None)
    beat = ctx["beats"].get(sid) or {}
    action, purpose = _one_line(beat.get("action")), _one_line(beat.get("purpose"))
    tod, room = _one_line(beat.get("tod")), _one_line(beat.get("room"))
    return Brief(
        shot_id=sid, scene=_one_line(plan["prompt"]),
        beat=_clip(f"{action} ({purpose})" if action and purpose else action or purpose, BEAT_MAX), action=action,
        time_of_day="" if tod in ("-", "–") else tod, room="" if room in ("-", "–") else room,
        characters=[{"name": c["name"], "clause": c["clause"]} for c in meta.get("cast") or []],
        place=place, props=[{"name": t["name"], "clause": t["clause"]} for t in things if t.get("kind") == "prop"],
        refs=refs, aspect_ratio=str(plan["params"]["aspect_ratio"]), no_text=no_text_required(meta),
        style=ctx["style"], logline=ctx["logline"], notes=notes, problems=problems + _lock_problems(ctx, sid))


# ------------------------------------------------------------------------------------- prompt adapters
# Each still model reads a prompt its own way. The adapter for the server's still model lays the
# same brief out for it; a model with no adapter of its own gets the plain one.

def _orientation(aspect_ratio: str) -> str:
    try:
        w, h = (float(x) for x in aspect_ratio.split(":"))
    except ValueError:
        return f"aspect ratio {aspect_ratio}"
    shape = "tall, portrait" if h > w else "wide, landscape" if w > h else "square"
    return f"{shape}, aspect ratio {aspect_ratio}"


def _ref_list(brief: Brief) -> str:
    return "; ".join(f"image {k} is {r['label']}" for k, r in enumerate(brief.refs, 1))


def _time_of_day(brief: Brief, changes: list[str]) -> str:
    """The beatmap's time of day, unless a change names another one."""
    if not brief.time_of_day or any(TIME_WORDS.search(c) for c in changes):
        return ""
    return brief.time_of_day


def _variant(position: int) -> str:
    return VARIANTS[position % len(VARIANTS)]


def _write_story(brief: Brief, changes: list[str], position: int) -> list[str]:
    """Plain sentences, the moment first (models that read a prompt as a description)."""
    parts = [f"A cinematic film still: the first frame of a shot, {_orientation(brief.aspect_ratio)}.",
             f"The moment: {brief.beat.rstrip('.')}." if brief.beat else "",
             f"What we see: {brief.scene}"]
    if brief.characters:
        parts.append("In frame, each exactly as described: " + " ".join(c["clause"] for c in brief.characters))
    if brief.place:
        parts.append("Where: " + brief.place["clause"])
    elif brief.room:
        parts.append(f"Where: {brief.room}.")
    if brief.props:
        parts.append("Also in the shot: " + " ".join(p["clause"] for p in brief.props))
    tod = _time_of_day(brief, changes)
    if tod:
        parts.append(f"Time of day: {tod}.")
    if brief.style:
        parts.append(f"Visual style: {brief.style.rstrip('.')}.")
    if brief.logline:
        parts.append(f"The story this belongs to: {brief.logline.rstrip('.')}.")
    if brief.refs:
        parts.append(f"Reference images: {_ref_list(brief)}. Keep every face, hair, outfit, place and object exactly "
                     "as in them.")
    if changes:
        parts.append("Most important, change this from the last try: " + "; ".join(change_line(c) for c in changes) + ".")
    parts += [_variant(position), _no_text(brief), "One single picture, not a grid or a collage."]
    return parts


def _write_sections(brief: Brief, changes: list[str], position: int) -> list[str]:
    """Labelled sections (models that follow structured instructions closely)."""
    parts = ["Create one photoreal film still: the opening frame of a shot.",
             f"Scene: {brief.scene}",
             f"Story moment: {brief.beat.rstrip('.')}." if brief.beat else ""]
    if brief.characters:
        parts.append("Characters (match exactly):\n" + "\n".join(f"- {c['clause']}" for c in brief.characters))
    if brief.place or brief.room:
        parts.append("Setting: " + (brief.place["clause"] if brief.place else f"{brief.room}."))
    if brief.props:
        parts.append("Props:\n" + "\n".join(f"- {p['clause']}" for p in brief.props))
    tod = _time_of_day(brief, changes)
    if tod:
        parts.append(f"Time of day: {tod}")
    if brief.style:
        parts.append(f"Style: {brief.style.rstrip('.')}.")
    if brief.refs:
        parts.append("Reference images (keep identity, outfits and objects identical):\n"
                     + "\n".join(f"- Image {k}: {r['label']}" for k, r in enumerate(brief.refs, 1)))
    if changes:
        parts.append("Changes requested (these win over anything above):\n"
                     + "\n".join(f"- {change_line(c)}" for c in changes))
    parts.append(f"Composition: {_variant(position) or 'as the scene describes'} Format: {_orientation(brief.aspect_ratio)}.")
    parts.append("Constraints: one single image, not a grid or a collage." + (f" {_no_text(brief)}" if brief.no_text else ""))
    return parts


def _write_edit(brief: Brief, changes: list[str], position: int) -> list[str]:
    """Reference-led instructions (edit models that start from the reference images)."""
    who = ", ".join(c["name"] for c in brief.characters)
    where = brief.place["name"] if brief.place else brief.room
    lead = "Using the reference images" + (f" ({_ref_list(brief)})" if brief.refs else "")
    show = (f"Show {who}" + (f" at {where}." if where else ".")) if who else (f"Set at {where}." if where else "")
    parts = [f"{lead}, make one film still that opens a shot.", brief.scene, show,
             "Keep every face, outfit and object identical to the references." if brief.refs else ""]
    if changes:
        parts.append("Change: " + "; ".join(change_line(c) for c in changes) + ".")
    if brief.characters:
        parts.append(" ".join(c["clause"] for c in brief.characters))
    if brief.place:
        parts.append(brief.place["clause"])
    if brief.props:
        parts.append(" ".join(p["clause"] for p in brief.props))
    tod = _time_of_day(brief, changes)
    if tod:
        parts.append(f"Time of day: {tod}.")
    parts += [_variant(position), f"Format: {_orientation(brief.aspect_ratio)}.", _no_text(brief)]
    return parts


def _write_tags(brief: Brief, changes: list[str], position: int) -> list[str]:
    """Short comma-separated phrases, the most important first (models with a short prompt and no
    reference images): what is clipped to fit comes off the end."""
    tags = ["cinematic film still"] + [change_line(c) for c in changes] + [brief.scene.rstrip(".")]
    tags += [c["clause"].rstrip(".") for c in brief.characters]
    if brief.place:
        tags.append(brief.place["clause"].rstrip("."))
    elif brief.room:
        tags.append(brief.room)
    tags += [p["clause"].rstrip(".") for p in brief.props]
    tod = _time_of_day(brief, changes)
    if tod:
        tags.append(tod)
    if brief.style:
        tags.append(brief.style.rstrip("."))
    if _variant(position):
        tags.append(_variant(position).rstrip("."))
    tags.append(_orientation(brief.aspect_ratio))
    if brief.no_text:
        tags.append("no text")
    return [", ".join(t for t in tags if t)]


def _no_text(brief: Brief) -> str:
    from hfvg.cast_prompt import NO_TEXT_CLAUSE
    return NO_TEXT_CLAUSE if brief.no_text else ""


@dataclass(frozen=True)
class PromptAdapter:
    key: str
    models: tuple[str, ...]
    max_chars: int
    write: Callable[[Brief, list[str], int], list[str]]
    joiner: str = "\n"
    clip_part: int = 0


ADAPTERS = (
    PromptAdapter("story", ("xai/grok-imagine",), 2000, _write_story, clip_part=2),
    PromptAdapter("sections", ("openai/gpt-image", "gpt-image", "gpt_image"), 4000, _write_sections, clip_part=1),
    PromptAdapter("edit", ("alibaba/qwen-image",), 1200, _write_edit, " ", clip_part=1),
    PromptAdapter("tags", ("higgsfield-ai/soul",), 600, _write_tags, clip_part=0),
)
PLAIN = PromptAdapter("plain", (), 1500, _write_story, clip_part=2)


def _canonical(model: str) -> str:
    from hfvg import model_caps
    try:
        registry = model_caps.get_registry()
    except Exception:  # noqa: BLE001 - an unreadable registry is refused when the model is checked
        return model
    if model in registry.models:
        return model
    return next((mid for mid, caps in registry.models.items() if model in caps.aliases), model)


def adapter_for(model: str) -> PromptAdapter:
    """The prompt adapter of a still model (by its id or an alias); the plain one when none fits."""
    canonical = _canonical(model)
    for adapter in ADAPTERS:
        if any(canonical.startswith(prefix) or model.startswith(prefix) for prefix in adapter.models):
            return adapter
    return PLAIN


def write_prompt(brief: Brief, changes: list[str], position: int, adapter: PromptAdapter, limit: int) -> str:
    """One option's prompt within ``limit`` characters: the scene words are clipped first, so the
    characters, references and the owner's changes always reach the model."""
    parts = adapter.write(brief, changes, position)
    text = adapter.joiner.join(p for p in parts if p)
    if len(text) <= limit:
        return text
    over = len(text) - limit
    target = parts[adapter.clip_part]
    parts[adapter.clip_part] = _clip(target, max(len(target) - over, 40))
    text = adapter.joiner.join(p for p in parts if p)
    return text if len(text) <= limit else _clip(text, limit)


def _prompt_limit(caps: Any, adapter: PromptAdapter) -> int:
    try:
        known = caps.limit("prompt_max_chars").value
    except (AttributeError, KeyError):
        known = None
    return min(adapter.max_chars, known) if isinstance(known, int) and known > 0 else adapter.max_chars


def check_model(model: str, refs_needed: int, aspect_ratio: str) -> Any:
    """The still model's caps when the server routes it and it takes the frame's references,
    resolution, quality and aspect ratio."""
    from hfvg import model_caps
    from hfvg.cast_sheets import ROUTED_STATUSES
    from hfvg.config import max_refs_for
    from hfvg.defaults import STILL_QUALITY, STILL_RESOLUTION, check_value
    try:
        caps = model_caps.get_registry().resolve(model)
    except model_caps.UnknownModelError as e:
        raise FramePlanError(f"The still model is not in the model caps registry: {e}") from None
    if caps.status not in ROUTED_STATUSES:
        raise FramePlanError(f"The studio's still model cannot make first frames now (status {caps.status})")
    if refs_needed and max_refs_for(model) < refs_needed:
        raise FramePlanError(f"The still model takes {max_refs_for(model)} reference pictures; this frame needs "
                             f"{refs_needed}")
    for name, value in (("resolution", STILL_RESOLUTION), ("quality", STILL_QUALITY), ("aspect_ratio", aspect_ratio)):
        why = check_value(caps, name, value)
        if why:
            raise FramePlanError(f"First frames cannot use {name} {value!r} with the still model: {why}")
    return caps


def still_model() -> str:
    from hfvg.config import still_model_path
    return still_model_path()


# ------------------------------------------------------------------------------------- planning a round

def _set_rows(conn, episode_id: str, shot_id: str | None = None) -> list:
    if shot_id is None:
        return conn.execute("SELECT * FROM concept_frame_sets WHERE episode_id = ? ORDER BY shot_id, round",
                            (episode_id,)).fetchall()
    return conn.execute("SELECT * FROM concept_frame_sets WHERE episode_id = ? AND shot_id = ? ORDER BY round",
                        (episode_id, shot_id)).fetchall()


def plan_round(episode_id: str, shot_id: str, *, change: Any = None, keep_changes: bool = True, live: bool,
               model: str | None = None) -> dict:
    """The next round of options for one shot: its brief, the owner's changes in force and each
    option's prompt for the server's still model. Refused while the shot cannot have first frames
    (its cast cannot be planned, a Universe look is not locked, a round is being made)."""
    from hfvg.cast_sheets import prompt_problems
    from hfvg.config import max_refs_for
    if not SHOT_ID_RE.match(shot_id or ""):
        raise CastNotFound(f"Shot {shot_id} is not in this episode's shot list")
    new_change = clean_change(change)
    ctx = episode_context(episode_id)
    brief = shot_brief(ctx, shot_id, live=live, materialize=True)
    if brief.problems:
        raise FramePlanError(brief.problems[0])
    with connect() as conn:
        rows = _set_rows(conn, episode_id, shot_id)
    latest = rows[-1] if rows else None
    if latest is not None and latest["status"] == "making":
        raise CastConflict(BEING_MADE)
    if len(rows) >= ROUNDS_MAX:
        raise CastConflict(TOO_MANY_ROUNDS)
    changes = json.loads(latest["changes"]) if latest is not None and keep_changes else []
    if new_change:
        changes = (changes + [new_change])[-CHANGES_KEPT:]
    brief_sha = brief.sha()
    model = model or still_model()
    adapter = adapter_for(model)
    _, notes = _photo_notes(brief.notes)  # the shot names who has no photo yet once (no_photos)
    cap = max_refs_for(model)
    if len(brief.refs) > cap:
        notes.append(f"{len(brief.refs) - cap} reference picture(s) did not fit the picture model's limit")
        brief.refs = brief.refs[:cap]
    caps = check_model(model, len(brief.refs), brief.aspect_ratio)
    limit = _prompt_limit(caps, adapter)
    count = options_per_round()
    prompts = [write_prompt(brief, changes, position, adapter, limit) for position in range(count)]
    problems = sorted({p for prompt in prompts for p in prompt_problems(prompt)})
    if problems:
        raise CastError("This shot's words cannot be used for a picture (" + ", ".join(problems)
                        + "); change the shot's words or the change you asked for")
    return {"episode_id": episode_id, "shot_id": shot_id, "round": (rows[-1]["round"] + 1) if rows else 1,
            "changes": changes, "brief_sha": brief_sha, "adapter": adapter.key, "model_path": model,
            "aspect_ratio": brief.aspect_ratio, "count": count, "prompts": prompts,
            "refs": [{"path": r["path"], "sha256": _file_sha(Path(r["path"]))} for r in brief.refs],
            "summary": _summary(brief), "notes": notes}


def _summary(brief: Brief) -> dict:
    return {"characters": [c["name"] for c in brief.characters], "place": brief.place["name"] if brief.place else None,
            "props": [p["name"] for p in brief.props], "references": len(brief.refs)}


def new_set_id() -> str:
    return f"cf_{secrets.token_hex(6)}"


def option_id_for(set_id: str, position: int) -> str:
    """``cframe-<set hex>-<position>``: also the paid job id (a set id is never reused)."""
    return f"{JOB_PREFIX}{set_id.split('_', 1)[-1]}-{position}"


def plan_jobs(plan: dict, *, set_id: str) -> list[dict]:
    """The paid still jobs of a live round, one per option (the worker's guarded path)."""
    from hfvg.defaults import STILL_QUALITY, STILL_RESOLUTION
    return [{"job_id": option_id_for(set_id, position), "set_id": set_id, "episode_id": plan["episode_id"],
             "shot_id": plan["shot_id"], "position": position, "prompt": plan["prompts"][position],
             "refs": [r["path"] for r in plan["refs"]], "ref_shas": [r["sha256"] for r in plan["refs"]],
             "model_path": plan["model_path"], "resolution": STILL_RESOLUTION, "quality": STILL_QUALITY,
             "aspect_ratio": plan["aspect_ratio"]} for position in range(plan["count"])]


# ------------------------------------------------------------------------------------- placeholders

def _frame_size(aspect_ratio: str) -> tuple[int, int]:
    try:
        w, h = (float(x) for x in aspect_ratio.split(":"))
    except ValueError:
        w, h = 9.0, 16.0
    if w <= 0 or h <= 0:
        w, h = 9.0, 16.0
    long_side = PLACEHOLDER_LONG_SIDE
    return (long_side, max(256, round(long_side * h / w))) if w >= h else (max(256, round(long_side * w / h)), long_side)


def placeholder_frame(seed: str, ref_paths: list[str], position: int, aspect_ratio: str) -> bytes:
    """A deterministic dry-run stand-in for one option (no provider call): a sky, a ground and one
    of the shot's reference pictures framed in the middle (when it has any). No text."""
    from PIL import Image, ImageDraw, ImageOps
    digest = hashlib.sha256(f"{seed}:{position}".encode()).digest()
    width, height = _frame_size(aspect_ratio)
    top = (120 + digest[0] % 80, 170 + digest[1] % 60, 220 + digest[2] % 35)
    bottom = (40 + digest[3] % 120, 70 + digest[4] % 100, 60 + digest[5] % 90)
    img = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(img)
    horizon = height * (55 + digest[6] % 20) // 100
    for y in range(horizon):
        t = y / max(horizon, 1)
        draw.line((0, y, width, y), fill=tuple(round(top[k] * (1 - t * 0.35)) for k in range(3)))
    draw.rectangle((0, horizon, width, height), fill=bottom)
    sun = min(width, height) // (5 + digest[7] % 4)
    cx = width * (20 + digest[8] % 60) // 100
    cy = horizon * (25 + digest[9] % 40) // 100
    draw.ellipse((cx - sun // 2, cy - sun // 2, cx + sun // 2, cy + sun // 2), fill=(255, 236 - digest[10] % 40, 180))
    if ref_paths:
        try:
            with Image.open(ref_paths[position % len(ref_paths)]) as src:
                side = min(width, height) * (45 + 10 * (position % 3)) // 100
                photo = ImageOps.fit(ImageOps.exif_transpose(src).convert("RGB"), (side, side))
            img.paste(photo, ((width - side) // 2, horizon - side * 2 // 3))
        except (OSError, ValueError):
            pass
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------------------------- rounds and options

def _option_view(row, episode_id: str) -> dict:
    made = row["status"] == "completed" and bool(row["sha256"])
    base = f"/api/episodes/{episode_id}/concept-frames/images/{row['option_id']}"
    return {"option_id": row["option_id"], "position": row["position"], "status": row["status"],
            "placeholder": bool(row["placeholder"]), "width": row["width"], "height": row["height"],
            "error": row["error"], "image_url": base if made else None, "thumb_url": f"{base}/thumb" if made else None}


def _set_view(conn, row) -> dict:
    options = conn.execute("SELECT * FROM concept_frame_options WHERE set_id = ? ORDER BY position",
                           (row["set_id"],)).fetchall()
    return {"set_id": row["set_id"], "shot_id": row["shot_id"], "round": row["round"], "status": row["status"],
            "changes": json.loads(row["changes"]), "option_count": row["option_count"], "dry_run": bool(row["dry_run"]),
            "estimate_usd_micros": row["estimate_usd_micros"], "error": row["error"],
            "requested_by": row["requested_by"], "created_at": row["created_at"], "finished_at": row["finished_at"],
            "made": sum(1 for o in options if o["status"] == "completed" and o["sha256"]),
            "brief_sha": row["brief_sha"], "options": [_option_view(o, row["episode_id"]) for o in options]}


def _insert_set(conn, plan: dict, *, set_id: str, status: str, dry_run: bool, estimate_usd_micros: int,
                approved_total_usd_micros: int, workflow_id: str | None, requested_by: str | None, now: str) -> None:
    rows = _set_rows(conn, plan["episode_id"], plan["shot_id"])
    if rows and rows[-1]["status"] == "making":
        raise CastConflict(BEING_MADE)
    if ((rows[-1]["round"] + 1) if rows else 1) != plan["round"]:
        raise CastConflict("First frames for this shot changed meanwhile; look at them again")
    conn.execute(
        "INSERT INTO concept_frame_sets (set_id, episode_id, shot_id, round, status, changes, prompt, brief_sha, "
        "adapter, model_path, aspect_ratio, option_count, dry_run, estimate_usd_micros, approved_total_usd_micros, "
        "workflow_id, requested_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (set_id, plan["episode_id"], plan["shot_id"], plan["round"], status, json.dumps(plan["changes"]),
         plan["prompts"][0], plan["brief_sha"], plan["adapter"], plan["model_path"], plan["aspect_ratio"],
         plan["count"], int(dry_run), int(estimate_usd_micros), int(approved_total_usd_micros), workflow_id,
         requested_by, now))


def make_dry_set(plan: dict, *, estimate_usd_micros: int, approved_total_usd_micros: int,
                 requested_by: str | None) -> dict:
    """A dry-run round, made here: one placeholder picture per option, free."""
    set_id = new_set_id()
    refs = [r["path"] for r in plan["refs"]]
    seed = f"{plan['episode_id']}:{plan['shot_id']}:{plan['round']}:{'|'.join(plan['changes'])}"
    pictures = [placeholder_frame(seed, refs, position, plan["aspect_ratio"]) for position in range(plan["count"])]
    infos = [cast_store.inspect_image(data, max_bytes=PICTURE_MAX_BYTES, min_px=128) for data in pictures]
    now = _now()
    with connect() as conn:
        with _tx(conn):
            _insert_set(conn, plan, set_id=set_id, status="ready", dry_run=True, estimate_usd_micros=estimate_usd_micros,
                        approved_total_usd_micros=approved_total_usd_micros, workflow_id=None,
                        requested_by=requested_by, now=now)
            conn.execute("UPDATE concept_frame_sets SET finished_at = ? WHERE set_id = ?", (now, set_id))
            for position, (data, info) in enumerate(zip(pictures, infos)):
                cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
                conn.execute(
                    "INSERT INTO concept_frame_options (option_id, set_id, position, prompt, status, "
                    "estimated_usd_micros, approved_usd_micros, sha256, ext, mime, width, height, bytes, placeholder, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, 'completed', 0, 0, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (option_id_for(set_id, position), set_id, position, plan["prompts"][position], info["sha256"],
                     info["ext"], info["mime"], info["width"], info["height"], info["bytes"], now, now))
            _audit_round(conn, plan, set_id, requested_by, estimate_usd_micros, approved_total_usd_micros, True)
        for info in infos:
            cast_store.ensure_thumb(info["sha256"], info["ext"])
        return _set_view(conn, _set_row(conn, set_id))


def _audit_round(conn, plan: dict, set_id: str, actor: str | None, estimate: int, approved: int, dry_run: bool,
                 workflow_id: str | None = None) -> None:
    audit(conn, actor, "concept_frames_request", episode_id=plan["episode_id"],
          details={"set_id": set_id, "shot_id": plan["shot_id"], "round": plan["round"], "options": plan["count"],
                   "changes": plan["changes"], "estimate_usd_micros": int(estimate),
                   "approved_total_usd_micros": int(approved), "dry_run": dry_run, "workflow_id": workflow_id,
                   "adapter": plan["adapter"], "model_path": plan["model_path"]})


def start_set(plan: dict, jobs: list[dict], *, set_id: str, workflow_id: str, estimate_usd_micros: int,
              approved_total_usd_micros: int, requested_by: str | None) -> dict:
    """Record a live round the owner approved the price of, with its options queued, before the
    worker is asked to make them."""
    now = _now()
    with connect() as conn:
        with _tx(conn):
            _insert_set(conn, plan, set_id=set_id, status="making", dry_run=False,
                        estimate_usd_micros=estimate_usd_micros, approved_total_usd_micros=approved_total_usd_micros,
                        workflow_id=workflow_id, requested_by=requested_by, now=now)
            for job in jobs:
                conn.execute(
                    "INSERT INTO concept_frame_options (option_id, set_id, position, prompt, status, "
                    "estimated_usd_micros, approved_usd_micros, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                    (job["job_id"], set_id, int(job["position"]), job["prompt"], int(job["estimated_usd_micros"]),
                     int(job["approved_usd_micros"]), now, now))
            _audit_round(conn, plan, set_id, requested_by, estimate_usd_micros, approved_total_usd_micros, False,
                         workflow_id)
        return _set_view(conn, _set_row(conn, set_id))


def _set_row(conn, set_id: str):
    row = conn.execute("SELECT * FROM concept_frame_sets WHERE set_id = ?", (set_id,)).fetchone()
    if row is None:
        raise CastNotFound(f"First frames {set_id} not found")
    return row


def get_set(set_id: str) -> dict:
    with connect() as conn:
        return _set_view(conn, _set_row(conn, set_id))


def option(option_id: str) -> dict | None:
    """An option with its round's episode, shot, status (set_status) and brief."""
    with connect() as conn:
        row = conn.execute("SELECT o.*, s.episode_id, s.shot_id, s.status AS set_status, s.brief_sha, s.round "
                           "FROM concept_frame_options o JOIN concept_frame_sets s ON s.set_id = o.set_id "
                           "WHERE o.option_id = ?", (option_id,)).fetchone()
    return dict(row) if row else None


def set_option(option_id: str, status: str, error: str | None = None) -> None:
    if status not in OPTION_STATUSES:
        raise CastError(f"Unknown first frame status {status!r}")
    with connect() as conn:
        conn.execute("UPDATE concept_frame_options SET status = ?, error = ?, updated_at = ? WHERE option_id = ?",
                     (status, (error or "")[:1000] or None, _now(), option_id))


def store_option_picture(option_id: str, data: bytes, *, placeholder: bool) -> dict:
    """Keep the picture an option's job made (the private image store) and complete it.
    Idempotent: an option that has its picture keeps the first one."""
    info = cast_store.inspect_image(data, max_bytes=PICTURE_MAX_BYTES, min_px=128)
    with connect() as conn:
        with _tx(conn):
            row = conn.execute("SELECT * FROM concept_frame_options WHERE option_id = ?", (option_id,)).fetchone()
            if row is None:
                raise CastNotFound(f"First frame option {option_id} not found")
            if row["sha256"] is None:
                cast_store._write_once(cast_store.image_file(info["sha256"], info["ext"]), data)
                conn.execute(
                    "UPDATE concept_frame_options SET status = 'completed', sha256 = ?, ext = ?, mime = ?, width = ?, "
                    "height = ?, bytes = ?, placeholder = ?, error = NULL, updated_at = ? WHERE option_id = ?",
                    (info["sha256"], info["ext"], info["mime"], info["width"], info["height"], info["bytes"],
                     int(placeholder), _now(), option_id))
            else:
                conn.execute("UPDATE concept_frame_options SET status = 'completed', error = NULL, updated_at = ? "
                             "WHERE option_id = ?", (_now(), option_id))
            sha, ext = conn.execute("SELECT sha256, ext FROM concept_frame_options WHERE option_id = ?",
                                    (option_id,)).fetchone()
    cast_store.ensure_thumb(sha, ext)
    return option(option_id)


def finish_set(set_id: str, error: str | None = None, actor: str | None = "worker") -> dict:
    """End a live round: ``ready`` with every option made, ``partial`` with some (and the reason it
    stopped), ``failed`` with none. Options that did not run are ``skipped``. Idempotent."""
    now = _now()
    with connect() as conn:
        with _tx(conn):
            row = _set_row(conn, set_id)
            if row["status"] == "making":
                options = conn.execute("SELECT status, sha256 FROM concept_frame_options WHERE set_id = ?",
                                       (set_id,)).fetchall()
                made = sum(1 for o in options if o["status"] == "completed" and o["sha256"])
                if error is None and made < row["option_count"]:
                    error = STOPPED
                if error is not None:
                    conn.execute("UPDATE concept_frame_options SET status = 'skipped', error = ?, updated_at = ? "
                                 "WHERE set_id = ? AND status IN ('queued', 'running')",
                                 (f"Stopped: {error}"[:1000], now, set_id))
                status = "ready" if error is None else "partial" if made else "failed"
                conn.execute("UPDATE concept_frame_sets SET status = ?, error = ?, finished_at = ? WHERE set_id = ?",
                             (status, error[:1000] if error else None, now, set_id))
                audit(conn, actor, "concept_frames_finished", episode_id=row["episode_id"],
                      details={"set_id": set_id, "shot_id": row["shot_id"], "status": status, "made": made,
                               "error": (error or "")[:300] or None})
        return _set_view(conn, _set_row(conn, set_id))


def making_sets(episode_id: str) -> list[dict]:
    """The live rounds of an episode being made (set id, workflow id, created at)."""
    with connect() as conn:
        rows = conn.execute("SELECT set_id, workflow_id, created_at FROM concept_frame_sets "
                            "WHERE episode_id = ? AND status = 'making'", (episode_id,)).fetchall()
    return [dict(r) for r in rows]


def option_picture(episode_id: str, option_id: str) -> dict:
    """The stored picture of an option of this episode: {"path", "mime", "sha256", "ext"}."""
    row = option(option_id) if OPTION_ID_RE.match(option_id or "") else None
    if row is None or row["episode_id"] != episode_id or not row["sha256"]:
        raise CastNotFound(f"Picture {option_id} not found")
    return {"path": cast_store.image_file(row["sha256"], row["ext"]), "mime": row["mime"], "sha256": row["sha256"],
            "ext": row["ext"]}


# ------------------------------------------------------------------------------------- the pick

def _spec_path(spec: Any) -> str | None:
    if isinstance(spec, str):
        return spec.strip() or None
    if isinstance(spec, dict) and isinstance(spec.get("path"), str):
        return spec["path"].strip() or None
    return None


def picked_here(spec: Any) -> bool:
    """Whether a shot's pre_approved_still is a first frame picked here (one this module may replace)."""
    path = _spec_path(spec)
    return bool(path) and path.startswith(f"{FRAMES_DIR}/") and "/" not in path[len(FRAMES_DIR) + 1:]


def _write_shots(episode_id: str, raw: dict) -> None:
    from hfvg.shot_data import ShotDataError, episode_profile, parse_shots
    try:
        parse_shots(raw, episode_profile(episode_id))
    except (ShotDataError, ValueError) as e:
        raise CastError(f"The shot list would not be valid: {e}") from None
    path = shots_path(episode_id)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _copy_into_episode(episode_id: str, row: dict) -> str:
    """The picked picture as a file of the episode (``concept_frames/<shot>-<sha12>.<ext>``, written
    once); its path relative to shots.json."""
    src = cast_store.image_file(row["sha256"], row["ext"])
    if not src.is_file():
        raise CastConflict("This option's picture is missing; make new options")
    rel = f"{FRAMES_DIR}/{row['shot_id']}-{row['sha256'][:12]}.{row['ext']}"
    dest = shots_path(episode_id).parent / rel
    if not (dest.is_file() and _file_sha(dest) == row["sha256"]):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(src.read_bytes())
        tmp.replace(dest)
    if _file_sha(dest) != row["sha256"]:
        raise CastError("The picked picture could not be copied into the episode")
    return rel


def pick(episode_id: str, shot_id: str, option_id: str, *, actor: str | None, approved_by: str, live: bool) -> dict:
    """Make an option the shot's start frame: copied into the episode and written as the shot's
    ``pre_approved_still``. The caller holds the shot list lock and checked no run is going.
    Refused for an option made from an earlier version of the shot, a dry-run stand-in when the
    studio runs live, and a start frame set in the shot list by hand."""
    row = option(option_id) if OPTION_ID_RE.match(option_id or "") else None
    if row is None or row["episode_id"] != episode_id or row["shot_id"] != shot_id:
        raise CastNotFound(f"Option {option_id} is not one of this shot's first frames")
    if row["status"] != "completed" or not row["sha256"]:
        raise CastConflict("This option was not made; pick one that was")
    if live and row["placeholder"]:
        raise CastConflict("This option is a dry-run stand-in; make new options to pick a real picture")
    ctx = episode_context(episode_id)
    if shot_brief(ctx, shot_id, live=live).sha() != row["brief_sha"]:
        raise CastConflict(CHANGED)
    item = ctx["items"][shot_id]
    current = item.get("pre_approved_still")
    if current is not None and not picked_here(current):
        raise CastConflict(NOT_OURS)
    rel = _copy_into_episode(episode_id, row)
    item["pre_approved_still"] = {"path": rel, "sha256": row["sha256"], "approved_by": approved_by}
    _write_shots(episode_id, ctx["raw"])
    now = _now()
    with connect() as conn:
        with _tx(conn):
            conn.execute("UPDATE concept_frame_picks SET status = 'replaced', updated_at = ? WHERE episode_id = ? "
                         "AND shot_id = ? AND status = 'current'", (now, episode_id, shot_id))
            conn.execute(
                "INSERT INTO concept_frame_picks (pick_id, episode_id, shot_id, option_id, path, sha256, brief_sha, "
                "status, picked_by, picked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, ?)",
                (_new_id("fp"), episode_id, shot_id, option_id, rel, row["sha256"], row["brief_sha"], actor, now, now))
            audit(conn, actor, "concept_frame_pick", episode_id=episode_id,
                  details={"shot_id": shot_id, "option_id": option_id, "path": rel, "sha256": row["sha256"],
                           "replaced": _spec_path(current)})
    return shot_view(episode_id, shot_id, live=live)


def unpick(episode_id: str, shot_id: str, *, actor: str | None, live: bool) -> dict:
    """Take a first frame picked here off the shot (the shot's still is made by the run again).
    A start frame set in the shot list by hand is not touched."""
    ctx = episode_context(episode_id)
    if shot_id not in ctx["items"]:
        raise CastNotFound(f"Shot {shot_id} is not in this episode's shot list")
    item = ctx["items"][shot_id]
    current = item.get("pre_approved_still")
    if current is not None:
        if not picked_here(current):
            raise CastConflict(NOT_OURS)
        item.pop("pre_approved_still")
        _write_shots(episode_id, ctx["raw"])
    now = _now()
    with connect() as conn:
        with _tx(conn):
            conn.execute("UPDATE concept_frame_picks SET status = 'removed', updated_at = ? WHERE episode_id = ? "
                         "AND shot_id = ? AND status = 'current'", (now, episode_id, shot_id))
            audit(conn, actor, "concept_frame_unpick", episode_id=episode_id,
                  details={"shot_id": shot_id, "path": _spec_path(current)})
    return shot_view(episode_id, shot_id, live=live)


# ------------------------------------------------------------------------------------- the page's words
# The page describes each shot in plain words. The prompt a still model reads stays on the server.

def plain_moment(beat: str) -> str:
    """The beat without a camera label in front of it ("cinematic shot: A boat sails" -> "A boat sails")."""
    text = _one_line(beat)
    label = CAMERA_LABEL.match(text)
    if label and (not label.group(1)[:1].isupper() or CAMERA_WORDS & set(label.group(1).lower().split())):
        text = text[label.end():]
    return text[:1].upper() + text[1:]


def _names(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) > NAMES_SHOWN:
        names = names[:NAMES_SHOWN - 1] + [f"{len(names) - NAMES_SHOWN + 1} more"]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}" if names else ""


def _when(brief: Brief, changes: list[str]) -> str:
    for change in reversed(changes):
        named = TIME_WORDS.search(change)
        if named:
            return named.group(1).lower()
    return brief.time_of_day.lower()


def _looks(changes: list[str]) -> list[str]:
    chosen: dict[str, str] = {}
    for change in changes:
        for kind, pattern, words in CHANGE_LOOKS:
            if re.search(pattern, change, re.IGNORECASE):
                if words == "close-up":
                    on = CLOSE_ON.search(change)
                    words = f"close on {on.group(1).lower()}" if on else words
                chosen.pop(kind, None)
                chosen[kind] = words
    return list(chosen.values())


def describe(brief: Brief, changes: list[str], *, with_words: bool = True) -> str:
    """A shot's first frame in a few plain words ("Mara at Harbour cabin, dusk, close on her face"):
    who, where and what is in it, the time of day and what the owner's changes ask for. The beatmap's
    room, and the moment for a shot that names nobody, are only for whoever may read the shot's words."""
    room = brief.room if with_words and brief.room.lower() not in NO_ROOM else ""
    where = brief.place["name"] if brief.place else room
    head = " at ".join(x for x in (_names([c["name"] for c in brief.characters]), where) if x)
    things = _names([p["name"] for p in brief.props])
    if things:
        head = f"{head}, with {things}" if head else things
    if not head and with_words:
        head = _clip(plain_moment(brief.action or brief.beat), 80)
    text = ", ".join(x for x in (head, _when(brief, changes), *_looks(changes)) if x)
    return text[:1].upper() + text[1:]


def _photo_notes(notes: list[str]) -> tuple[list[str], list[str]]:
    """(the names with no reference photo yet, the other notes): the page names the first once, in
    its line about the shot's reference photos."""
    missing, other = [], []
    for note in notes:
        named = NO_PHOTO_NOTE.match(note)
        if named:
            if named.group(1) not in missing:
                missing.append(named.group(1))
        else:
            other.append(note)
    return missing, other


# ------------------------------------------------------------------------------------- the view

def _pick_view(spec: Any, pick_row, episode_id: str, sha: str) -> dict | None:
    if spec is None:
        return None
    if not picked_here(spec) or pick_row is None or pick_row["path"] != _spec_path(spec):
        return {"source": "shot_list", "option_id": None, "image_url": None, "thumb_url": None, "stale": False,
                "picked_by": None, "picked_at": None}
    base = f"/api/episodes/{episode_id}/concept-frames/images/{pick_row['option_id']}"
    return {"source": "here", "option_id": pick_row["option_id"], "image_url": base, "thumb_url": f"{base}/thumb",
            "stale": pick_row["brief_sha"] != sha, "picked_by": pick_row["picked_by"], "picked_at": pick_row["picked_at"]}


def _shot_view(ctx: dict, sid: str, sets: list, picks: dict, conn, *, live: bool, with_words: bool) -> dict:
    brief = shot_brief(ctx, sid, live=live)
    sha = brief.sha()
    rounds = []
    for row in reversed(sets):
        view = _set_view(conn, row)
        view["current"] = view.pop("brief_sha") == sha
        rounds.append(view)
    latest = rounds[0] if rounds else None
    changes = latest["changes"] if latest else []
    no_photos, notes = _photo_notes(brief.notes)
    return {"shot_id": sid, "description": describe(brief, changes, with_words=with_words),
            "beat": plain_moment(brief.beat) if with_words else None,
            "time_of_day": brief.time_of_day or None, **_summary(brief), "no_photos": no_photos,
            "aspect_ratio": brief.aspect_ratio, "notes": notes, "problems": brief.problems, "ready": not brief.problems,
            "changes": changes, "making": bool(latest and latest["status"] == "making"),
            "changed": bool(latest and not latest["current"]), "rounds": rounds,
            "pick": _pick_view(ctx["items"][sid].get("pre_approved_still"), picks.get(sid), ctx["episode_id"], sha)}


def frames_view(episode_id: str, *, live: bool, with_words: bool = True) -> dict:
    """Every shot with what its first frame shows in plain words, its rounds of options and its pick
    (never its prompt). ``with_words``: the beat and the beatmap's room (only for whoever may read the
    episode's prompts)."""
    base = {"episode_id": episode_id, "options_per_round": options_per_round(), "dry_run": not live,
            "words_hidden": not with_words}
    try:
        ctx = episode_context(episode_id)
    except CastNotFound:
        return {**base, "uploaded": False, "shots": [], "error": None}
    except CastError as e:
        return {**base, "uploaded": True, "shots": [], "error": str(e) if with_words else "The shot list cannot be read"}
    with connect() as conn:
        sets: dict[str, list] = {}
        for row in _set_rows(conn, episode_id):
            sets.setdefault(row["shot_id"], []).append(row)
        picks = {r["shot_id"]: r for r in conn.execute(
            "SELECT * FROM concept_frame_picks WHERE episode_id = ? AND status = 'current'", (episode_id,)).fetchall()}
        shots = [_shot_view(ctx, item["id"], sets.get(item["id"], []), picks, conn, live=live, with_words=with_words)
                 for item in ctx["raw"]["shots"]]
    return {**base, "uploaded": True, "shots": shots, "error": None}


def shot_view(episode_id: str, shot_id: str, *, live: bool, with_words: bool = True) -> dict:
    ctx = episode_context(episode_id)
    if shot_id not in ctx["items"]:
        raise CastNotFound(f"Shot {shot_id} is not in this episode's shot list")
    with connect() as conn:
        sets = _set_rows(conn, episode_id, shot_id)
        picks = {r["shot_id"]: r for r in conn.execute(
            "SELECT * FROM concept_frame_picks WHERE episode_id = ? AND shot_id = ? AND status = 'current'",
            (episode_id, shot_id)).fetchall()}
        return _shot_view(ctx, shot_id, sets, picks, conn, live=live, with_words=with_words)
