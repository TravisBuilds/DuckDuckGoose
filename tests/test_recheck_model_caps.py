"""Test recheck_model_caps.py script."""
import pytest
import json
import sys
from pathlib import Path


def test_recheck_detects_schema_drift(tmp_path, monkeypatch):
    """T20: recheck script detects schema drift (new model, removed model, maxItems change)."""
    # Create test registry
    registry = {
        "schema_version": 1,
        "checked": "2026-10-19",
        "recheck_interval_days": 30,
        "models": {
            "xai/grok-imagine-image-2.0": {
                "status": "active_default_still",
                "limits": {
                    "max_refs": {"value": 5, "unknown": False}
                }
            },
            "old/model": {
                "status": "active",
                "limits": {}
            }
        }
    }
    
    # Create test fixture with drift
    fixture = {
        "models": [
            {
                "id": "xai/grok-imagine-image-2.0",
                "schema": {
                    "properties": {
                        "image_urls": {"maxItems": 10}  # Changed from 5
                    }
                }
            },
            {
                "id": "new/model",  # New model not in registry
                "schema": {}
            }
            # old/model removed from fixture
        ]
    }
    
    registry_path = tmp_path / "registry.json"
    fixture_path = tmp_path / "fixture.json"
    
    registry_path.write_text(json.dumps(registry))
    fixture_path.write_text(json.dumps(fixture))
    
    # Import and run recheck
    monkeypatch.chdir(Path(__file__).parent.parent)
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    
    from recheck_model_caps import check_schema_drift, load_registry, load_fixture
    
    loaded_registry = load_registry(str(registry_path))
    loaded_fixture = load_fixture(str(fixture_path))
    
    diffs = check_schema_drift(loaded_registry, loaded_fixture)
    
    # Should detect:
    # 1. New model in fixture
    # 2. Removed active model
    # 3. max_refs drift (5 -> 10)
    assert len(diffs) >= 2, f"Expected at least 2 drifts, got {len(diffs)}: {diffs}"
    
    drift_text = "\n".join(diffs)
    assert "new/model" in drift_text or "New model" in drift_text
    assert "max_refs drift" in drift_text or "maxItems" in drift_text


def test_recheck_passes_when_no_drift(tmp_path, monkeypatch):
    """T21: recheck script passes when registry matches fixture."""
    registry = {
        "schema_version": 1,
        "checked": "2026-10-19",
        "models": {
            "xai/grok-imagine-image-2.0": {
                "status": "active_default_still",
                "limits": {
                    "max_refs": {"value": 5}
                }
            }
        }
    }
    
    fixture = {
        "models": [
            {
                "id": "xai/grok-imagine-image-2.0",
                "schema": {
                    "properties": {
                        "image_urls": {"maxItems": 5}
                    }
                }
            }
        ]
    }
    
    registry_path = tmp_path / "registry.json"
    fixture_path = tmp_path / "fixture.json"
    
    registry_path.write_text(json.dumps(registry))
    fixture_path.write_text(json.dumps(fixture))
    
    monkeypatch.chdir(Path(__file__).parent.parent)
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    
    from recheck_model_caps import check_schema_drift, load_registry, load_fixture
    
    loaded_registry = load_registry(str(registry_path))
    loaded_fixture = load_fixture(str(fixture_path))
    
    diffs = check_schema_drift(loaded_registry, loaded_fixture)
    
    assert len(diffs) == 0, f"Expected no drift, got: {diffs}"


REAL_SNAPSHOT = Path(__file__).parent / "fixtures" / "higgsfield_llms_full_2026-10-19.txt"
REGISTRY = Path(__file__).parent.parent / "hfvg" / "data" / "model_caps.json"


def _recheck():
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import recheck_model_caps
    return recheck_model_caps


