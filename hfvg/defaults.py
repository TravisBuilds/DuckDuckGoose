"""Every request default the harness applies when a shot / call does not set the field (B6).

Each default is declared here once, with the model it is sent to, and validated against the model
capabilities registry (hfvg.model_caps) by ``validate_code_defaults`` (run by the test suite and
by ``hfvg lint``-style checks). A default the registry does not allow for its model is a bug: the
provider would reject it (or silently remap it) at the first paid call.

Literal defaults in hfvg/activities, hfvg/workflows and hfvg/cli.py for the fields below must be
one of the declared values (tests/test_code_defaults_caps.py scans the source).
"""

from __future__ import annotations

from typing import Any

# Stills (start / end frames): sent to the still model (config.still_model_path()).
STILL_RESOLUTION = "1k"
STILL_QUALITY = "medium"
ASPECT_RATIO = "9:16"

# Reference sheets: generated with SOUL V2 (hfvg.activities.ref_generation).
REF_MODEL_PATH = "higgsfield-ai/soul/v2/standard"
REF_RESOLUTION = "1080p"

# Clips: default duration when a plan has none (Kling and Seedance both accept it).
CLIP_DURATION_S = 5
# Seedance output resolution per tier: hfvg.routing.SEEDANCE_TIER_RESOLUTION (single source, used by
# the submit route, the G1 estimate and the dry rate table); validated here too.

# field name in source -> model_caps limit name
FIELD_LIMITS = {
    "resolution": "resolutions",
    "quality": "quality",
    "aspect_ratio": "aspect_ratios",
    "duration": "duration_s",
}


def code_defaults() -> list[dict[str, Any]]:
    """[{"name", "field", "value", "model"}] for every declared default and the model it goes to."""
    from hfvg.config import still_model_path
    from hfvg.preflight import DEFAULT_CLIP_PATHS, clip_model_path

    still = still_model_path()
    rows = [
        {"name": "STILL_RESOLUTION", "field": "resolution", "value": STILL_RESOLUTION, "model": still},
        {"name": "STILL_QUALITY", "field": "quality", "value": STILL_QUALITY, "model": still},
        {"name": "ASPECT_RATIO(still)", "field": "aspect_ratio", "value": ASPECT_RATIO, "model": still},
        {"name": "REF_RESOLUTION", "field": "resolution", "value": REF_RESOLUTION, "model": REF_MODEL_PATH},
        {"name": "ASPECT_RATIO(ref)", "field": "aspect_ratio", "value": ASPECT_RATIO, "model": REF_MODEL_PATH},
    ]
    from hfvg.routing import SEEDANCE_TIER_RESOLUTION
    for tier, res in SEEDANCE_TIER_RESOLUTION.items():
        rows.append({"name": f"SEEDANCE_TIER_RESOLUTION[{tier}]", "field": "resolution", "value": res,
                     "model": clip_model_path("seedance")})
    for alias in DEFAULT_CLIP_PATHS:
        rows.append({"name": f"CLIP_DURATION_S({alias})", "field": "duration", "value": CLIP_DURATION_S,
                     "model": clip_model_path(alias)})
        rows.append({"name": f"ASPECT_RATIO(clip {alias})", "field": "aspect_ratio", "value": ASPECT_RATIO,
                     "model": clip_model_path(alias)})
    return rows


def check_value(caps: Any, field: str, value: Any) -> str | None:
    """Why ``value`` is not allowed for ``field`` on these caps (None = allowed / not constrained)."""
    limit = caps.limit(FIELD_LIMITS[field])
    if limit is None or limit.value is None:
        return None if field != "duration" else f"model has no {FIELD_LIMITS[field]} limit"
    allowed = limit.value
    if field == "duration":
        lo, hi = allowed.get("min"), allowed.get("max")
        v = float(value)
        if lo is not None and v < lo or hi is not None and v > hi:
            return f"{value} s outside [{lo}, {hi}]"
        if allowed.get("integer") and v != int(v):
            return f"{value} s is not a whole number of seconds"
        return None
    if field == "aspect_ratio" and allowed == ["follows_start_frame"]:
        return None  # the clip follows its start frame's aspect: nothing to send
    if value not in allowed:
        return f"{value!r} not in {allowed}"
    return None


def validate_code_defaults(registry: Any = None) -> list[str]:
    """Every declared default checked against model_caps; returns the problems (empty = ok)."""
    from hfvg import model_caps

    registry = registry or model_caps.get_registry()
    problems = []
    for row in code_defaults():
        try:
            caps = registry.resolve(row["model"])
        except model_caps.UnknownModelError as e:
            problems.append(f"{row['name']}: model {row['model']} unknown to model_caps ({e})")
            continue
        why = check_value(caps, row["field"], row["value"])
        if why:
            problems.append(f"{row['name']} = {row['value']!r} for {row['model']}: {why}")
    return problems


def declared_values(field: str) -> set[Any]:
    """The values declared for one field (for the source scan)."""
    vals = {r["value"] for r in code_defaults() if r["field"] == field}
    if field == "duration":
        vals |= {float(v) for v in vals}
    return vals
