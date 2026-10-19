"""Preflight: check every shot against the documented model limits BEFORE any upload, estimate or reserve.

Used at three points (same rules, same messages):
- G1 plan (compute_g1_plan_activity): every shot, in dry and live runs; the G1 approval validator refuses
  approval while any error-severity violation exists.
- Submit (submit_still_job_enforced / submit_clip_job_enforced): defense in depth, right before spend.
- API / CLI (POST /api/episodes/{id}/preflight, `hfvg preflight`): per-shot pass/fail for creators.

Inputs are the normalized dicts produced by shot_inputs()/route_for(); missing optional values mean
"provider default" (aspect None -> field omitted, quality None -> field omitted), never a violation.
"""
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from temporalio.exceptions import ApplicationError

from hfvg import model_caps
from hfvg.config import max_refs_for, still_model_path
from hfvg.still_refs import assemble_still_refs

DEFAULT_CLIP_PATHS = {
    "kling": ("MODEL_PATH_KLING", "kling-video/v3.0/pro/image-to-video"),
    "seedance": ("MODEL_PATH_SEEDANCE", "bytedance/seedance-2.5/image-to-video"),
}
# Clip routes whose submit path sends int(duration) (Kling); others refuse fractional seconds (Seedance)
TRUNCATING_CLIP_ALIASES = ("kling",)
# Model statuses that refuse (no API path) vs. warn
REFUSE_STATUSES = {"mcp_only_not_on_api"}
WARN_STATUSES = {"deprecated_upstream", "configured_cap_only_not_routed"}
PIL_FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}


class PreflightError(ApplicationError, ValueError):
    """A shot breaks a documented model limit. Non-retryable: retrying cannot fix the shot.

    Subclasses Temporal's ApplicationError (non_retryable=True, type="PreflightError") so the activity
    fails once and the creator message reaches failed_shots; also a ValueError for existing callers.
    """

    def __init__(self, message: str, violations: list["Violation"] | None = None):
        super().__init__(message, type="PreflightError", non_retryable=True)
        self.violations = list(violations or [])


@dataclass
class Violation:
    shot_id: str
    frame: str  # "start" | "end" | "clip" | "audio"
    model: str
    field: str
    limit: Any
    actual: Any
    severity: str  # "error" | "warning"
    message: str
    source_url: str | None = None
    checked: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["text"] = format_violation(self)
        return d


def format_violation(v: Violation) -> str:
    """Creator-facing message plus where the limit comes from."""
    msg = v.message
    if v.source_url and v.checked:
        host = v.source_url.replace("https://", "").split("/")[0] if v.source_url.startswith("https://") else v.source_url
        msg += f" Limit source: {host}, checked {v.checked}."
    return msg


def raise_on_errors(violations: list[Violation], label: str) -> None:
    errors = [v for v in violations if v.severity == "error"]
    if errors:
        raise PreflightError(
            f"Preflight refused {label} before any spend:\n" + "\n".join(format_violation(v) for v in errors),
            errors,
        )


def clip_model_path(alias: str | None) -> str:
    """Harness clip routing alias (params.model: kling|seedance) -> model path, as the providers use it."""
    alias = alias or "kling"
    if alias in DEFAULT_CLIP_PATHS:
        env, default = DEFAULT_CLIP_PATHS[alias]
        return os.getenv(env, default)
    return alias


def _present(value: Any) -> bool:
    """A frame flag/URL counts only when truthy: shots.json uses end_frame: false for 'no end frame'."""
    return value not in (None, False, "", 0)


