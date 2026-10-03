"""Tests for asset registry."""

import json
import tempfile
from pathlib import Path

import pytest

from hfvg.registry import AssetRegistry, CharacterLock


@pytest.fixture
def temp_series_dir():
    """Create a temporary series directory structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        series_path = Path(tmpdir)
        
        # Create CHARACTER-LOCK.md
        lock_content = """# Quacked Concierge Peak Glow Up

**Locked 2026-09-20 (v3 short-bill remake)**

## Identity
- Compact pear-shaped body
- SHORT broad classic orange duck bill
- Iridescent bottle-green mallard head

## Canonical refs
- Apparel ABs: `refs/apparel/concierge-mallard-tuque-v3/AB02.png`
- Anatomy source: Ember Ep2
"""
        (series_path / "CHARACTER-LOCK.md").write_text(lock_content)
        
        # Create apparel refs structure
        apparel_dir = series_path / "refs" / "apparel"
        
        # Active lock (v3)
        v3_dir = apparel_dir / "concierge-mallard-tuque-v3"
        v3_dir.mkdir(parents=True)
        v3_media = {
            "media_ids": ["img_abc123", "img_def456"],
            "description": "v3 short bill"
        }
        (v3_dir / "media-ids.json").write_text(json.dumps(v3_media))
        
        # Superseded lock (v2)
        v2_dir = apparel_dir / "concierge-mallard-tuque-v2"
        v2_dir.mkdir(parents=True)
        v2_media = {
            "media_ids": ["img_old123"],
            "description": "v2 bill too long - superseded"
        }
        (v2_dir / "media-ids.json").write_text(json.dumps(v2_media))
        
        # World refs
        world_dir = series_path / "refs" / "world"
        world_dir.mkdir(parents=True)
        world_data = {
            "ENV01": "img_resort_lobby",
            "ENV02": "img_resort_pool"
        }
        (world_dir / "media-ids.json").write_text(json.dumps(world_data))
        
        yield series_path


def test_load_character_locks(temp_series_dir):
    """Test loading character locks from CHARACTER-LOCK.md."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    assert "duck" in registry.characters
    char = registry.characters["duck"]
    assert isinstance(char, CharacterLock)
    assert char.name == "Quacked Concierge (Mallard)"
    assert char.locked_date == "2026-09-20"


def test_load_apparel_locks(temp_series_dir):
    """Test loading apparel locks."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    # Should load both v2 and v3
    assert "concierge-mallard-tuque-v3" in registry.locks
    assert "concierge-mallard-tuque-v2" in registry.locks
    
    # v3 should not be superseded
    v3_lock = registry.locks["concierge-mallard-tuque-v3"]
    assert not v3_lock.superseded
    assert v3_lock.version == "v3"
    assert len(v3_lock.media_ids) == 2
    
    # v2 should be superseded
    v2_lock = registry.locks["concierge-mallard-tuque-v2"]
    assert v2_lock.superseded


def test_load_world_refs(temp_series_dir):
    """Test loading world references."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    assert "ENV01" in registry.env_plates
    assert registry.env_plates["ENV01"] == "img_resort_lobby"
    
    world_refs = registry.get_world_refs()
    assert len(world_refs) == 2


def test_validate_shot_references_superseded(temp_series_dir):
    """Test validation rejects superseded locks."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    # Shot referencing superseded v2
    shot = {
        "shot_id": "A01",
        "refs": [
            "refs/apparel/concierge-mallard-tuque-v2/AB02.png"
        ]
    }
    
    errors = registry.validate_shot_references(shot)
    assert len(errors) == 1
    assert "superseded" in errors[0].lower()
    assert "v2" in errors[0]


def test_validate_shot_references_valid(temp_series_dir):
    """Test validation passes for active locks."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    # Shot referencing active v3
    shot = {
        "shot_id": "A01",
        "refs": [
            "refs/apparel/concierge-mallard-tuque-v3/AB02.png"
        ]
    }
    
    errors = registry.validate_shot_references(shot)
    assert len(errors) == 0


def test_get_active_locks(temp_series_dir):
    """Test getting only active locks."""
    registry = AssetRegistry(temp_series_dir)
    registry.load()
    
    active = registry.get_active_locks()
    assert len(active) == 1
    assert active[0].lock_id == "concierge-mallard-tuque-v3"
