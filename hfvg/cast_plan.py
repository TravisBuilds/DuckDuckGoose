"""Cast planning: a shot's ``cast`` (who is in frame) becomes identity refs, ref keys and prompt facts.

Runs where shots.json is planned: ``hfvg.shot_data.load_shot_plans`` (the parse activity, so the
result is recorded in workflow history and replays unchanged), the API's preflight / start checks
and the Cast panel's preview. Per shot with a cast:

- each character's identity refs: the locked sheet of the version (master, head sheet, 3/4, side,
  back, front) or, before a sheet is locked, its reference uploads (primary first);
- two or more characters in frame: a lineup image (each character's first ref side by side) leads;
- the still model's max refs (model_caps) minus one slot (the end frame's start still or an anchor
  still, hfvg.still_refs) minus the shot's own refs.json refs is the room for cast refs; when the
  refs do not fit, the last slot becomes one grid sheet of everything left (never silently dropped);
- ``shot_meta["cast"]``: per character its id, version and locked description clause (the still
  submit injects it, the lint requires it), and ``shot_meta["text_policy"]``.

A shot's ``props`` and ``place`` (hfvg.prop_store) are planned with it: their photos share the same
room (after the first picture of each character, each place and prop gets its main photo before
anyone's second picture; whatever does not fit goes into the grid sheet), and
``shot_meta["props"]`` holds the place, then the props, each with its clause. Shots without props
or a place get no ``props`` key, so cast-only shots plan exactly as before.

Composite images (lineup, grid) are built locally with Pillow (no text, no spend), content-addressed
under ``<cast root>/composites`` so the same inputs always give the same key and file.

Ref keys: ``cast:img:<sha256>.<ext>`` (an image of the library) and ``cast:comp:<sha256>`` (a
composite); both resolve from the key alone (:func:`resolve_cast_key`), so the G2 re-check needs no
refs.json entry for them.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
from pathlib import Path
from typing import Any

from hfvg import cast_store, model_caps, prop_store
from hfvg.cast_prompt import CAST_KEY_PREFIX, is_cast_key, parse_text_policy, prop_clause

logger = logging.getLogger(__name__)

COMPOSITE_VERSION = "v1"
RESERVED_SLOTS = 1
GRID_MAX_TILES = 9
LINEUP_TILE_H = 1024
LINEUP_MAX_W = 4096
GRID_CELL = 640
BACKGROUND = (236, 236, 236)
IMG_PREFIX = f"{CAST_KEY_PREFIX}img:"
COMP_PREFIX = f"{CAST_KEY_PREFIX}comp:"


class CastPlanError(ValueError):
    """The cast of one or more shots cannot be planned (unknown character, broken lock...)."""


def image_key(image: dict) -> str:
    return f"{IMG_PREFIX}{image['sha256']}.{image['ext']}"


def composites_dir() -> Path:
    return cast_store.cast_root() / "composites"


def resolve_cast_key(key: str) -> Path | None:
    """The file a cast ref key names (None for a malformed key). The file may not exist."""
    if key.startswith(IMG_PREFIX):
        name = key[len(IMG_PREFIX):]
        sha, _, ext = name.partition(".")
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha) and ext in ("png", "jpg", "webp"):
            return cast_store.image_file(sha, ext)
        return None
    if key.startswith(COMP_PREFIX):
        sha = key[len(COMP_PREFIX):]
        if len(sha) == 64 and all(c in "0123456789abcdef" for c in sha):
            return composites_dir() / f"{sha}.png"
    return None


def resolve_cast_refs(keys: list[str]) -> dict[str, list]:
    """{"refs": [paths on disk], "missing_refs": [keys whose file is gone]} for cast keys."""
    refs, missing = [], []
    for key in keys:
        path = resolve_cast_key(key)
        if path is not None and path.is_file():
            refs.append(str(path))
        else:
            missing.append(key)
    return {"refs": refs, "missing_refs": missing}


# ------------------------------------------------------------------------------------- composites

def _open_rgb(path: Path):
    from PIL import Image, ImageOps
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            flat = Image.new("RGB", img.size, BACKGROUND)
            flat.paste(img, mask=img.split()[-1])
            return flat
        return img.convert("RGB")


def _build_lineup(paths: list[Path]) -> bytes:
    from PIL import Image
    tiles = []
    for p in paths:
        img = _open_rgb(p)
        scale = min(LINEUP_TILE_H / img.height, LINEUP_TILE_H / img.width)
        tiles.append(img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale)))))
    pad = 32
    width = sum(t.width for t in tiles) + pad * (len(tiles) + 1)
    canvas = Image.new("RGB", (width, LINEUP_TILE_H + 2 * pad), BACKGROUND)
    x = pad
    for t in tiles:
        canvas.paste(t, (x, pad + (LINEUP_TILE_H - t.height) // 2))
        x += t.width + pad
    if canvas.width > LINEUP_MAX_W:
        canvas = canvas.resize((LINEUP_MAX_W, max(1, round(canvas.height * LINEUP_MAX_W / canvas.width))))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def _build_grid(paths: list[Path]) -> bytes:
    from PIL import Image
    n = len(paths)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    pad = 16
    canvas = Image.new("RGB", (cols * GRID_CELL, rows * GRID_CELL), BACKGROUND)
    for i, p in enumerate(paths):
        img = _open_rgb(p)
        img.thumbnail((GRID_CELL - 2 * pad, GRID_CELL - 2 * pad))
        cx = (i % cols) * GRID_CELL + (GRID_CELL - img.width) // 2
        cy = (i // cols) * GRID_CELL + (GRID_CELL - img.height) // 2
        canvas.paste(img, (cx, cy))
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=False)
    return buf.getvalue()


def composite(kind: str, images: list[dict], *, materialize: bool = True) -> tuple[str, Path]:
    """(key, path) of a lineup or grid composite of library images; built once, content-addressed."""
    if kind not in ("lineup", "grid"):
        raise ValueError(f"unknown composite kind {kind!r}")
    sources = [i["sha256"] for i in images]
    digest = hashlib.sha256(f"{kind}|{COMPOSITE_VERSION}|{','.join(sources)}".encode()).hexdigest()
    path = composites_dir() / f"{digest}.png"
    if materialize and not path.is_file():
        paths = [cast_store.image_file(i["sha256"], i["ext"]) for i in images]
        data = _build_lineup(paths) if kind == "lineup" else _build_grid(paths)
        cast_store._write_once(path, data)
        cast_store._write_once(path.with_suffix(".json"), json.dumps(
            {"kind": kind, "version": COMPOSITE_VERSION, "sources": sources}).encode())
    return f"{COMP_PREFIX}{digest}", path


# ------------------------------------------------------------------------------------- planning

def kit_text_policy(episode_id: str) -> str | None:
    """The episode prompt kit's text_policy (None when absent or the kit cannot be read: the lint
    reports a broken kit on its own). Raises CastPlanError for an invalid value."""
    from hfvg.paths import episode_dir
    path = episode_dir(episode_id) / "prompt_kit.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return parse_text_policy(data.get("text_policy"), "prompt_kit.json")
    except ValueError as e:
        raise CastPlanError(str(e)) from None


def shot_text_policy(plan: dict, kit_policy: str | None) -> str | None:
    """"no_text" when the shot's stills get the no-text constraint, else None. The shot's own
    allow_text wins; then the kit's policy; with neither, shots with a cast get no_text."""
    allow = plan.get("allow_text")
    if allow is True:
        return None
    if allow is False:
        return "no_text"
    if kit_policy == "allow_text":
        return None
    if kit_policy == "no_text" or plan.get("cast"):
        return "no_text"
    return None


