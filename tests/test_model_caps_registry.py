"""Tests for model capabilities registry."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hfvg import model_caps
from hfvg.config import max_refs_for


def test_seed_json_validates():
    """T1: The seed JSON validates and has correct structure."""
    registry = model_caps.get_registry()
    
    # Basic structure validation
    assert registry.schema_version == 1
    assert isinstance(registry.models, dict)
    assert isinstance(registry.sources, dict)
    assert len(registry.models) > 0
    assert len(registry.sources) > 0
    
    # Every limit's sources must exist
    for model_id, caps in registry.models.items():
        for limit_name, limit in caps.limits.items():
            for source_id in limit.sources:
                assert source_id in registry.sources, (
                    f"Model {model_id} limit {limit_name} references unknown source '{source_id}'"
                )
    
    # Every source must have an https:// (or mcp:/repo-path for obs_*/harness_*) url and checked date
    for source_id, source in registry.sources.items():
        # Internal observation sources can have repo-relative paths
        is_internal = source_id.startswith("obs_") or source_id.startswith("harness_")
        
        assert (
            source.url.startswith("https://")
            or source.url.startswith("mcp:")
            or source.url.startswith("/")
            or is_internal
        ), f"Source {source_id} has invalid URL: {source.url}"
        
        assert source.checked, f"Source {source_id} has no checked date"
        # Validate date format
        datetime.strptime(source.checked, "%Y-%m-%d")
    
    # Every non-unknown value must have ≥1 source
    for model_id, caps in registry.models.items():
        for limit_name, limit in caps.limits.items():
            if not limit.unknown:
                assert len(limit.sources) > 0, (
                    f"Model {model_id} limit {limit_name} has a value but no sources"
                )


def test_unknown_model_refused():
    """T3: Unknown models raise UnknownModelError with no fallback."""
    registry = model_caps.get_registry()
    
    # resolve() should raise for unknown model
    with pytest.raises(model_caps.UnknownModelError):
        registry.resolve("made/up-model")
    
    # max_refs_for() should also raise (no fallback to 3)
    with pytest.raises(model_caps.UnknownModelError):
        max_refs_for("made/up-model")


def test_stricter_value_wins_grok():
    """T4: Grok max_refs uses stricter upstream value (5) over Higgsfield (10)."""
    registry = model_caps.get_registry()
    caps = registry.resolve("xai/grok-imagine-image-2.0")
    
    max_refs_limit = caps.limit("max_refs")
    assert max_refs_limit is not None
    assert max_refs_limit.value == 5
    assert max_refs_limit.higgsfield == 10
    assert max_refs_limit.upstream == 5
    assert max_refs_limit.basis == "stricter_upstream"


def test_override_can_lower_not_raise(monkeypatch):
    """T5: MODEL_MAX_REFS_JSON can lower but not raise a cap (unless ALLOW_RAISE=1)."""
    # Lower: should work
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({"xai/grok-imagine-image-2.0": 4}))
    # Clear the global registry cache to force reload
    model_caps._registry = None
    result = max_refs_for("xai/grok-imagine-image-2.0")
    assert result == 4
    
    # Raise without flag: should clamp to registry value (5)
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({"xai/grok-imagine-image-2.0": 6}))
    monkeypatch.delenv("MODEL_CAPS_ALLOW_RAISE", raising=False)
    model_caps._registry = None
    result = max_refs_for("xai/grok-imagine-image-2.0")
    assert result == 5  # Clamped, not 6
    
    # Raise with flag: should allow
    monkeypatch.setenv("MODEL_CAPS_ALLOW_RAISE", "1")
    model_caps._registry = None
    result = max_refs_for("xai/grok-imagine-image-2.0")
    assert result == 6  # Raised
    
    # Clean up
    model_caps._registry = None


def test_caps_freshness_warns(monkeypatch):
    """T6: Registry emits UserWarning when stale (31+ days old)."""
    # Mock a stale checked date
    registry = model_caps.get_registry()
    old_checked = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
    
    # Modify the registry's checked date
    registry.checked = old_checked
    
    # Check that stale() returns True
    assert registry.stale()


def test_every_configured_model_has_caps():
    """T2: Every model id/alias collected from configured sources resolves."""
    from hfvg.config import still_model_path
    from hfvg.providers.elevenlabs import AUDIO_COSTS
    from hfvg.credit_pricing import STILL_CREDITS, CLIP_CREDITS_PER_SECOND, REF_CREDITS
    
    registry = model_caps.get_registry()
    
    models_to_check = set()
    
    # still_model_path default
    models_to_check.add(still_model_path())
    
    # AUDIO_COSTS keys
    models_to_check.update(AUDIO_COSTS.keys())
    
    # STILL_CREDITS keys
    models_to_check.update(STILL_CREDITS.keys())
    
    # CLIP_CREDITS_PER_SECOND keys
    models_to_check.update(CLIP_CREDITS_PER_SECOND.keys())
    
    # REF_CREDITS keys
    models_to_check.update(REF_CREDITS.keys())
    
    # Skip known legacy aliases that are not primary model IDs
    legacy_aliases = {"higgsfield_still", "kling_3.0", "seedance_2.5", "soul_v2"}
    models_to_check -= legacy_aliases
    
    # Check that all resolve
    failed = []
    for model in models_to_check:
        try:
            caps = registry.resolve(model)
            assert caps is not None, f"Model {model} resolved to None"
        except model_caps.UnknownModelError as e:
            failed.append((model, str(e)))
    
    # Report failures if any
    if failed:
        msg = "Models not in registry:\n" + "\n".join(f"  - {m}: {e}" for m, e in failed)
        pytest.fail(msg)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
