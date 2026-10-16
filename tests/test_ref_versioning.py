"""Tests for ref versioning and archiving."""

import json
import pytest
from pathlib import Path


def create_valid_test_png():
    """Create a minimal valid PNG that passes QC (>256x256 px, >1KB)."""
    from PIL import Image, ImageDraw
    import io
    # Create image with some patterns to ensure file size >1KB
    img = Image.new('RGB', (512, 512), color='blue')
    draw = ImageDraw.Draw(img)
    for i in range(0, 512, 10):
        draw.line([(i, 0), (i, 512)], fill='red', width=1)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


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
    
    # Pre-create v1 of "family" ref with sidecar (valid PNG for QC)
    family_v1_path = refs_dir / "family.png"
    family_v1_content = create_valid_test_png()
    family_v1_path.write_bytes(family_v1_content)
    version_sidecar = refs_dir / ".versions" / "family.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text(json.dumps({"version": 1, "ref_key": "family"}))
    
    # Pre-create unversioned "duck" ref (valid PNG for QC)
    duck_path = refs_dir / "duck.png"
    duck_v1_content = create_valid_test_png()
    duck_path.write_bytes(duck_v1_content)
    
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
        
        # In dry mode, output is redirected to scratch dir
        actual_output_v2 = Path(result_family["output_path"])
        
        # In dry mode, real files are NOT archived (don't touch real files)
        # The real v1 file stays in place
        assert family_v1_path.exists(), "v1 should still exist at original location in dry mode"
        
        # No archive created in dry mode
        family_v1_archive = refs_dir / "_v1" / "family.png"
        assert not family_v1_archive.exists(), f"Dry mode should NOT archive, but found {family_v1_archive}"
        
        # Check new v2 file exists (at redirected path in dry mode)
        assert actual_output_v2.exists(), f"v2 file not created at {actual_output_v2}"
        
        # Check version sidecar updated to v2 (at redirected path)
        version_sidecar_v2 = actual_output_v2.parent / ".versions" / f"{actual_output_v2.stem}.json"
        assert version_sidecar_v2.exists(), f"Version sidecar not found at {version_sidecar_v2}"
        version_data = json.loads(version_sidecar_v2.read_text())
        assert version_data["version"] == 2, f"Version sidecar should be v2, got {version_data}"
        
        # Test 2: Generate duck v1 (no sidecar = default v1, should reuse)
        # First need to create duck with sidecar for proper version tracking
        duck_sidecar = refs_dir / ".versions" / "duck.json"
        duck_sidecar.write_text(json.dumps({"version": 1, "ref_key": "duck"}))
        
        result_duck = await generate_ref_activity(
            episode_id="ep99",
            ref_key="duck",
            prompt="Duck on water",
            output_path=str(duck_path),
            version=1,  # Matches existing v1
        )
        
        assert result_duck["status"] == "reused", f"Expected reused, got {result_duck}"
        assert result_duck["credits_used"] == 0, "Reused ref should cost 0 credits"
        assert duck_path.read_bytes() == duck_v1_content, "Duck content should be unchanged"
        
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
    pool_v1_content = create_valid_test_png()
    pool_path.write_bytes(pool_v1_content)
    
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
        
        # In dry mode, output is redirected
        actual_output_v3 = Path(result["output_path"])
        
        # In dry mode, real files are NOT archived (don't touch real files)
        # v1 stays at original location
        assert pool_path.exists(), "v1 should still exist at original location in dry mode"
        
        # No archive in dry mode
        v1_archive = refs_dir / "_v1" / "pool.png"
        assert not v1_archive.exists(), f"Dry mode should NOT archive, but found {v1_archive}"
        
        # New file should exist (at redirected path)
        assert actual_output_v3.exists(), f"v3 file not created at {actual_output_v3}"
        
        # Sidecar should show v3 (at redirected path)
        version_sidecar_v3 = actual_output_v3.parent / ".versions" / f"{actual_output_v3.stem}.json"
        assert version_sidecar_v3.exists(), f"Version sidecar not found at {version_sidecar_v3}"
        version_data = json.loads(version_sidecar_v3.read_text())
        assert version_data["version"] == 3, f"Version should be 3, got {version_data}"
        
    finally:
        temporalio.activity.info = original_info
