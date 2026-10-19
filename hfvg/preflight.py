"""Preflight checks: validate shot specs against model limits before any spend."""
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hfvg import model_caps
from hfvg.activities.studio_generation import assemble_still_refs
from hfvg.config import max_refs_for, still_model_path


class PreflightError(Exception):
    """Non-retryable error raised when preflight validation fails."""

    def __init__(self, message: str):
        super().__init__(message)
        self.non_retryable = True


@dataclass
class Violation:
    """A preflight violation."""

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


def format_violation(v: Violation) -> str:
    """Format a violation as a creator-facing message."""
    msg = v.message
    if v.source_url and v.checked:
        # Extract domain or key part of URL for brevity
        url_display = v.source_url
        if url_display.startswith("https://"):
            url_display = url_display.replace("https://", "").split("/")[0]
        msg += f" Limit source: {url_display}, checked {v.checked}."
    return msg


def preflight_shot(
    shot: dict[str, Any],
    route: dict[str, Any],
    registry: model_caps.ModelCapsRegistry,
    *,
    phase: str,
    files: dict[str, str] | None = None,
) -> list[Violation]:
    """
    Run preflight checks on one shot.

    Args:
        shot: shot dict from shots.json (id, start_prompt, end_prompt, clip_prompt, duration, refs, etc.)
        route: routing info (still_model, clip_model, etc.)
        registry: model capabilities registry
        phase: "start" | "end" | "clip" | "audio"
        files: optional dict of {key: local_path} for file-based checks (format, size, dimensions)

    Returns:
        list of Violation
    """
    violations = []
    shot_id = shot["id"]

    # Still checks (start and/or end)
    if phase in ("start", "end"):
        still_model = route.get("still_model", still_model_path())
        
        try:
            caps = registry.resolve(still_model)
        except model_caps.UnknownModelError as e:
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame=phase,
                    model=still_model,
                    field="model",
                    limit="known model",
                    actual=still_model,
                    severity="error",
                    message=f"Shot {shot_id} ({phase} frame): unknown model '{still_model}'. {e}",
                )
            )
            return violations  # Can't check further without caps

        # Check model status
        if caps.status == "mcp_only_not_on_api":
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame=phase,
                    model=still_model,
                    field="status",
                    limit="active",
                    actual=caps.status,
                    severity="error",
                    message=f"Shot {shot_id} ({phase} frame): {still_model} is MCP-only and cannot be used on the API path. Choose a different model.",
                )
            )
        elif caps.status == "deprecated_upstream":
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame=phase,
                    model=still_model,
                    field="status",
                    limit="active",
                    actual=caps.status,
                    severity="warning",
                    message=f"Shot {shot_id} ({phase} frame): {still_model} is deprecated. Consider migrating to a newer model.",
                )
            )

        # Ref count check
        planned_refs = shot.get("ref_keys", [])
        # For end frames, we need to account for lead ref (start still)
        lead_ref = None
        if phase == "end":
            # In practice, lead_ref would be the start still path, but for preflight we just know it exists
            lead_ref = "placeholder_start_still"  # Counted as 1 ref

        # Get anchor if configured (would come from DB in practice; for preflight check route)
        anchor_ref = route.get("anchor_ref")

        # Assemble refs using the same logic as submit
        max_refs = max_refs_for(still_model)
        assembled, anchor_used = assemble_still_refs(
            lead=lead_ref,
            anchor=anchor_ref,
            planned=planned_refs,
            cap=max_refs,
        )

        max_refs_limit = caps.limit("max_refs")
        if max_refs_limit and not max_refs_limit.unknown and len(assembled) > max_refs_limit.value:
            source_urls = max_refs_limit.source_urls(registry)
            source_url, checked = source_urls[0] if source_urls else (None, None)
            
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame=phase,
                    model=still_model,
                    field="max_refs",
                    limit=max_refs_limit.value,
                    actual=len(assembled),
                    severity="error",
                    message=(
                        f"Shot {shot_id} ({phase} frame) uses {len(assembled)} reference images, "
                        f"but {still_model} accepts at most {max_refs_limit.value}. "
                        f"Remove {len(assembled) - max_refs_limit.value} reference(s) in shots.json (ref_keys) and try again."
                    ),
                    source_url=source_url,
                    checked=checked,
                )
            )

        # Prompt length check
        prompt_field = "end_prompt" if phase == "end" and "end_prompt" in shot else "start_prompt"
        prompt = shot.get(prompt_field, "")
        prompt_limit = caps.limit("prompt_max_chars")
        if prompt_limit and not prompt_limit.unknown and len(prompt) > prompt_limit.value:
            source_urls = prompt_limit.source_urls(registry)
            source_url, checked = source_urls[0] if source_urls else (None, None)
            
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame=phase,
                    model=still_model,
                    field="prompt_max_chars",
                    limit=prompt_limit.value,
                    actual=len(prompt),
                    severity="error",
                    message=(
                        f"Shot {shot_id} ({phase} frame) prompt is {len(prompt)} characters; "
                        f"{still_model} accepts {prompt_limit.value}. "
                        f"Shorten it by {len(prompt) - prompt_limit.value}."
                    ),
                    source_url=source_url,
                    checked=checked,
                )
            )

        # Aspect ratio check
        aspect = shot.get("aspect_ratio", "9:16")
        aspect_limit = caps.limit("aspect_ratios")
        if aspect_limit and not aspect_limit.unknown:
            if isinstance(aspect_limit.value, list) and aspect not in aspect_limit.value:
                source_urls = aspect_limit.source_urls(registry)
                source_url, checked = source_urls[0] if source_urls else (None, None)
                
                violations.append(
                    Violation(
                        shot_id=shot_id,
                        frame=phase,
                        model=still_model,
                        field="aspect_ratio",
                        limit=aspect_limit.value,
                        actual=aspect,
                        severity="error",
                        message=(
                            f"Shot {shot_id} ({phase} frame) aspect ratio '{aspect}' is not supported by {still_model}. "
                            f"Supported: {', '.join(aspect_limit.value)}."
                        ),
                        source_url=source_url,
                        checked=checked,
                    )
                )

        # Resolution check
        resolution = shot.get("resolution", "1k")
        resolution_limit = caps.limit("resolutions")
        if resolution_limit and not resolution_limit.unknown:
            if isinstance(resolution_limit.value, list) and resolution not in resolution_limit.value:
                source_urls = resolution_limit.source_urls(registry)
                source_url, checked = source_urls[0] if source_urls else (None, None)
                
                violations.append(
                    Violation(
                        shot_id=shot_id,
                        frame=phase,
                        model=still_model,
                        field="resolution",
                        limit=resolution_limit.value,
                        actual=resolution,
                        severity="error",
                        message=(
                            f"Shot {shot_id} ({phase} frame) resolution '{resolution}' is not supported by {still_model}. "
                            f"Supported: {', '.join(resolution_limit.value)}."
                        ),
                        source_url=source_url,
                        checked=checked,
                    )
                )

        # Quality check
        quality = shot.get("quality", "medium")
        quality_limit = caps.limit("quality")
        if quality_limit and not quality_limit.unknown:
            if isinstance(quality_limit.value, list) and quality not in quality_limit.value:
                source_urls = quality_limit.source_urls(registry)
                source_url, checked = source_urls[0] if source_urls else (None, None)
                
                violations.append(
                    Violation(
                        shot_id=shot_id,
                        frame=phase,
                        model=still_model,
                        field="quality",
                        limit=quality_limit.value,
                        actual=quality,
                        severity="error",
                        message=(
                            f"Shot {shot_id} ({phase} frame) quality '{quality}' is not supported by {still_model}. "
                            f"Supported: {', '.join(quality_limit.value)}."
                        ),
                        source_url=source_url,
                        checked=checked,
                    )
                )

    # Clip checks
    if phase == "clip":
        clip_model = route.get("clip_model", "kling-video/v3.0/pro/image-to-video")
        
        try:
            caps = registry.resolve(clip_model)
        except model_caps.UnknownModelError as e:
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame="clip",
                    model=clip_model,
                    field="model",
                    limit="known model",
                    actual=clip_model,
                    severity="error",
                    message=f"Shot {shot_id} (clip): unknown model '{clip_model}'. {e}",
                )
            )
            return violations

        # Duration check
        duration = shot.get("duration")
        duration_limit = caps.limit("duration_s")
        if duration_limit and not duration_limit.unknown:
            dur_spec = duration_limit.value
            if isinstance(dur_spec, dict):
                min_dur = dur_spec.get("min", 0)
                max_dur = dur_spec.get("max", float("inf"))
                integer_required = dur_spec.get("integer", False)

                if duration is not None:
                    if duration < min_dur or duration > max_dur:
                        source_urls = duration_limit.source_urls(registry)
                        source_url, checked = source_urls[0] if source_urls else (None, None)
                        
                        violations.append(
                            Violation(
                                shot_id=shot_id,
                                frame="clip",
                                model=clip_model,
                                field="duration",
                                limit=f"{min_dur}-{max_dur}s",
                                actual=duration,
                                severity="error",
                                message=(
                                    f"Shot {shot_id} clip is {duration} s, but {clip_model} needs a duration "
                                    f"from {min_dur} to {max_dur} seconds."
                                ),
                                source_url=source_url,
                                checked=checked,
                            )
                        )

                    if integer_required and not isinstance(duration, int) and duration != int(duration):
                        source_urls = duration_limit.source_urls(registry)
                        source_url, checked = source_urls[0] if source_urls else (None, None)
                        
                        violations.append(
                            Violation(
                                shot_id=shot_id,
                                frame="clip",
                                model=clip_model,
                                field="duration",
                                limit="integer seconds",
                                actual=duration,
                                severity="error",
                                message=(
                                    f"Shot {shot_id} clip is {duration} s, but {clip_model} needs a whole number of seconds "
                                    f"from {min_dur} to {max_dur}."
                                ),
                                source_url=source_url,
                                checked=checked,
                            )
                        )

        # Clip prompt length
        clip_prompt = shot.get("clip_prompt", "")
        clip_prompt_limit = caps.limit("prompt_max_chars")
        if clip_prompt_limit and not clip_prompt_limit.unknown and len(clip_prompt) > clip_prompt_limit.value:
            source_urls = clip_prompt_limit.source_urls(registry)
            source_url, checked = source_urls[0] if source_urls else (None, None)
            
            note = ""
            if "kling" in clip_model.lower():
                note = " (Higgsfield silently cuts the rest)"
            
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame="clip",
                    model=clip_model,
                    field="prompt_max_chars",
                    limit=clip_prompt_limit.value,
                    actual=len(clip_prompt),
                    severity="error",
                    message=(
                        f"Shot {shot_id} prompt is {len(clip_prompt)} characters; "
                        f"{clip_model} accepts {clip_prompt_limit.value}{note}. "
                        f"Shorten it by {len(clip_prompt) - clip_prompt_limit.value}."
                    ),
                    source_url=source_url,
                    checked=checked,
                )
            )

        # Start frame requirement
        start_frame_limit = caps.limit("start_frame")
        if start_frame_limit and start_frame_limit.value == "required":
            # Check if start frame exists (in practice this would check the file)
            if not shot.get("start_frame"):
                violations.append(
                    Violation(
                        shot_id=shot_id,
                        frame="clip",
                        model=clip_model,
                        field="start_frame",
                        limit="required",
                        actual="missing",
                        severity="error",
                        message=f"Shot {shot_id} clip: {clip_model} requires a start frame.",
                    )
                )

        # End frame handling
        end_frame_limit = caps.limit("end_frame")
        has_start = shot.get("start_frame") is not None
        has_end = shot.get("end_frame") is not None
        
        if end_frame_limit and end_frame_limit.value == "unsupported" and has_end:
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame="clip",
                    model=clip_model,
                    field="end_frame",
                    limit="unsupported",
                    actual="present",
                    severity="error",
                    message=f"Shot {shot_id} clip: {clip_model} does not support end frames.",
                )
            )

        # Last-frame-only not supported
        if has_end and not has_start:
            violations.append(
                Violation(
                    shot_id=shot_id,
                    frame="clip",
                    model=clip_model,
                    field="frames",
                    limit="start required with end",
                    actual="end only",
                    severity="error",
                    message=f"Shot {shot_id} clip: {clip_model} does not support last-frame-only. Provide a start frame.",
                )
            )

        # Frame format check (if files available)
        if files:
            frame_formats_limit = caps.limit("frame_formats")
            if frame_formats_limit and not frame_formats_limit.unknown:
                allowed_formats = frame_formats_limit.value
                for frame_key in ["start_frame", "end_frame"]:
                    if frame_key in shot and shot[frame_key] in files:
                        frame_path = files[shot[frame_key]]
                        # Detect format from file
                        frame_file = Path(frame_path)
                        if frame_file.exists():
                            # Simple format detection by extension or magic bytes
                            suffix = frame_file.suffix.lower()
                            format_map = {
                                ".jpg": "image/jpeg",
                                ".jpeg": "image/jpeg",
                                ".png": "image/png",
                                ".webp": "image/webp",
                                ".gif": "image/gif",
                            }
                            detected_format = format_map.get(suffix)
                            if detected_format and detected_format not in allowed_formats:
                                source_urls = frame_formats_limit.source_urls(registry)
                                source_url, checked = source_urls[0] if source_urls else (None, None)
                                
                                violations.append(
                                    Violation(
                                        shot_id=shot_id,
                                        frame="clip",
                                        model=clip_model,
                                        field="frame_format",
                                        limit=allowed_formats,
                                        actual=detected_format,
                                        severity="error",
                                        message=(
                                            f"Shot {shot_id} clip {frame_key}: format {detected_format} not supported by {clip_model}. "
                                            f"Supported: {', '.join(allowed_formats)}."
                                        ),
                                        source_url=source_url,
                                        checked=checked,
                                    )
                                )

            # Frame size and dimension checks
            frame_min_px_limit = caps.limit("frame_min_px")
            if frame_min_px_limit and not frame_min_px_limit.unknown:
                for frame_key in ["start_frame", "end_frame"]:
                    if frame_key in shot and shot[frame_key] in files:
                        frame_path = files[shot[frame_key]]
                        frame_file = Path(frame_path)
                        if frame_file.exists():
                            try:
                                from PIL import Image
                                with Image.open(frame_file) as img:
                                    width, height = img.size
                                    min_dim = min(width, height)
                                    if min_dim < frame_min_px_limit.value:
                                        source_urls = frame_min_px_limit.source_urls(registry)
                                        source_url, checked = source_urls[0] if source_urls else (None, None)
                                        
                                        violations.append(
                                            Violation(
                                                shot_id=shot_id,
                                                frame="clip",
                                                model=clip_model,
                                                field="frame_min_px",
                                                limit=frame_min_px_limit.value,
                                                actual=min_dim,
                                                severity="error",
                                                message=(
                                                    f"Shot {shot_id} clip {frame_key}: minimum dimension {min_dim} px is below "
                                                    f"{clip_model} requirement of {frame_min_px_limit.value} px."
                                                ),
                                                source_url=source_url,
                                                checked=checked,
                                            )
                                        )

                                    # Aspect ratio check
                                    frame_aspect_limit = caps.limit("frame_aspect_range")
                                    if frame_aspect_limit and not frame_aspect_limit.unknown:
                                        aspect_ratio = width / height
                                        min_aspect, max_aspect = frame_aspect_limit.value
                                        if aspect_ratio < min_aspect or aspect_ratio > max_aspect:
                                            source_urls = frame_aspect_limit.source_urls(registry)
                                            source_url, checked = source_urls[0] if source_urls else (None, None)
                                            
                                            violations.append(
                                                Violation(
                                                    shot_id=shot_id,
                                                    frame="clip",
                                                    model=clip_model,
                                                    field="frame_aspect_range",
                                                    limit=f"{min_aspect:.2f}-{max_aspect:.2f}",
                                                    actual=f"{aspect_ratio:.2f}",
                                                    severity="error",
                                                    message=(
                                                        f"Shot {shot_id} clip {frame_key}: aspect ratio {aspect_ratio:.2f} (1:{1/aspect_ratio:.1f} or {aspect_ratio:.1f}:1) "
                                                        f"is outside {clip_model} range {min_aspect:.2f}-{max_aspect:.2f}."
                                                    ),
                                                    source_url=source_url,
                                                    checked=checked,
                                                )
                                            )
                            except Exception:
                                pass  # Can't check dimensions without PIL or if file is invalid

    return violations


