"""Synthetic prompt-lint fixtures (public repo: no real episode text)."""

import json

SYNTHETIC_LINT_KIT = {
    "count_template": "Exactly {n} animals in frame",
    "realtime_pattern": r"real-time.{0,40}no slow motion",
    "locked_camera_clause": "locked-off tripod, no camera movement",
    "antler_clause": "synthetic antler clause: two full antlers visible",
    "antler_characters": ["Birch"],
    "duck_clause": "synthetic duck clause: one duck on the water",
    "duck_characters": ["Duck"],
    "banned_decor": ["gazebo", "lantern"],
}

REALTIME = "Real-time speed, no slow motion."


def write_kit(path, overrides=None):
    kit = dict(SYNTHETIC_LINT_KIT)
    kit.update(overrides or {})
    path.write_text(json.dumps(kit))
    return path


def lint_ok(text: str = "Synthetic scene", count: int = 1) -> str:
    """A prompt that satisfies the synthetic kit for an interior shot with ``count`` animals."""
    return f"{text}. Exactly {count} animals in frame. {REALTIME}"


def lint_meta(count: int = 1, **kw) -> dict:
    meta = {"count": count, "exterior": False, "water": False, "characters": []}
    meta.update(kw)
    return meta


def capture(exc_type, fn, *args, **kwargs):
    """Call fn; return the exception if exc_type is raised, else None (assert on the result so a
    surviving mutation fails with a real AssertionError, not 'DID NOT RAISE')."""
    try:
        fn(*args, **kwargs)
    except exc_type as e:
        return e
    return None


async def acapture(exc_type, awaitable):
    """Await `awaitable`; return the exception if exc_type is raised, else None."""
    try:
        await awaitable
    except exc_type as e:
        return e
    return None
