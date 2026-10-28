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

from hfvg.qc.clip_constraints import ConstraintError, validate_constraints
from hfvg.qc.continuity import ContinuityError, validate_hard_checks
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
        
        # Optional clip_prompt: the motion prompt for the clip. When present it REPLACES the still
        # prompt for the clip submit (never appended to it); absent -> the clip uses the still prompt.
        clip_prompt = item.get("clip_prompt")
        if clip_prompt is not None:
            if not isinstance(clip_prompt, str):
                raise ShotDataError(f"shots.json shot {sid}: clip_prompt must be string, got {clip_prompt!r}")
            if not clip_prompt.strip():
                raise ShotDataError(f"shots.json shot {sid}: empty clip_prompt (omit the key to reuse the still prompt)")
            clip_prompt = clip_prompt.strip()

        trim_in, trim_out = _parse_trim(item, sid, duration)
        pre_approved = _parse_pre_approved_still(item.get("pre_approved_still"), sid)
        qc_fail_if = item.get("qc_fail_if")
        if qc_fail_if is not None:
            try:
                qc_fail_if = validate_constraints(qc_fail_if, f"shots.json shot {sid}: qc_fail_if")
            except ConstraintError as e:
                raise ShotDataError(str(e)) from None
            if not qc_fail_if:
                raise ShotDataError(f"shots.json shot {sid}: empty qc_fail_if (omit the key instead)")

        seedance_res = item.get("seedance_resolution")
        if seedance_res is not None:
            from hfvg.routing import SEEDANCE_RESOLUTIONS
            if model != "seedance":
                raise ShotDataError(f"shots.json shot {sid}: seedance_resolution is only valid with model 'seedance'")
            if seedance_res not in SEEDANCE_RESOLUTIONS:
                raise ShotDataError(
                    f"shots.json shot {sid}: seedance_resolution must be one of {SEEDANCE_RESOLUTIONS}, "
                    f"got {seedance_res!r}")

        ref_keys = _require(item, "ref_keys", list, sid)
        out[sid] = {
            "prompt": prompt,  # AS-IS: never recomposed
            # (the still prompt; the clip uses clip_prompt when present)
            "clip_prompt": clip_prompt,  # None -> clip reuses the still prompt; else replaces it
            "end_prompt": end_prompt,  # Optional: for _end still when end_frame=true
            "shot_meta": {
                "count": count,
                "characters": [str(c) for c in chars],
                "exterior": bool(item.get("exterior", False)),
                "water": bool(item.get("water", False)),
                "wet": wet,
                "antler": bool(item.get("antler_character", False)),
                "duck": bool(item.get("duck_present", False)),
                # declared at all? (the playbook duck_role rule only judges an explicit duck_present)
                "duck_declared": "duck_present" in item,
                "end_frame": end_frame,
                "seedance_resolution": seedance_res,
            },
            "params": {
                "model": model,
                "duration": duration,
                "aspect_ratio": str(item.get("aspect_ratio", "9:16")),
                "end_frame": end_frame,
                "seedance_resolution": seedance_res,  # None = tier default (final -> 1080p)
            },
            "trim": {"in": float(trim_in), "out": float(trim_out)},
            "tags": ["wet"] if wet else [],
            "ref_keys": [str(k) for k in ref_keys],
            "pre_approved_still": pre_approved,
            "qc_fail_if": list(qc_fail_if or []),
        }
    return out


