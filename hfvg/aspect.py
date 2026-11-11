"""Output aspect ratios: the canvas each one renders at, and the per-aspect text defaults.

A card's aspect (hfvg/data/genre_presets.yaml) is the frame its shots are generated in and the canvas
the edit, QC and delivery render at. ``16:9_rotated_to_9:16`` cards are generated and delivered
9:16 (no rotation). Pure (no I/O): safe to import in workflow code.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

PORTRAIT = "9:16"

# aspect -> edit canvas (width, height); every side even (yuv420p)
CANVAS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "4:3": (1440, 1080),
    "3:4": (1080, 1440),
    "1:1": (1080, 1080),
}

# Generated and delivered as 9:16 (the card is framed for a rotated phone, see module doc).
GENERATION_ASPECT = {"16:9_rotated_to_9:16": PORTRAIT}

# Burned subtitles, ASS script units (PlayResX x PlayResY = the canvas). The 9:16 values are
# audio_mix.DEFAULT_SUBTITLES; landscape frames get a smaller font, a lower margin and longer lines.
SUBTITLE_STYLE: dict[str, dict[str, int]] = {
    "9:16": {"fontsize": 64, "margin_v": 260, "max_line_chars": 24},
    "3:4": {"fontsize": 60, "margin_v": 150, "max_line_chars": 28},
    "1:1": {"fontsize": 56, "margin_v": 90, "max_line_chars": 30},
    "4:3": {"fontsize": 54, "margin_v": 70, "max_line_chars": 36},
    "16:9": {"fontsize": 54, "margin_v": 64, "max_line_chars": 42},
}


class AspectError(ValueError):
    pass


def generation_aspect(aspect: str | None) -> str:
    """The aspect a card's stills and clips are generated in (and the edit canvas)."""
    if not aspect:
        return PORTRAIT
    aspect = GENERATION_ASPECT.get(aspect, aspect)
    if aspect not in CANVAS:
        raise AspectError(f"unsupported aspect ratio {aspect!r} (supported: {', '.join(CANVAS)})")
    return aspect


def canvas(aspect: str | None) -> tuple[int, int]:
    return CANVAS[generation_aspect(aspect)]


def episode_aspect(shot_aspects: Iterable[str | None]) -> str:
    """One canvas per episode: the aspect most shots are generated in (ties: first seen; none: 9:16).
    Aspects without a canvas are ignored here (preflight refuses them before anything is generated)."""
    seen = [GENERATION_ASPECT.get(a, a) for a in shot_aspects if a]
    seen = [a for a in seen if a in CANVAS]
    if not seen:
        return PORTRAIT
    counts = Counter(seen)
    best = max(counts.values())
    return next(a for a in seen if counts[a] == best)


def aspect_of_size(width: int, height: int, tol: float = 0.01) -> str | None:
    """The supported aspect whose canvas has this shape (None when none is within tol)."""
    if not width or not height:
        return None
    r = width / height
    for name, (w, h) in CANVAS.items():
        if abs(r - w / h) <= tol * (w / h):
            return name
    return None


def subtitle_style(width: int, height: int) -> dict[str, Any]:
    """ASS PlayRes + readable defaults for a picture of this size (closest supported aspect)."""
    name = aspect_of_size(width, height, tol=0.05) or (PORTRAIT if height >= width else "16:9")
    w, h = CANVAS[name]
    return {"aspect": name, "play_res": (w, h), **SUBTITLE_STYLE[name]}