def test_real_llms_full_snapshot_parses():
    """The shipped fixture is a real docs.higgsfield.ai/llms-full.txt snapshot, not synthetic JSON."""
    rc = _recheck()
    text = REAL_SNAPSHOT.read_text(encoding="utf-8")
    assert text.startswith("<!-- Snapshot of https://docs.higgsfield.ai/llms-full.txt")
    assert "fetched 2026-10-19" in text.splitlines()[1]
    assert "SYNTHETIC" not in text
    models = {m["id"]: m for m in rc.load_fixture(str(REAL_SNAPSHOT))["models"]}
    assert len(models) == 83
    registry = rc.load_registry(str(REGISTRY))
    hf_ids = {k for k, v in registry["models"].items() if v.get("provider") == "higgsfield"}
    assert hf_ids <= set(models), sorted(hf_ids - set(models))
    grok = models["xai/grok-imagine-image-2.0"]
    assert grok["source"] == "https://docs.higgsfield.ai/docs/models/grok-image-2/generate-and-edit"
    assert grok["schema"]["properties"]["image_urls"]["maxItems"] == 10
    assert models["alibaba/qwen-image-3/edit"]["schema"]["properties"]["image_urls"]["maxItems"] == 3


def test_real_snapshot_drift_matches_expectations():
    """Real drift on 2026-10-19: no drift on tracked models; 77 untracked endpoints;
    grok (5 < 10) and seedance reference-to-video (8 < 30) are deliberately tighter."""
    rc = _recheck()
    registry = rc.load_registry(str(REGISTRY))
    fixture = rc.load_fixture(str(REAL_SNAPSHOT))
    diffs = rc.check_schema_drift(registry, fixture)
    drifts = [d for d in diffs if not rc._is_untracked_new(d)]
    untracked = [d for d in diffs if rc._is_untracked_new(d)]
    assert drifts == []
    assert len(untracked) == 77
    assert not any("eleven" in d or "gpt_image_2" in d or "sound_generation" in d for d in diffs)
    tighter = "\n".join(rc.list_tighter_limits(registry, fixture))
    assert "xai/grok-imagine-image-2.0: max_refs registry=5 < upstream=10" in tighter
    assert "bytedance/seedance-2.5/reference-to-video: max_refs registry=8 < upstream=30" in tighter


def test_tighter_basis_still_drifts_when_upstream_drops_below_registry():
    """A stricter_* basis must not hide an upstream cap that fell below the registry value."""
    rc = _recheck()
    registry = {"models": {"m": {"provider": "higgsfield", "status": "active",
                                 "limits": {"max_refs": {"value": 5, "basis": "stricter_upstream"}}}}}
    lower = {"models": [{"id": "m", "schema": {"properties": {"image_urls": {"maxItems": 4}}}}]}
    looser = {"models": [{"id": "m", "schema": {"properties": {"image_urls": {"maxItems": 10}}}}]}
    assert rc.check_schema_drift(registry, lower) == ["m: max_refs drift - registry=5, fixture=4"]
    assert rc.check_schema_drift(registry, looser) == []


def test_real_snapshot_detects_injected_drift():
    """Lowering a registry value the docs still publish is reported against the real snapshot."""
    rc = _recheck()
    registry = rc.load_registry(str(REGISTRY))
    registry["models"]["alibaba/qwen-image-3/edit"]["limits"]["max_refs"]["value"] = 2
    registry["models"]["kling-video/v3.0/pro/image-to-video"]["limits"]["duration_s"]["value"]["max"] = 10
    diffs = rc.check_schema_drift(registry, rc.load_fixture(str(REAL_SNAPSHOT)))
    drifts = [d for d in diffs if not rc._is_untracked_new(d)]
    assert "alibaba/qwen-image-3/edit: max_refs drift - registry=2, fixture=3" in drifts
    assert "kling-video/v3.0/pro/image-to-video: duration_s.max drift - registry=10, fixture=15" in drifts


def test_recheck_cli_on_real_snapshot_exits_zero():
    """The CLI's default fixture loads (the SYNTHETIC '#' header made it fail with exit 1)."""
    import subprocess
    root = Path(__file__).parent.parent
    r = subprocess.run([sys.executable, "scripts/recheck_model_caps.py"], cwd=root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "77 upstream endpoint(s) not tracked" in r.stdout
    r = subprocess.run([sys.executable, "scripts/recheck_model_caps.py", "--strict"], cwd=root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
