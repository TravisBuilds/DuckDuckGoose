"""End-to-end test: refs.json versions map -> loader -> generate_ref_activity."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_loader_reads_versions_map_and_generates_correctly(tmp_path, monkeypatch):
    """
    End-to-end: refs.json with versions {family:2} -> loader -> generate_ref_activity:
    - family regenerated as v2, v1 moved to refs/_v1/
    - rowan (no versions entry) reused as v1
    Test MUST fail if loader ignores the versions map.
    """
    from hfvg.activities.ref_generation import load_refs_to_generate_activity, generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from PIL import Image, ImageDraw
    import io
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=200)
    
    # Set up episode directory and refs
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    refs_dir = episode_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    
    # Create helper for valid PNG
    def make_png():
        img = Image.new('RGB', (512, 512), color='blue')
        draw = ImageDraw.Draw(img)
        for i in range(0, 512, 10):
            draw.line([(i, 0), (i, 512)], fill='red', width=1)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    
    # Pre-create family v1 with sidecar
    family_v1_content = make_png()
    family_path = refs_dir / "family.png"
    family_path.write_bytes(family_v1_content)
    
    family_sidecar = refs_dir / ".versions" / "family.json"
    family_sidecar.parent.mkdir(parents=True, exist_ok=True)
    family_sidecar.write_text(json.dumps({"version": 1, "ref_key": "family"}))
    
    # Pre-create rowan v1 (no versions entry = stays v1)
    rowan_v1_content = make_png()
    rowan_path = refs_dir / "rowan.png"
    rowan_path.write_bytes(rowan_v1_content)
    
    rowan_sidecar = refs_dir / ".versions" / "rowan.json"
    rowan_sidecar.write_text(json.dumps({"version": 1, "ref_key": "rowan"}))
    
    # Create refs.json with versions map
    refs_json = episode_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "to_generate": {
            "family": str(family_path),
            "rowan": str(rowan_path),
        },
        "prompts": {
            "family": "Family portrait",
            "rowan": "Rowan character",
        },
        "versions": {
            "family": 2,  # Bump family to v2
            # rowan not in versions = default v1
        }
    }))
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-e2e-wf'})()
        
        # Step 1: Load refs to generate (should read versions map)
        refs_to_gen = await load_refs_to_generate_activity("ep99", is_live=False)
        
        # Verify loader read the versions map correctly
        assert len(refs_to_gen) == 2, f"Expected 2 refs, got {len(refs_to_gen)}"
        
        family_spec = next((r for r in refs_to_gen if r["key"] == "family"), None)
        rowan_spec = next((r for r in refs_to_gen if r["key"] == "rowan"), None)
        
        assert family_spec is not None, "family not in to_generate"
        assert rowan_spec is not None, "rowan not in to_generate"
        
        # THIS IS THE KEY ASSERTION: loader must read version 2 from versions map
        assert family_spec["version"] == 2, \
            f"Loader FAILED to read versions map: family should be v2, got v{family_spec['version']}"
        assert rowan_spec["version"] == 1, \
            f"rowan should default to v1, got v{rowan_spec['version']}"
        
        # Step 2: Generate family v2 (should archive v1 and regenerate)
        result_family = await generate_ref_activity(
            episode_id="ep99",
            ref_key=family_spec["key"],
            prompt=family_spec["prompt"],
            output_path=family_spec["output_path"],
            version=family_spec["version"],
        )
        
        assert result_family["status"] == "completed", \
            f"family should regenerate for v2, got {result_family['status']}"
        
        # Verify v1 was archived (at original location, not scratch)
        family_v1_archive = refs_dir / "_v1" / "family.png"
        assert family_v1_archive.exists(), \
            f"family v1 not archived to {family_v1_archive}"
        assert family_v1_archive.read_bytes() == family_v1_content, \
            "family v1 archive content mismatch"
        
        # Verify new v2 file exists (at scratch location in dry mode)
        actual_v2_path = Path(result_family["output_path"])
        assert actual_v2_path.exists(), \
            f"family v2 not created at {actual_v2_path}"
        
        # Step 3: Generate rowan v1 (should reuse)
        result_rowan = await generate_ref_activity(
            episode_id="ep99",
            ref_key=rowan_spec["key"],
            prompt=rowan_spec["prompt"],
            output_path=rowan_spec["output_path"],
            version=rowan_spec["version"],
        )
        
        assert result_rowan["status"] == "reused", \
            f"rowan v1 should be reused, got {result_rowan['status']}"
        assert result_rowan["credits_used"] == 0, \
            "reused ref should cost 0 credits"
        
        # Verify rowan was NOT archived (version matched)
        rowan_v1_archive = refs_dir / "_v1" / "rowan.png"
        assert not rowan_v1_archive.exists(), \
            f"rowan should not be archived, but found {rowan_v1_archive}"
        
        # Verify rowan content unchanged (still at original location)
        assert rowan_path.read_bytes() == rowan_v1_content, \
            "rowan content should be unchanged"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_dry_mode_never_archives_outside_scratch(tmp_path, monkeypatch):
    """
    Test: In dry mode, archive/rename never touches real files outside scratch dir.
    This test ensures the fix for the bug where dry mode moved real files.
    """
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from PIL import Image, ImageDraw
    import io
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # Set up real ref directory
    real_refs_dir = tmp_path / "data" / "episodes" / "ep99" / "refs"
    real_refs_dir.mkdir(parents=True, exist_ok=True)
    
    # Create helper for valid PNG
    def make_png():
        img = Image.new('RGB', (512, 512), color='green')
        draw = ImageDraw.Draw(img)
        for i in range(0, 512, 10):
            draw.line([(0, i), (512, i)], fill='yellow', width=1)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        return buf.getvalue()
    
    # Create a real file at the "real" location
    real_file_path = real_refs_dir / "mochi.png"
    original_content = make_png()
    real_file_path.write_bytes(original_content)
    
    # Create v1 sidecar
    real_sidecar = real_refs_dir / ".versions" / "mochi.json"
    real_sidecar.parent.mkdir(parents=True, exist_ok=True)
    real_sidecar.write_text(json.dumps({"version": 1, "ref_key": "mochi"}))
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-dry-wf'})()
        
        # Generate v2 in dry mode (should NOT touch real file)
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="mochi",
            prompt="Mochi character v2",
            output_path=str(real_file_path),
            version=2,
        )
        
        assert result["status"] == "completed"
        
        # CRITICAL: Real file at original location should be UNTOUCHED
        assert real_file_path.exists(), \
            "Dry mode DELETED the real file (should never touch it)"
        assert real_file_path.read_bytes() == original_content, \
            "Dry mode MODIFIED the real file (should never touch it)"
        
        # Real file should NOT be moved to _v1/ in dry mode
        real_v1_archive = real_refs_dir / "_v1" / "mochi.png"
        assert not real_v1_archive.exists(), \
            f"Dry mode archived real file to {real_v1_archive} (should only affect scratch)"
        
        # The actual work happens in scratch directory
        scratch_output = Path(result["output_path"])
        assert "dry_run_outputs" in str(scratch_output), \
            f"Dry mode output should be in scratch, got {scratch_output}"
        
    finally:
        temporalio.activity.info = original_info
