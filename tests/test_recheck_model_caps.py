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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