def _number(value: Any, key: str, sid: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShotDataError(f"shots.json shot {sid}: {key} must be a number, got {value!r}")
    return float(value)


def _parse_trim(item: dict, sid: str, duration: float) -> tuple[float, float]:
    """The shot's trim window in clip seconds: (trim_in, trim_out), 0 <= in < out <= duration.

    Two equivalent spellings (one per shot, never both):
    - ``trim_in`` / ``trim_out``: absolute window into the generated clip
    - ``trim_head_s`` / ``trim_tail_s``: seconds cut from the clip start / end; mapped onto the same
      window (in = head, out = duration - tail), so assembly and QC use one representation.
    """
    absolute = [k for k in ("trim_in", "trim_out") if k in item]
    relative = [k for k in ("trim_head_s", "trim_tail_s") if k in item]
    if absolute and relative:
        raise ShotDataError(
            f"shots.json shot {sid}: use either trim_in/trim_out or trim_head_s/trim_tail_s, not both "
            f"(got {absolute + relative})"
        )
    if relative:
        head = _number(item.get("trim_head_s", 0), "trim_head_s", sid)
        tail = _number(item.get("trim_tail_s", 0), "trim_tail_s", sid)
        if head < 0 or tail < 0:
            raise ShotDataError(f"shots.json shot {sid}: trim_head_s / trim_tail_s must be >= 0 (got {head}, {tail})")
        if head + tail >= duration:
            raise ShotDataError(
                f"shots.json shot {sid}: trim_head_s + trim_tail_s ({head} + {tail}) must be shorter than "
                f"the clip duration {duration}"
            )
        trim_in, trim_out = head, float(duration) - tail
    else:
        trim_in = _number(item.get("trim_in", 0), "trim_in", sid)
        trim_out = _number(item.get("trim_out", duration), "trim_out", sid)
    if not (0 <= trim_in < trim_out <= duration):
        raise ShotDataError(
            f"shots.json shot {sid}: trim must satisfy 0 <= trim_in < trim_out <= duration "
            f"(got trim_in={trim_in}, trim_out={trim_out}, duration={duration})"
        )
    return trim_in, trim_out


_SHA256_HEX = set("0123456789abcdef")


def _parse_pre_approved_still(value: Any, sid: str) -> dict[str, Any] | None:
    """``pre_approved_still``: a path string or {"path", "sha256"?, "approved_by"?}.

    The owner already approved this start still (e.g. from an earlier run): the shot skips the still
    spend and uses it as its G2 still. Relative paths resolve against the episode directory (the
    directory of shots.json) when the still is prepared; existence and sha256 are checked then."""
    if value is None:
        return None
    if isinstance(value, str):
        value = {"path": value}
    if not isinstance(value, dict):
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still must be a path or an object, got {value!r}")
    unknown = sorted(set(value) - {"path", "sha256", "approved_by"})
    if unknown:
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still has unknown key(s) {unknown}")
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still.path must be a non-empty string")
    if path.startswith(("http://", "https://", "file:")):
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still.path must be a local file path, got {path!r}")
    sha = value.get("sha256")
    if sha is not None and (not isinstance(sha, str) or len(sha) != 64 or not set(sha.lower()) <= _SHA256_HEX):
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still.sha256 must be 64 hex chars")
    approved_by = value.get("approved_by", "owner")
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ShotDataError(f"shots.json shot {sid}: pre_approved_still.approved_by must be a non-empty string")
    return {"path": path.strip(), "sha256": sha.lower() if sha else None, "approved_by": approved_by.strip(),
            "source": "shots.json"}


def parse_episode_options(data: Any) -> dict[str, Any]:
    """Episode-level options from shots.json (top level, next to "shots").

    - ``continuity_hard_checks``: which continuity checks block the assembly (hfvg.qc.continuity);
      None when absent (the default policy applies).
    """
    if not isinstance(data, dict):
        return {"continuity_hard_checks": None}
    hard = data.get("continuity_hard_checks")
    if hard is not None:
        try:
            hard = validate_hard_checks(hard)
        except ContinuityError as e:
            raise ShotDataError(f"shots.json: {e}") from None
    return {"continuity_hard_checks": hard}


def load_episode_options(episode_id: str) -> dict[str, Any]:
    """parse_episode_options for the episode's shots.json ({} values when there is no shots.json)."""
    path = shots_json_path(episode_id)
    if not path.is_file():
        return parse_episode_options(None)
    return parse_episode_options(_read_json(path, "shots.json"))


def picture_seconds(shots: list[dict[str, Any]]) -> float:
    """Planned mute-picture length: the sum of every shot's trim window (out - in)."""
    total = 0.0
    for shot in shots:
        trim = shot.get("trim") or {}
        duration = float((shot.get("params") or {}).get("duration") or 0.0)
        t_in = float(trim.get("in", 0.0))
        t_out = float(trim.get("out", duration))
        total += max(0.0, t_out - t_in)
    return round(total, 3)


def pre_approved_still_paths(episode_id: str, plans: dict[str, dict[str, Any]]) -> dict[str, str]:
    """{shot_id: absolute path} of every shot's pre-approved start still (relative paths are
    resolved against the shots.json directory, like the activity that copies them)."""
    base = shots_json_path(episode_id).parent
    out: dict[str, str] = {}
    for sid, plan in plans.items():
        spec = plan.get("pre_approved_still")
        if spec:
            raw = Path(spec["path"]).expanduser()
            out[sid] = str(raw if raw.is_absolute() else (base / raw))
    return out


def resolve_refs(
    episode_id: str,
    ref_keys_by_shot: dict[str, list[str]],
    pre_approved_paths: dict[str, str] | None = None,
) -> dict[str, dict[str, list]]:
    """Resolve each shot's refs against refs.json and the filesystem (checked NOW, not the stale flag).

    A refs.json entry may use ``"pre_approved_still_of": "<shot_id>"`` instead of a path/url: it
    resolves to that shot's pre-approved start still, so an owner-approved still can be the
    continuity ref of other shots. Naming a shot without a pre-approved still is an error.

    Returns {shot_id: {"refs": [path-or-url of refs that exist], "missing_refs": [keys not on disk]}}.
    """
    pre_approved_paths = pre_approved_paths or {}
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
            of = entry.get("pre_approved_still_of")
            if of is not None:
                if url or ref_path:
                    raise ShotDataError(
                        f"refs.json {sid}/{entry.get('key')}: pre_approved_still_of cannot be combined with path/url"
                    )
                if of not in pre_approved_paths:
                    raise ShotDataError(
                        f"refs.json {sid}/{entry.get('key')}: pre_approved_still_of {of!r} names a shot "
                        f"without a pre_approved_still (have: {sorted(pre_approved_paths)})"
                    )
                ref_path = pre_approved_paths[of]
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
    resolved = resolve_refs(
        episode_id,
        {sid: p["ref_keys"] for sid, p in plans.items()},
        pre_approved_still_paths(episode_id, plans),
    )
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
        new["clip_prompt"] = plan.get("clip_prompt")  # Optional: replaces prompt for the clip only
        new["trim"] = dict(plan.get("trim") or {"in": 0.0, "out": float(plan["params"]["duration"])})
        new["shot_meta"] = plan["shot_meta"]
        new["params"] = {**dict(shot.get("params") or {}), **plan["params"]}
        new["tags"] = sorted(set(shot.get("tags") or []) | set(plan["tags"]))
        new["refs"] = plan["refs"]
        new["ref_keys"] = plan["ref_keys"]
        new["missing_refs"] = plan["missing_refs"]
        new["pre_approved_still"] = plan.get("pre_approved_still")
        new["qc_fail_if"] = list(plan.get("qc_fail_if") or [])
        merged.append(new)
    return merged
