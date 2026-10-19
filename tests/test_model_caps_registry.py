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
    """T3: Unknown models raise UnknownModelError with no fallback (registry and max_refs_for)."""
    from tests.lint_helpers import capture
    registry = model_caps.get_registry()
    err = capture(model_caps.UnknownModelError, registry.resolve, "made/up-model")
    assert err is not None, "resolve() must refuse an unknown model, not fall back"
    assert "made/up-model" in str(err) and "Known models" in str(err), err
    err = capture(model_caps.UnknownModelError, max_refs_for, "made/up-model")
    assert err is not None, "max_refs_for() must refuse an unknown model (no fallback cap)"
    # aliases resolve to their model
    assert registry.resolve("grok") is registry.models["xai/grok-imagine-image-2.0"]
    assert registry.resolve("kling_3.0") is registry.models["kling-video/v3.0/pro/image-to-video"]


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
    """T5: MODEL_MAX_REFS_JSON can lower a cap; raising is ignored with a warning unless ALLOW_RAISE=1."""
    import warnings as _w
    grok = "xai/grok-imagine-image-2.0"
    monkeypatch.delenv("MODEL_CAPS_ALLOW_RAISE", raising=False)
    for asked, expected, warns in ((4, 4, False), (5, 5, False), (6, 5, True)):
        monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({grok: asked}))
        with _w.catch_warnings(record=True) as caught:
            _w.simplefilter("always")
            got = max_refs_for(grok)
        assert got == expected, f"override {asked}: got {got}, expected {expected}"
        raised = [str(w.message) for w in caught if "MODEL_CAPS_ALLOW_RAISE" in str(w.message)]
        assert bool(raised) == warns, f"override {asked}: warnings {raised}"
    monkeypatch.setenv("MODEL_CAPS_ALLOW_RAISE", "1")
    assert max_refs_for(grok) == 6
    assert model_caps.get_registry().resolve(grok).limit("max_refs").value == 5   # registry untouched


def _load_with(monkeypatch, tmp_path, name, **changes):
    import warnings as _w
    seed = json.loads((Path(model_caps.__file__).parent / "data" / "model_caps.json").read_text())
    path = tmp_path / name
    path.write_text(json.dumps(dict(seed, **changes)))
    monkeypatch.setenv("MODEL_CAPS_PATH", str(path))
    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        reg = model_caps.get_registry()   # reloads because MODEL_CAPS_PATH changed
    return seed, reg, [str(w.message) for w in caught if "last checked" in str(w.message)]


def test_caps_freshness_warns(monkeypatch, tmp_path):
    """T6: a registry checked more than recheck_interval_days ago warns on load; a fresh one does not;
    clearing MODEL_CAPS_PATH goes back to the packaged seed (no cache hacks needed)."""
    today = datetime.now(timezone.utc)
    seed, reg, warned = _load_with(monkeypatch, tmp_path, "stale.json",
                                   checked=(today - timedelta(days=35)).strftime("%Y-%m-%d"))
    assert seed["recheck_interval_days"] == 30
    assert reg.stale() and warned, f"35-day-old registry: stale={reg.stale()} warnings={warned}"
    _, reg, warned = _load_with(monkeypatch, tmp_path, "fresh.json",
                                checked=(today - timedelta(days=1)).strftime("%Y-%m-%d"))
    assert not reg.stale() and not warned, warned
    monkeypatch.delenv("MODEL_CAPS_PATH")
    assert model_caps.get_registry().checked == seed["checked"]


# Keys in cost tables that are not model ids. Each needs a reason; anything else must resolve.
NOT_MODEL_IDS = {
    "higgsfield_still": "generic rate-table key of the still provider (the model is still_model_path())",
    "upscale_image": "legacy HiggsfieldProvider cost-table operation; no submit path in the harness",
    "upscale_video": "legacy HiggsfieldProvider cost-table operation; no submit path in the harness",
}


def _configured_model_ids():
    from hfvg.config import config, still_model_path
    from hfvg.credit_pricing import CLIP_CREDITS_PER_SECOND, REF_CREDITS, STILL_CREDITS
    from hfvg.preflight import DEFAULT_CLIP_PATHS, clip_model_path
    from hfvg.providers.elevenlabs import AUDIO_COSTS
    from hfvg.providers.higgsfield import MODEL_COSTS

    ids = {still_model_path(), config.MODEL_PATH_SOUL_V2, config.MODEL_PATH_SEEDANCE, config.MODEL_PATH_KLING}
    ids |= {clip_model_path(alias) for alias in DEFAULT_CLIP_PATHS}   # params.model routing aliases
    ids |= set(AUDIO_COSTS) | set(STILL_CREDITS) | set(CLIP_CREDITS_PER_SECOND) | set(REF_CREDITS)
    ids |= {key[0] for key in MODEL_COSTS}
    return {m for m in ids if m} - set(NOT_MODEL_IDS)


def test_every_configured_model_has_caps():
    """T2: every model id/alias the harness configures, prices or routes resolves in the registry."""
    registry = model_caps.get_registry()
    ids = _configured_model_ids()
    # the collector must actually see every kind of model the harness uses (catches a dropped source)
    kinds = {registry.resolve(m).kind for m in ids if m in registry.models or any(m in c.aliases for c in registry.models.values())}
    assert {"image", "video", "tts", "sfx", "music"} <= kinds, f"collector missed a model kind: {sorted(kinds)}"
    assert {"eleven_multilingual_v2", "sound_generation", "eleven_music", "kling_3.0", "seedance_2.5",
            "soul_v2", "gpt_image_2", "xai/grok-imagine-image-2.0"} <= ids, sorted(ids)
    failed = []
    for model in sorted(ids):
        try:
            registry.resolve(model)
        except model_caps.UnknownModelError as e:
            failed.append(f"{model}: {e}")
    assert not failed, "Models without a registry entry:\n" + "\n".join(failed)


def test_unconfigured_model_fails_the_coverage_check(monkeypatch):
    """The coverage check is live: a newly priced model without an entry is reported."""
    from hfvg.providers import elevenlabs
    monkeypatch.setitem(elevenlabs.AUDIO_COSTS, "eleven_v9_unreleased", 1.0)
    registry = model_caps.get_registry()
    assert "eleven_v9_unreleased" in _configured_model_ids()
    with pytest.raises(model_caps.UnknownModelError):
        registry.resolve("eleven_v9_unreleased")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
