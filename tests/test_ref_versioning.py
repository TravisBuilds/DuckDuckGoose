"""Tests for ref versioning and archiving."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_map_form_refs_with_versions_archives_and_regenerates(tmp_path, monkeypatch):
    """
    Test: Map-form refs.json with versions {family:2} archives the v1 file to 
    refs/_v1/ and regenerates, while unversioned refs are reused.
    """
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    refs_dir = tmp_path / "data" / "episodes" / "ep99" / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    
    # Pre-create v1 of "family" ref with sidecar
    family_v1_path = refs_dir / "family.png"
    family_v1_path.write_bytes(b"family-v1-content")
    version_sidecar = refs_dir / ".versions" / "family.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text(json.dumps({"version": 1, "ref_key": "family"}))
    
    # Pre-create unversioned "duck" ref (no sidecar = v1 default)
    duck_path = refs_dir / "duck.png"
    duck_path.write_bytes(b"duck-v1-content")
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Test 1: Generate family v2 (should archive v1 and regenerate)
        result_family = await generate_ref_activity(
            episode_id="ep99",
            ref_key="family",
            prompt="Family portrait v2",
            output_path=str(family_v1_path),
            version=2,
        )
        
        assert result_family["status"] == "completed", f"Expected completed, got {result_family}"
        
        # Check v1 was archived
        family_v1_archive = refs_dir / "_v1" / "family.png"
        assert family_v1_archive.exists(), f"v1 archive not found at {family_v1_archive}"
        assert family_v1_archive.read_bytes() == b"family-v1-content", "v1 content mismatch"
        
        # Check new v2 file exists
        assert family_v1_path.exists(), "v2 file not created"
        
        # Check version sidecar updated to v2
        version_data = json.loads(version_sidecar.read_text())
        assert version_data["version"] == 2, f"Version sidecar should be v2, got {version_data}"
        
        # Test 2: Generate duck v1 (should reuse, no regeneration)
        result_duck = await generate_ref_activity(
            episode_id="ep99",
            ref_key="duck",
            prompt="Duck on water",
            output_path=str(duck_path),
            version=1,  # Matches existing (default v1)
        )
        
        assert result_duck["status"] == "reused", f"Expected reused, got {result_duck}"
        assert result_duck["credits_used"] == 0, "Reused ref should cost 0 credits"
        assert duck_path.read_bytes() == b"duck-v1-content", "Duck content should be unchanged"
        
        # No archive for duck (version matched)
        duck_v1_archive = refs_dir / "_v1" / "duck.png"
        assert not duck_v1_archive.exists(), f"Duck shouldn't be archived at {duck_v1_archive}"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_version_mismatch_archives_old_version(tmp_path, monkeypatch):
    """Test: Version bump archives old file to refs/_v{n}/ directory."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    refs_dir = tmp_path / "data" / "episodes" / "ep99" / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    
    pool_path = refs_dir / "pool.png"
    pool_path.write_bytes(b"pool-v1-content")
    
    # Create v1 sidecar
    version_sidecar = refs_dir / ".versions" / "pool.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text(json.dumps({"version": 1, "ref_key": "pool"}))
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate v3 (skip v2 to test any version jump)
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="pool",
            prompt="Pool v3",
            output_path=str(pool_path),
            version=3,
        )
        
        assert result["status"] == "completed"
        
        # v1 should be in _v1/ directory
        v1_archive = refs_dir / "_v1" / "pool.png"
        assert v1_archive.exists(), f"v1 not archived to {v1_archive}"
        assert v1_archive.read_bytes() == b"pool-v1-content"
        
        # New file should exist
        assert pool_path.exists()
        
        # Sidecar should show v3
        version_data = json.loads(version_sidecar.read_text())
        assert version_data["version"] == 3
        
    finally:
        temporalio.activity.info = original_info