def still_ref_cap() -> int:
    from hfvg.config import max_refs_for, still_model_path
    return max_refs_for(still_model_path())


def _choose_refs(chars: list[dict], budget: int, materialize: bool,
                 things: list[dict] | None = None) -> tuple[list[tuple[str, Path, str]], list[str]]:
    """[(key, path, label)] within ``budget`` slots, plus notes. chars and things (the shot's place,
    then its props): [{"name", "images"}]. Order: the lineup (two or more characters), each
    character's first picture, each place's and prop's first photo, then the characters' further
    pictures, then the further photos."""
    notes: list[str] = []
    with_images = [c for c in chars if c["images"]]
    shown = [t for t in things or [] if t["images"]]
    what = "reference images" if shown else "cast reference images"
    wanted: list[tuple[str, Any, str]] = []  # (kind, payload, label)
    if len(with_images) >= 2:
        firsts = [c["images"][0] for c in with_images]
        wanted.append(("lineup", firsts, "lineup: " + " + ".join(c["name"] for c in with_images)))

    def rank(group: list[dict], n: int) -> list[tuple[str, Any, str]]:
        return [("image", g["images"][n], f"{g['name']} {_role_name(g['images'][n])}")
                for g in group if n < len(g["images"])]

    depth = max((len(g["images"]) for g in with_images + shown), default=0)
    wanted += rank(with_images, 0) + rank(shown, 0)
    for n in range(1, depth):
        wanted += rank(with_images, n)
    for n in range(1, depth):
        wanted += rank(shown, n)
    if not wanted:
        return [], notes
    if budget <= 0:
        notes.append(f"no room for {what} (the shot's own refs fill the still model's limit); "
                     "only the descriptions are used")
        return [], notes
    if len(wanted) <= budget:
        chosen = wanted
    else:
        keep = wanted[:budget - 1]
        rest = [w for w in wanted[budget - 1:] if w[0] == "image"]
        if budget == 1:
            keep, rest = [], [w for w in wanted if w[0] == "image"]
        tiles = rest[:GRID_MAX_TILES]
        if len(rest) > GRID_MAX_TILES:
            notes.append(f"{len(rest) - GRID_MAX_TILES} {what} did not fit the grid sheet")
        notes.append(f"{len(rest)} {what} were merged into one grid sheet to stay within "
                     f"the still model's limit")
        chosen = keep + [("grid", [w[1] for w in tiles], "grid sheet: " + ", ".join(w[2] for w in tiles))]
    out = []
    for kind, payload, label in chosen:
        if kind == "image":
            out.append((image_key(payload), cast_store.image_file(payload["sha256"], payload["ext"]), label))
        else:
            key, path = composite(kind, payload, materialize=materialize)
            out.append((key, path, label))
    return out, notes