def shot_inputs(shot: dict[str, Any]) -> dict[str, Any]:
    """Normalize a G1 shot plan ({shot_id, prompt, end_prompt, params, refs}) or a shots.json entry
    ({id, prompt, end_prompt, model, duration, end_frame, ref_keys}) into preflight inputs.
    At plan time the start frame is the planned start still ("planned")."""
    params = shot.get("params") or {}

    def pick(key, default=None):
        return params.get(key, shot.get(key, default))

    end_flag = pick("end_frame")
    return {
        "id": shot.get("shot_id") or shot.get("id"),
        "start_prompt": shot.get("prompt") or "",
        "end_prompt": shot.get("end_prompt") or None,
        "clip_prompt": shot.get("clip_prompt") or shot.get("prompt") or "",
        "ref_keys": list(shot.get("refs") if shot.get("refs") is not None else shot.get("ref_keys") or []),
        "aspect_ratio": pick("aspect_ratio"),
        "resolution": pick("resolution"),
        "quality": pick("quality"),
        "duration": pick("duration"),
        "clip_model_alias": pick("model", "kling"),
        "start_frame": "planned",
        "end_frame": "planned" if _present(end_flag) else None,
    }


def route_for(shot: dict[str, Any], *, anchor_ref: str | None = None) -> dict[str, Any]:
    """Model routing for a normalized shot (still model from config, clip model from params.model)."""
    return {
        "still_model": still_model_path(),
        "clip_model": clip_model_path(shot.get("clip_model_alias")),
        "anchor_ref": anchor_ref,
    }


def _src(limit, registry):
    urls = limit.source_urls(registry) if limit else []
    return urls[0] if urls else (None, None)


def _resolve(registry, model, shot_id, frame, out):
    try:
        caps = registry.resolve(model)
    except model_caps.UnknownModelError as e:
        out.append(Violation(shot_id, frame, model, "model", "known model", model, "error",
                             f"Shot {shot_id} ({frame}): unknown model '{model}'. {e}"))
        return None
    if caps.status in REFUSE_STATUSES:
        out.append(Violation(shot_id, frame, model, "status", "active", caps.status, "error",
                             f"Shot {shot_id} ({frame}): {model} is MCP-only and cannot be used on the API path. "
                             "Choose a different model."))
    elif caps.status in WARN_STATUSES:
        what = "is deprecated upstream; consider a newer model" if caps.status == "deprecated_upstream" \
            else "has documented limits but is not routed by the harness yet"
        out.append(Violation(shot_id, frame, model, "status", "active", caps.status, "warning",
                             f"Shot {shot_id} ({frame}): {model} {what}."))
    return caps


def _check_choice(caps, registry, out, shot_id, frame, model, field, limit_name, value, label):
    limit = caps.limit(limit_name)
    if value is None or not limit or limit.unknown or not isinstance(limit.value, list):
        return
    if value not in limit.value:
        url, checked = _src(limit, registry)
        out.append(Violation(shot_id, frame, model, field, limit.value, value, "error",
                             f"Shot {shot_id} ({frame} frame) {label} '{value}' is not supported by {model}. "
                             f"Supported: {', '.join(map(str, limit.value))}.", url, checked))


def _check_prompt(caps, registry, out, shot_id, frame, model, prompt, where):
    limit = caps.limit("prompt_max_chars")
    if prompt and limit and not limit.unknown and isinstance(limit.value, int) and len(prompt) > limit.value:
        url, checked = _src(limit, registry)
        note = " (Higgsfield silently cuts the rest)" if "kling" in model.lower() else ""
        out.append(Violation(shot_id, frame, model, "prompt_max_chars", limit.value, len(prompt), "error",
                             f"Shot {shot_id} {where} prompt is {len(prompt)} characters; {model} accepts "
                             f"{limit.value}{note}. Shorten it by {len(prompt) - limit.value}.", url, checked))


