"""Per-shot private data (shots.json + refs.json): loader, validation, merge into shot plans.

Private inputs (never committed): a ``shots.json`` (per-shot count, characters, wet, model, duration,
end frame, antler/duck flags and the FINAL ``prompt``, used as-is) and a ``refs.json`` (per-shot ref
lists). Paths come from ``SHOTS_JSON_PATH`` / ``REFS_JSON_PATH`` or default to
``<EPISODES_DIR>/<episode>/shots.json`` / ``refs.json`` (uploadable via the local API).

Live mode (DRY_RUN=false or a non-dry episode) refuses when the data is missing or inconsistent.
A ref whose file is not on disk is NEVER dropped silently: it is listed in ``missing_refs`` and the
shot is held at G2.01 until the file exists.
"""

import json
import os
from pathlib import Path
from typing import Any

from hfvg.routing import MODELS


class ShotDataError(ValueError):
    """Per-shot data is missing or invalid (fail closed in live mode)."""


def shots_json_path(episode_id: str) -> Path:
    override = os.getenv("SHOTS_JSON_PATH", "").strip()
    if override:
        return Path(override)
    return Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "shots.json"


def refs_json_path(episode_id: str) -> Path:
    override = os.getenv("REFS_JSON_PATH", "").strip()
    if override:
        return Path(override)
    return Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "refs.json"


def _read_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ShotDataError(f"Cannot read {what} {path}: {e}") from e


def _require(shot: dict, key: str, kind: type | tuple, sid: str) -> Any:
    value = shot.get(key)
    if isinstance(value, bool) and kind is not bool:
        value = None
    if not isinstance(value, kind):
        raise ShotDataError(f"shots.json shot {sid}: field {key!r} must be {kind}, got {value!r}")
    return value


def parse_shots(data: Any) -> dict[str, dict[str, Any]]:
    """Validate shots.json content and return {shot_id: normalized plan fields}."""
    items = data.get("shots") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise ShotDataError("shots.json must contain a non-empty 'shots' list")
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ShotDataError("shots.json 'shots' entries must be objects")
        sid = _require(item, "id", str, "?")
        if sid in out:
            raise ShotDataError(f"shots.json: duplicate shot id {sid}")
        count = _require(item, "count", int, sid)
        chars = _require(item, "characters", list, sid)
        wet = _require(item, "wet", bool, sid)
        model = item.get("model")
        if model not in MODELS:
            raise ShotDataError(f"shots.json shot {sid}: model must be one of {MODELS}, got {model!r}")
        duration = item.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            raise ShotDataError(f"shots.json shot {sid}: duration must be a positive number")
        end_frame = _require(item, "end_frame", bool, sid)
        prompt = _require(item, "prompt", str, sid)
        if not prompt.strip():
            raise ShotDataError(f"shots.json shot {sid}: empty prompt")
        
        # Optional end_prompt for end-frame shots (_end still uses this instead of prompt)
        end_prompt = item.get("end_prompt")
        if end_prompt is not None:
            if not isinstance(end_prompt, str):
                raise ShotDataError(f"shots.json shot {sid}: end_prompt must be string, got {end_prompt!r}")
            if not end_prompt.strip():
                raise ShotDataError(f"shots.json shot {sid}: empty end_prompt")
        
        ref_keys = _require(item, "ref_keys", list, sid)
        out[sid] = {
            "prompt": prompt,  # AS-IS: never recomposed
            "end_prompt": end_prompt,  # Optional: for _end still when end_frame=true
            "shot_meta": {
                "count": count,
                "characters": [str(c) for c in chars],
                "exterior": bool(item.get("exterior", False)),
                "water": bool(item.get("water", False)),
                "wet": wet,
                "antler": bool(item.get("antler_character", False)),
                "duck": bool(item.get("duck_present", False)),
                "end_frame": end_frame,
            },
            "params": {
                "model": model,
                "duration": duration,
                "aspect_ratio": str(item.get("aspect_ratio", "9:16")),
                "end_frame": end_frame,
            },
            "tags": ["wet"] if wet else [],
            "ref_keys": [str(k) for k in ref_keys],
        }
    return out