def _role_name(image: dict) -> str:
    role = image.get("role") or "photo"
    return {"upload": "reference", "master": "master", "head_sheet": "head sheet",
            "turnaround_three_quarter": "3/4 view", "turnaround_side": "side view",
            "turnaround_back": "back view", "turnaround_front": "front view"}.get(role, role)


def _plan_props(sid: str, plan: dict, items: dict[str, dict], where: str, errors: list[str],
                shot_report: dict) -> tuple[list[dict], list[dict]]:
    """The shot's place and props: (``shot_meta["props"]`` entries, [{"name", "images"}] for the
    refs), the place first. An unknown id, or a prop picked as the place (or the other way round),
    is an error."""
    wanted = ([(plan["place"], "place")] if plan.get("place") else []) + [(s, "prop") for s in plan.get("props") or []]
    if not wanted:
        return [], []
    picked, things = [], []
    shot_report["props"] = []
    for slug, kind in wanted:
        item = items.get(slug)
        if item is None:
            errors.append(f"shot {sid}: {kind} {slug!r} is not in the props and places of {where} "
                          "(add it in Props & places)")
            continue
        if item["kind"] != kind:
            fix = "the shot's place" if item["kind"] == "place" else "one of the shot's props"
            errors.append(f"shot {sid}: {item['name']} is a {item['kind']}, not a {kind}; pick it as {fix}")
            continue
        photos = prop_store.reference_photos(item)
        if not photos:
            shot_report["notes"].append(f"{item['name']} has no photo yet: only the description is used")
        picked.append({"slug": slug, "kind": kind, "name": item["name"],
                       "clause": prop_clause(item["name"], item["description"], kind)})
        things.append({"name": item["name"], "images": photos})
        shot_report["props"].append({"slug": slug, "kind": kind, "name": item["name"],
                                     "images": [p["image_id"] for p in photos]})
    return picked, things