def _still(shot, route, registry, phase, out):
    shot_id = shot["id"]
    model = route.get("still_model") or still_model_path()
    caps = _resolve(registry, model, shot_id, phase, out)
    if caps is None:
        return
    # Refs: same lead/anchor rule as submit. End stills lead with the start still: at plan time it is
    # implied (route without "lead_ref"); at submit time the caller passes the actual lead (or None).
    lead = route["lead_ref"] if "lead_ref" in route else ("start_still" if phase == "end" else None)
    cap = max_refs_for(model)
    assembled, _ = assemble_still_refs(lead=lead, anchor=route.get("anchor_ref"),
                                       planned=list(shot.get("ref_keys") or []), cap=cap)
    if len(assembled) > cap:
        url, checked = _src(caps.limit("max_refs"), registry)
        extra = " (including the start still that leads every end frame)" if lead else ""
        out.append(Violation(shot_id, phase, model, "max_refs", cap, len(assembled), "error",
                             f"Shot {shot_id} ({phase} frame) uses {len(assembled)} reference images{extra}, "
                             f"but {model} accepts at most {cap}. Remove {len(assembled) - cap} reference(s) "
                             "in shots.json (ref_keys) and try again (refused rather than silently dropping "
                             "refs).", url, checked))
    prompt = (shot.get("end_prompt") or shot.get("start_prompt")) if phase == "end" else shot.get("start_prompt")
    _check_prompt(caps, registry, out, shot_id, phase, model, prompt or "", f"({phase} frame)")
    _check_choice(caps, registry, out, shot_id, phase, model, "aspect_ratio", "aspect_ratios",
                  shot.get("aspect_ratio"), "aspect ratio")
    _check_choice(caps, registry, out, shot_id, phase, model, "resolution", "resolutions",
                  shot.get("resolution"), "resolution")
    # Quality: check what the provider actually sends (harness 'high' is a budget tier sent as medium)
    from hfvg.providers.higgsfield_still import provider_quality
    asked = shot.get("quality")
    sent = provider_quality(asked)
    if sent is not None and sent != asked:
        out.append(Violation(shot_id, phase, model, "quality", sent, asked, "warning",
                             f"Shot {shot_id} ({phase} frame) quality '{asked}' is sent to {model} as '{sent}'."))
    _check_choice(caps, registry, out, shot_id, phase, model, "quality", "quality", sent, "quality")


def _clip(shot, route, registry, files, out):
    shot_id = shot["id"]
    model = route.get("clip_model") or clip_model_path(shot.get("clip_model_alias"))
    caps = _resolve(registry, model, shot_id, "clip", out)
    if caps is None:
        return
    duration = shot.get("duration")
    dlim = caps.limit("duration_s")
    if duration is not None and dlim and not dlim.unknown and isinstance(dlim.value, dict):
        lo, hi = dlim.value.get("min", 0), dlim.value.get("max", float("inf"))
        url, checked = _src(dlim, registry)
        fractional = dlim.value.get("integer") and float(duration) != int(duration)
        truncates = any(model == clip_model_path(a) for a in TRUNCATING_CLIP_ALIASES)
        sent = int(duration) if fractional and truncates else duration
        if sent < lo or sent > hi:
            shown = f"{duration} s (sent as {sent} s)" if sent != duration else f"{duration} s"
            out.append(Violation(shot_id, "clip", model, "duration", f"{lo}-{hi}s", duration, "error",
                                 f"Shot {shot_id} clip is {shown}, but {model} needs a duration "
                                 f"from {lo} to {hi} seconds.", url, checked))
        if fractional and truncates:
            out.append(Violation(shot_id, "clip", model, "duration", "integer seconds", duration, "warning",
                                 f"Shot {shot_id} clip is {duration} s; {model} takes whole seconds, so it is "
                                 f"sent as {sent} s.", url, checked))
        elif fractional:
            out.append(Violation(shot_id, "clip", model, "duration", "integer seconds", duration, "error",
                                 f"Shot {shot_id} clip is {duration} s, but {model} needs a whole number of "
                                 f"seconds from {lo} to {hi}.", url, checked))
    _check_prompt(caps, registry, out, shot_id, "clip", model, shot.get("clip_prompt") or "", "clip")

    has_start, has_end = _present(shot.get("start_frame")), _present(shot.get("end_frame"))
    start_lim, end_lim = caps.limit("start_frame"), caps.limit("end_frame")
    if start_lim and start_lim.value == "required" and not has_start:
        out.append(Violation(shot_id, "clip", model, "start_frame", "required", "missing", "error",
                             f"Shot {shot_id} clip: {model} requires a start frame."))
    if has_end and end_lim and end_lim.value == "unsupported":
        out.append(Violation(shot_id, "clip", model, "end_frame", "unsupported", "present", "error",
                             f"Shot {shot_id} clip: {model} does not support end frames."))
    if has_end and not has_start:
        out.append(Violation(shot_id, "clip", model, "frames", "start required with end", "end only", "error",
                             f"Shot {shot_id} clip: {model} does not support last-frame-only. Provide a start frame."))
    if files:
        for key in ("start_frame", "end_frame"):
            ref = shot.get(key)
            if _present(ref) and ref in files:
                _frame_file(caps, registry, out, shot_id, model, key, Path(files[ref]))


