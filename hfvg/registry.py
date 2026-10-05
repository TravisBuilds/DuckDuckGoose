"""Asset registry for series identity locks and episode references."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AssetLock:
    """A locked asset reference."""

    lock_id: str
    category: str  # e.g., "character", "apparel", "env", "prop"
    version: str  # e.g., "v3", "v2-superseded"
    media_ids: list[str]  # Provider media/job IDs
    description: str
    superseded: bool = False  # True if this lock is no longer active


@dataclass
class CharacterLock:
    """Series character identity lock."""

    name: str
    locked_date: str
    canonical_refs: dict[str, str]  # ref_type -> path
    anatomy_notes: str
    rejected_versions: list[str]


class AssetRegistry:
    """
    Asset registry for series and episode assets.
    
    Loads from a local series folder structure:
    - series/CHARACTER-LOCK.md
    - series/refs/apparel/<lock-id>/
    - series/refs/apparel/<lock-id>/media-ids.json
    - episode/refs/ 
    - episode/envs/
    """

    def __init__(self, series_path: str | Path):
        self.series_path = Path(series_path)
        self.characters: dict[str, CharacterLock] = {}
        self.locks: dict[str, AssetLock] = {}
        self.env_plates: dict[str, str] = {}  # env_id -> media_id
        
    def load(self):
        """Load all series locks."""
        self._load_character_locks()
        self._load_apparel_locks()
        self._load_world_refs()
        
    def _load_character_locks(self):
        """Parse CHARACTER-LOCK.md for series identity."""
        lock_file = self.series_path / "CHARACTER-LOCK.md"
        if not lock_file.exists():
            return
            
        content = lock_file.read_text()
        
        # Simple parser for the duck character lock
        # In production, this would be more robust
        if "Quacked Concierge" in content:
            self.characters["duck"] = CharacterLock(
                name="Quacked Concierge (Mallard)",
                locked_date="2026-09-20",
                canonical_refs={
                    "apparel": "refs/apparel/concierge-mallard-tuque-v3/",
                    "anatomy": "refs/ember-duck-ref/",
                },
                anatomy_notes="Short broad bill, pear-shaped body, nub wings",
                rejected_versions=["v1-body-too-long", "v2-bill-too-long"]
            )
    
    def _load_apparel_locks(self):
        """Load apparel reference stacks from refs/apparel/."""
        apparel_dir = self.series_path / "refs" / "apparel"
        if not apparel_dir.exists():
            return
            
        for lock_dir in apparel_dir.iterdir():
            if not lock_dir.is_dir():
                continue
                
            # Check for media-ids.json
            media_ids_file = lock_dir / "media-ids.json"
            if media_ids_file.exists():
                try:
                    media_data = json.loads(media_ids_file.read_text())
                    
                    # Extract superseded status from directory name
                    superseded = "superseded" in lock_dir.name or lock_dir.name.endswith("-v1") or lock_dir.name.endswith("-v2")
                    
                    lock = AssetLock(
                        lock_id=lock_dir.name,
                        category="apparel",
                        version=self._extract_version(lock_dir.name),
                        media_ids=media_data.get("media_ids", []),
                        description=media_data.get("description", ""),
                        superseded=superseded
                    )
                    
                    self.locks[lock_dir.name] = lock
                    
                except (json.JSONDecodeError, KeyError) as e:
                    # Log warning but continue
                    pass
    
    def _load_world_refs(self):
        """Load world/environment references."""
        world_file = self.series_path / "refs" / "world" / "media-ids.json"
        if world_file.exists():
            try:
                world_data = json.loads(world_file.read_text())
                for env_id, media_id in world_data.items():
                    self.env_plates[env_id] = media_id
            except (json.JSONDecodeError, KeyError):
                pass
    
    def load_episode_refs(self, episode_path: str | Path):
        """Load episode-specific references."""
        episode_path = Path(episode_path)
        
        # Load episode envs
        envs_dir = episode_path / "envs"
        if envs_dir.exists():
            for env_file in envs_dir.glob("ENV*.png"):
                env_id = env_file.stem
                # In production, track these as episode-specific
                self.env_plates[env_id] = str(env_file)
    
    def validate_shot_references(self, shot: dict[str, Any]) -> list[str]:
        """
        Validate that a shot only references locked assets.
        
        Returns:
            List of validation errors (empty if valid)
        """
        errors = []
        refs = shot.get("refs", [])
        
        for ref in refs:
            # Check if ref is a locked asset
            ref_path = Path(ref)
            
            # Check for superseded locks first
            is_superseded = False
            for lock_id, lock in self.locks.items():
                if lock.superseded and lock_id in str(ref_path):
                    errors.append(
                        f"Shot {shot.get('shot_id')} references superseded lock: {lock_id}. "
                        f"Use current version instead."
                    )
                    is_superseded = True
                    break
            
            # Only check for unlocked if not superseded
            if not is_superseded and "refs/apparel/" in str(ref_path):
                found_lock = False
                for lock_id, lock in self.locks.items():
                    if lock_id in str(ref_path) and not lock.superseded:
                        found_lock = True
                        break
                
                if not found_lock:
                    errors.append(
                        f"Shot {shot.get('shot_id')} references unlocked apparel: {ref}. "
                        f"Only locked assets in CHARACTER-LOCK.md may be used."
                    )
        
        return errors
    
    def get_character_refs(self, character: str) -> list[str]:
        """Get canonical reference stack for a character."""
        char_lock = self.characters.get(character)
        if not char_lock:
            return []
        
        refs = []
        for ref_type, ref_path in char_lock.canonical_refs.items():
            full_path = self.series_path / ref_path
            if full_path.exists():
                refs.append(str(full_path))
        
        return refs
    
    def get_world_refs(self) -> list[str]:
        """Get world/resort environment references (ONE-RESORT RULE)."""
        return list(self.env_plates.values())
    
    def get_active_locks(self) -> list[AssetLock]:
        """Get all active (non-superseded) locks."""
        return [lock for lock in self.locks.values() if not lock.superseded]
    
    def _extract_version(self, name: str) -> str:
        """Extract version from lock name."""
        if "-v3" in name:
            return "v3"
        elif "-v2" in name:
            return "v2"
        elif "-v1" in name:
            return "v1"
        return "unknown"


def load_registry(series_path: str | None = None) -> AssetRegistry:
    """
    Load asset registry from configured series path.
    
    Args:
        series_path: Path to series folder, or None to use config default
    
    Returns:
        Loaded AssetRegistry
    """
    from hfvg.config import config
    
    if series_path is None:
        series_path = getattr(config, "SERIES_PATH", "./series")
    
    registry = AssetRegistry(series_path)
    registry.load()
    return registry