def preflight_episode(
    shots: list[dict[str, Any]],
    routing: dict[str, dict[str, Any]],
    registry: model_caps.ModelCapsRegistry,
    *,
    files: dict[str, str] | None = None,
) -> list[Violation]:
    """
    Run preflight checks on all shots in an episode.

    Args:
        shots: list of shot dicts from shots.json
        routing: dict of {shot_id: route_info}
        registry: model capabilities registry
        files: optional dict of {key: local_path} for file-based checks

    Returns:
        list of all violations across all shots
    """
    all_violations = []

    for shot in shots:
        shot_id = shot["id"]
        route = routing.get(shot_id, {})

        # Check start frame
        violations = preflight_shot(shot, route, registry, phase="start", files=files)
        all_violations.extend(violations)

        # Check end frame if present
        if shot.get("end_prompt") or shot.get("end_frame"):
            violations = preflight_shot(shot, route, registry, phase="end", files=files)
            all_violations.extend(violations)

        # Check clip
        if shot.get("duration") and shot.get("duration") > 0:
            violations = preflight_shot(shot, route, registry, phase="clip", files=files)
            all_violations.extend(violations)

        # Check audio (if implemented)
        # violations = preflight_shot(shot, route, registry, phase="audio", files=files)
        # all_violations.extend(violations)

    return all_violations