def _frame_file(caps, registry, out, shot_id, model, key, path):
    """Frame file checks: real format (decoded, not the extension), bytes, min side, aspect range."""
    def err(field, lim, actual, msg, limit_obj):
        url, checked = _src(limit_obj, registry)
        out.append(Violation(shot_id, "clip", model, field, lim, actual, "error",
                             f"Shot {shot_id} clip {key}: {msg}", url, checked))

    if not path.exists():
        out.append(Violation(shot_id, "clip", model, "frame_file", "exists", "missing", "error",
                             f"Shot {shot_id} clip {key}: file {path.name} not found."))
        return
    blim = caps.limit("frame_max_bytes")
    size = path.stat().st_size
    if blim and not blim.unknown and isinstance(blim.value, int) and size > blim.value:
        err("frame_max_bytes", blim.value, size,
            f"{size} bytes is over the {model} limit of {blim.value} bytes.", blim)
    try:
        from PIL import Image
        with Image.open(path) as img:
            fmt, (w, h) = PIL_FORMATS.get(img.format or "", f"image/{(img.format or 'unknown').lower()}"), img.size
    except Exception:
        out.append(Violation(shot_id, "clip", model, "frame_format", "image", "unreadable", "error",
                             f"Shot {shot_id} clip {key}: {path.name} is not a readable image."))
        return
    flim = caps.limit("frame_formats")
    if flim and not flim.unknown and isinstance(flim.value, list) and fmt not in flim.value:
        err("frame_format", flim.value, fmt,
            f"format {fmt} not supported by {model}. Supported: {', '.join(flim.value)}.", flim)
    mlim = caps.limit("frame_min_px")
    if mlim and not mlim.unknown and isinstance(mlim.value, int) and min(w, h) < mlim.value:
        err("frame_min_px", mlim.value, min(w, h),
            f"minimum dimension {min(w, h)} px is below {model} requirement of {mlim.value} px.", mlim)
    alim = caps.limit("frame_aspect_range")
    if alim and not alim.unknown and isinstance(alim.value, list):
        lo, hi = alim.value
        ratio = w / h
        if ratio < lo or ratio > hi:
            err("frame_aspect_range", f"{lo:.2f}-{hi:.2f}", f"{ratio:.2f}",
                f"aspect ratio {ratio:.2f} is outside {model} range {lo:.2f}-{hi:.2f}.", alim)