def plan_cast(episode_id: str, plans: dict[str, dict[str, Any]], *, live: bool,
              materialize: bool = True) -> dict[str, Any]:
    """Apply the cast, the props and places and the text policy to parsed shot plans (in place).
    Returns the report ``{"library": {...} | None, "text_policy", "shots": {sid: {...}}, "errors":
    [...], "notes": [...]}``.

    Shots without a cast, props or a place keep their refs and ref keys; a kit without a
    text_policy changes nothing for them. The library is read only when some shot picks from it."""
    report: dict[str, Any] = {"library": None, "text_policy": None, "shots": {}, "errors": [], "notes": []}
    try:
        kit_policy = kit_text_policy(episode_id)
    except CastPlanError as e:
        report["errors"].append(str(e))
        return report
    report["text_policy"] = kit_policy
    cast_shots = [sid for sid, p in plans.items() if p.get("cast") or p.get("props") or p.get("place")]
    for sid, plan in plans.items():
        policy = shot_text_policy(plan, kit_policy)
        if policy:
            plan["shot_meta"] = {**plan["shot_meta"], "text_policy": policy}
    if not cast_shots:
        return report

    library = cast_store.library_for_episode(episode_id)
    report["library"] = {"kind": library[0], "id": library[1]}
    where = f"series {library[1]}" if library[0] == "series" else "this episode"
    characters = {c["slug"]: c for c in cast_store.list_characters(library)}
    items = ({i["slug"]: i for i in prop_store.list_items(library)}
             if any(plans[sid].get("props") or plans[sid].get("place") for sid in cast_shots) else {})
    try:
        cap = still_ref_cap()
    except (model_caps.UnknownModelError, ValueError) as e:
        report["errors"].append(f"cannot read the still model's reference limit: {e}")
        return report
    verified: dict[tuple[str, int], list[str]] = {}
    for sid in cast_shots:
        plan = plans[sid]
        shot_report: dict[str, Any] = {"characters": [], "refs": [], "notes": []}
        entries, chars = [], []
        for req in plan.get("cast") or []:
            character = characters.get(req["character"])
            if character is None:
                report["errors"].append(f"shot {sid}: cast character {req['character']!r} is not in the cast of "
                                        f"{where} (add it in the Cast panel)")
                continue
            number = req["version"] or character["default_version"]
            version = next((v for v in character["versions"] if v["version"] == number), None)
            if version is None:
                report["errors"].append(f"shot {sid}: {character['name']} has no version {number}")
                continue
            if version["locked"]:
                key = (character["character_id"], number)
                if key not in verified:
                    verified[key] = cast_store.verify_lock(character["character_id"], number)
                if verified[key]:
                    report["errors"].extend(f"shot {sid}: {p}" for p in verified[key])
                    continue
            images, notes = cast_store.identity_images(character, version, live=live)
            shot_report["notes"].extend(notes)
            if not images:
                shot_report["notes"].append(f"{character['name']} has no reference images yet: only the "
                                            "description is used")
            entries.append({"character": character["slug"], "version": number, "name": character["name"],
                            "clause": cast_store.version_clause(character, version),
                            "locked": bool(version["locked"])})
            chars.append({"name": character["name"], "images": images})
            shot_report["characters"].append({"character": character["slug"], "version": number,
                                              "name": character["name"], "locked": bool(version["locked"]),
                                              "images": [i["image_id"] for i in images]})
        picked, things = _plan_props(sid, plan, items, where, report["errors"], shot_report)
        explicit = len(plan.get("ref_keys") or [])
        budget = cap - RESERVED_SLOTS - explicit
        chosen, notes = _choose_refs(chars, budget, materialize, things)
        shot_report["notes"].extend(notes)
        shot_report["refs"] = [{"key": k, "label": label} for k, _p, label in chosen]
        shot_report["cap"] = cap
        plan["ref_keys"] = list(plan.get("ref_keys") or []) + [k for k, _p, _l in chosen]
        present = [str(p) for _k, p, _l in chosen if (not materialize) or p.is_file()]
        absent = [k for k, p, _l in chosen if materialize and not p.is_file()]
        plan["refs"] = list(plan.get("refs") or []) + present
        plan["missing_refs"] = list(plan.get("missing_refs") or []) + absent
        meta = dict(plan["shot_meta"])
        if plan.get("cast"):
            meta["cast"] = entries
        if plan.get("props") or plan.get("place"):
            meta["props"] = picked
        plan["shot_meta"] = meta
        report["shots"][sid] = shot_report
        for note in shot_report["notes"]:
            logger.info("cast plan %s/%s: %s", episode_id, sid, note)
    return report


def split_keys(keys: list[str]) -> tuple[list[str], list[str]]:
    """(refs.json keys, cast keys) of a shot's ref keys, each in order."""
    return [k for k in keys if not is_cast_key(k)], [k for k in keys if is_cast_key(k)]


def cast_problems(episode_id: str, shots_raw: Any) -> list[str]:
    """The cast errors of a shots.json: what the run's planning would refuse (unknown characters,
    versions, props or places, a broken lock, an invalid kit text_policy). None for shots without
    a cast, props or a place."""
    try:
        return list(preview_episode_cast(episode_id, shots_raw, live=False)["errors"])
    except ValueError as e:
        return [str(e)]


def preview_episode_cast(episode_id: str, shots_raw: Any, *, live: bool) -> dict[str, Any]:
    """The Cast panel / preflight view of an uploaded shots.json: what each shot's cast plans to
    (no composite files written). Raises ValueError for a shots.json that does not parse."""
    from hfvg.shot_data import episode_profile, parse_shots
    plans = parse_shots(shots_raw, episode_profile(episode_id))
    for plan in plans.values():
        plan.setdefault("refs", [])
        plan.setdefault("missing_refs", [])
    report = plan_cast(episode_id, plans, live=live, materialize=False)
    for sid, plan in plans.items():
        meta = plan["shot_meta"]
        row = report["shots"].setdefault(sid, {"characters": [], "refs": [], "notes": []})
        row["text_policy"] = meta.get("text_policy")
        row["cast"] = plan.get("cast") or []
        row["prop_ids"] = plan.get("props") or []
        row["place"] = plan.get("place")
    return report