def resolve_refs(episode_id: str, ref_keys_by_shot: dict[str, list[str]]) -> dict[str, dict[str, list]]:
    """Resolve each shot's refs against refs.json and the filesystem (checked NOW, not the stale flag).

    Returns {shot_id: {"refs": [path-or-url of refs that exist], "missing_refs": [keys not on disk]}}.
    """
    path = refs_json_path(episode_id)
    if not path.is_file():
        raise ShotDataError(f"No refs.json found for {episode_id} (looked at {path})")
    per_shot = _read_json(path, "refs.json").get("per_shot")
    if not isinstance(per_shot, dict):
        raise ShotDataError("refs.json must contain a 'per_shot' object")
    out: dict[str, dict[str, list]] = {}
    for sid, keys in ref_keys_by_shot.items():
        entries = per_shot.get(sid)
        if not isinstance(entries, list):
            raise ShotDataError(f"refs.json has no ref list for shot {sid}")
        listed = [e.get("key") for e in entries if isinstance(e, dict)]
        if listed != keys:
            raise ShotDataError(f"refs.json refs for {sid} {listed} differ from shots.json ref_keys {keys}")
        refs: list[str] = []
        missing: list[str] = []
        for entry in entries:
            url = str(entry.get("url") or "")
            ref_path = str(entry.get("path") or "")
            if url.startswith(("http://", "https://")):
                refs.append(url)
            elif ref_path and os.path.isfile(ref_path):
                refs.append(ref_path)
            else:
                missing.append(entry["key"])
        out[sid] = {"refs": refs, "missing_refs": missing}
    return out


def load_shot_plans(episode_id: str, require: bool) -> dict[str, dict[str, Any]] | None:
    """Load shots.json (+ refs). None when absent and not required; ShotDataError when required."""
    path = shots_json_path(episode_id)
    if not path.is_file():
        if require:
            raise ShotDataError(
                f"Live mode requires per-shot data for {episode_id}: no shots.json at {path} "
                "(set SHOTS_JSON_PATH or upload it)"
            )
        return None
    plans = parse_shots(_read_json(path, "shots.json"))
    if not refs_json_path(episode_id).is_file() and not require:
        for plan in plans.values():
            plan.update({"refs": [], "missing_refs": []})
        return plans
    resolved = resolve_refs(episode_id, {sid: p["ref_keys"] for sid, p in plans.items()})
    for sid, plan in plans.items():
        plan.update(resolved[sid])
    return plans


def merge_into_shots(
    episode_id: str, shots: list[dict[str, Any]], plans: dict[str, dict[str, Any]] | None, require: bool
) -> list[dict[str, Any]]:
    """Overlay per-shot data on parsed beatmap shots (prompt as-is). Live: shot sets must match."""
    if plans is None:
        return shots
    beat_ids = {s["shot_id"] for s in shots}
    if require and beat_ids != set(plans):
        raise ShotDataError(
            f"shots.json ids differ from beatmap: only in beatmap {sorted(beat_ids - set(plans))}, "
            f"only in shots.json {sorted(set(plans) - beat_ids)}"
        )
    merged = []
    for shot in shots:
        plan = plans.get(shot["shot_id"])
        if plan is None:
            merged.append(shot)
            continue
        new = dict(shot)
        new["prompt"] = plan["prompt"]
        new["end_prompt"] = plan.get("end_prompt")  # Optional: for _end still
        new["shot_meta"] = plan["shot_meta"]
        new["params"] = {**dict(shot.get("params") or {}), **plan["params"]}
        new["tags"] = sorted(set(shot.get("tags") or []) | set(plan["tags"]))
        new["refs"] = plan["refs"]
        new["ref_keys"] = plan["ref_keys"]
        new["missing_refs"] = plan["missing_refs"]
        merged.append(new)
    return merged
