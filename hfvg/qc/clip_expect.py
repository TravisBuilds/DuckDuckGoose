"""Expected clip output dimensions, derived BEFORE any paid clip submit (G-QC-1).

Motion QC compares the delivered clip against an expected ``WxH`` and aspect ratio. Those used to
default to a hard-coded 1024x576 / 16:9 landscape (wrong for every 9:16 shot), and a later attempt
raised only *after* the paid clip had been generated. This module derives the expectation from the
shot params plus the documented routing, so the workflow can fail closed at preflight, before the
clip is submitted:

- aspect: ``params["clip_aspect_ratio"]`` or ``params["aspect_ratio"]`` or ``"9:16"`` (the same
  default the still path uses, so the still and the clip agree).
- resolution: an explicit ``params["clip_resolution"]`` ("WxH"), else a legacy ``params["resolution"]``
  when it is "WxH" (still tiers such as "1k"/"2k" are still settings and are ignored), else the
  short side of the routed model's native output (Kling 3.0 Pro: 1080p; Seedance: 480p draft /
  1080p final, see ``hfvg.routing``).

Anything that cannot be resolved (unknown model, malformed aspect, an explicit WxH that contradicts
the aspect) raises ``ClipSpecError``: never a silent default.
"""

from __future__ import annotations

import re

from hfvg.routing import MODELS, SEEDANCE_TIER_RESOLUTION

KLING_NATIVE_SHORT_SIDE = 1080
DEFAULT_CLIP_ASPECT = "9:16"
_ASPECT_RE = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")
_WXH_RE = re.compile(r"^\s*(\d+)\s*x\s*(\d+)\s*$", re.IGNORECASE)
_P_RE = re.compile(r"^\s*(\d+)p\s*$", re.IGNORECASE)


class ClipSpecError(ValueError):
    """The expected clip dimensions cannot be determined (fail closed, before any paid submit)."""


def _parse_aspect(aspect: str) -> tuple[int, int]:
    m = _ASPECT_RE.match(str(aspect))
    if not m or int(m.group(1)) <= 0 or int(m.group(2)) <= 0:
        raise ClipSpecError(f"clip aspect_ratio must look like '9:16', got {aspect!r}")
    return int(m.group(1)), int(m.group(2))


def _even(x: float) -> int:
    n = int(round(x))
    return n + (n % 2)


def native_short_side(model: str, tier: str | None = None, resolution: str | None = None) -> int:
    """Short side (px) of the routed model's output. ``tier`` None = the submit default (final);
    ``resolution`` = the shot's Seedance override (``seedance_resolution``)."""
    if model == "kling":
        return KLING_NATIVE_SHORT_SIDE
    if model == "seedance":
        from hfvg.routing import RoutingError, seedance_resolution_for
        try:
            res = seedance_resolution_for(tier, resolution)
        except RoutingError as e:
            raise ClipSpecError(str(e)) from None
        return int(_P_RE.match(res).group(1))
    raise ClipSpecError(f"cannot derive clip resolution: unknown clip model {model!r} (expected one of {MODELS})")


def expected_clip_dims(params: dict | None, tier: str | None = None) -> dict:
    """Return ``{"resolution": "WxH", "aspect_ratio": "W:H", "source": ...}`` or raise ClipSpecError."""
    params = params or {}
    aspect = params.get("clip_aspect_ratio") or params.get("aspect_ratio") or DEFAULT_CLIP_ASPECT
    aw, ah = _parse_aspect(aspect)

    explicit = params.get("clip_resolution")
    if not explicit and _WXH_RE.match(str(params.get("resolution") or "")):
        explicit = params.get("resolution")
    if explicit:
        m = _WXH_RE.match(str(explicit))
        if not m:
            raise ClipSpecError(f"clip_resolution must look like '1080x1920', got {explicit!r}")
        w, h = int(m.group(1)), int(m.group(2))
        if w <= 0 or h <= 0:
            raise ClipSpecError(f"clip_resolution must be positive, got {explicit!r}")
        if abs(w / h - aw / ah) > 0.01:
            raise ClipSpecError(
                f"clip_resolution {w}x{h} contradicts aspect_ratio {aspect} ({w / h:.3f} vs {aw / ah:.3f})"
            )
        return {"resolution": f"{w}x{h}", "aspect_ratio": f"{aw}:{ah}", "source": "explicit"}

    short = native_short_side(params.get("model", "kling"), tier, params.get("seedance_resolution"))
    if aw <= ah:
        w, h = short, _even(short * ah / aw)
    else:
        w, h = _even(short * aw / ah), short
    return {"resolution": f"{w}x{h}", "aspect_ratio": f"{aw}:{ah}", "source": "derived"}