def _audio(shot, route, registry, out):
    shot_id = shot["id"]
    model = route.get("audio_model") or shot.get("audio_model")
    caps = _resolve(registry, model, shot_id, "audio", out)
    if caps is None:
        return
    if caps.status == "alias_invalid_upstream_id":
        out.append(Violation(shot_id, "audio", model, "status", "upstream id", model, "warning",
                             f"Shot {shot_id} (audio): '{model}' is not an ElevenLabs model id; the harness sends music_v1."))
    text = shot.get("text") or ""
    tlim = caps.limit("text_max_chars")
    if text and tlim and not tlim.unknown and isinstance(tlim.value, int) and len(text) > tlim.value:
        url, checked = _src(tlim, registry)
        out.append(Violation(shot_id, "audio", model, "text_max_chars", tlim.value, len(text), "error",
                             f"Shot {shot_id} audio text is {len(text)} characters; {model} accepts {tlim.value}. "
                             f"Shorten it by {len(text) - tlim.value}.", url, checked))
    for field, key, unit in (("duration_s", "duration_s", "s"), ("duration_ms", "music_length_ms", "ms")):
        value, lim = shot.get(key), caps.limit(field)
        if value is None or not lim or lim.unknown or not isinstance(lim.value, dict):
            continue
        lo, hi = lim.value.get("min", 0), lim.value.get("max", float("inf"))
        if value < lo or value > hi:
            url, checked = _src(lim, registry)
            out.append(Violation(shot_id, "audio", model, field, f"{lo}-{hi}{unit}", value, "error",
                                 f"Shot {shot_id} audio is {value} {unit}, but {model} needs {lo} to {hi} {unit}.",
                                 url, checked))


def preflight_shot(
    shot: dict[str, Any],
    route: dict[str, Any],
    registry: model_caps.ModelCapsRegistry,
    *,
    phase: str,
    files: dict[str, str] | None = None,
) -> list[Violation]:
    """Check one shot for one phase ("start" | "end" | "clip" | "audio"). Returns violations (errors and
    warnings); never raises for a violation. Use raise_on_errors() at spend points."""
    out: list[Violation] = []
    if phase in ("start", "end"):
        _still(shot, route, registry, phase, out)
    elif phase == "clip":
        _clip(shot, route, registry, files, out)
    elif phase == "audio":
        _audio(shot, route, registry, out)
    else:
        raise ValueError(f"unknown preflight phase {phase!r}")
    return out


def preflight_episode(
    shots: list[dict[str, Any]],
    routing: dict[str, dict[str, Any]],
    registry: model_caps.ModelCapsRegistry,
    *,
    files: dict[str, str] | None = None,
) -> list[Violation]:
    """Every phase of every (normalized) shot: start, end when the shot has an end frame, clip."""
    out: list[Violation] = []
    for shot in shots:
        route = routing.get(shot["id"]) or route_for(shot)
        out += preflight_shot(shot, route, registry, phase="start", files=files)
        if _present(shot.get("end_frame")):
            out += preflight_shot(shot, route, registry, phase="end", files=files)
        if shot.get("duration"):
            out += preflight_shot(shot, route, registry, phase="clip", files=files)
    return out


def preflight_plans(shot_plans: list[dict[str, Any]], *, anchors: dict[str, str] | None = None) -> dict[str, Any]:
    """Preflight raw shot plans / shots.json entries. Returns a JSON-safe report:
    {"violations": [...], "errors": [...text], "shots": {shot_id: {"ok": bool, "errors": [...], "warnings": [...]}}}"""
    registry = model_caps.get_registry()
    shots = [shot_inputs(p) for p in shot_plans]
    routing = {s["id"]: route_for(s, anchor_ref=(anchors or {}).get(s["id"])) for s in shots}
    violations = preflight_episode(shots, routing, registry)
    per_shot = {s["id"]: {"ok": True, "errors": [], "warnings": []} for s in shots}
    for v in violations:
        entry = per_shot.setdefault(v.shot_id, {"ok": True, "errors": [], "warnings": []})
        if v.severity == "error":
            entry["ok"] = False
            entry["errors"].append(format_violation(v))
        else:
            entry["warnings"].append(format_violation(v))
    return {
        "violations": [v.to_dict() for v in violations],
        "errors": [format_violation(v) for v in violations if v.severity == "error"],
        "shots": per_shot,
    }
