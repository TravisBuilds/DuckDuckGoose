"""Shared request plumbing for Higgsfield job requests (stills, Kling, Seedance)."""

import os
from typing import Any


def declined_preset_id() -> str | None:
    """Configured ``declined_preset_id`` (env HIGGSFIELD_DECLINED_PRESET_ID), or None when unset.

    Read at request time so it can be changed without re-importing. Default: unset.
    """
    value = os.getenv("HIGGSFIELD_DECLINED_PRESET_ID", "").strip()
    return value or None


def apply_declined_preset(payload: dict[str, Any]) -> dict[str, Any]:
    """Add ``declined_preset_id`` to a JOB request payload when configured (every model)."""
    preset = declined_preset_id()
    if preset:
        payload["declined_preset_id"] = preset
    return payload
