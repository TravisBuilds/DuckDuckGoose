"""Model capabilities registry accessor."""

import json
from pathlib import Path
from typing import Any


_CAPS_CACHE: dict[str, Any] | None = None


def get_model_caps(model_id: str) -> dict[str, Any]:
    """
    Load capabilities for a model from model_caps.json.
    
    Returns: model capabilities dict, or empty dict if not found.
    """
    global _CAPS_CACHE
    
    if _CAPS_CACHE is None:
        caps_path = Path(__file__).parent / "model_caps.json"
        _CAPS_CACHE = json.loads(caps_path.read_text())
    
    return _CAPS_CACHE.get("models", {}).get(model_id, {})


def list_models(kind: str | None = None, provider: str | None = None, status: str | None = None) -> list[str]:
    """
    List model IDs matching the given filters.
    
    Args:
        kind: Filter by model kind (image, video, tts, music, sfx)
        provider: Filter by provider (higgsfield, elevenlabs)
        status: Filter by status (active, deprecated, etc.)
    
    Returns: list of model IDs
    """
    global _CAPS_CACHE
    
    if _CAPS_CACHE is None:
        caps_path = Path(__file__).parent / "model_caps.json"
        _CAPS_CACHE = json.loads(caps_path.read_text())
    
    models = _CAPS_CACHE.get("models", {})
    result = []
    
    for model_id, caps in models.items():
        if kind and caps.get("kind") != kind:
            continue
        if provider and caps.get("provider") != provider:
            continue
        if status and not caps.get("status", "").startswith(status):
            continue
        result.append(model_id)
    
    return sorted(result)
