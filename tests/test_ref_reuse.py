"""Tests for ref reuse (skip generation if file exists and passes QC)."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_ref_generation_skips_existing_file_that_passes_qc(tmp_path, monkeypatch):
    """Test: Ref generation skips if output file exists and passes QC (no spend)."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create a pre-existing ref file that will pass QC (large enough)
    # Minimal valid PNG (1x1 red pixel) - will fail QC due to size
    # Let's make it "pass" by mocking QC
    output_path.write_bytes(b"existing-ref-image-data")
    
    # Mock QC to pass
    from hfvg.activities import ref_generation
    original_qc = ref_generation._check_ref_qc
    
    async def mock_qc_pass(*args, **kwargs):
        return {"passed": True}
    
    ref_generation._check_ref_qc = mock_qc_pass
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=str(output_path),
            version=1,
        )
        
        assert result["status"] == "reused", "Should reuse existing file"
        assert result["credits_used"] == 0, "Should not spend credits"
        assert output_path.read_bytes() == b"existing-ref-image-data", "Should not overwrite"
        
        # Check that no credits were reserved or committed
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ?",
                ("ref-ep99-r_duck-v1",)
            )
            row = await cursor.fetchone()
            assert row[0] == 0, "Should not have any credit transactions"
    
    finally:
        temporalio.activity.info = original_info
        ref_generation._check_ref_qc = original_qc


@pytest.mark.asyncio
async def test_ref_generation_regenerates_if_qc_fails(tmp_path, monkeypatch):
    """Test: Ref generation regenerates if existing file fails QC."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Create a pre-existing ref file that will fail QC
    output_path.write_bytes(b"bad-ref")
    
    # Mock QC to fail on existing, pass on new
    from hfvg.activities import ref_generation
    original_qc = ref_generation._check_ref_qc
    
    call_count = 0
    
    async def mock_qc_fail_then_pass(path):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call on existing file - fail
            return {"passed": False, "reason": "too small"}
        else:
            # Second call on new file - pass
            return {"passed": True}
    
    ref_generation._check_ref_qc = mock_qc_fail_then_pass
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=str(output_path),
            version=1,
        )
        
        assert result["status"] == "completed", "Should regenerate"
        assert result["credits_used"] > 0, "Should spend credits"
        # In dry mode, output is redirected to scratch dir, not original path
        # Just verify that generation happened (credits spent)
        
        # Check that credits were committed (dry mode)
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'commit'",
                ("ref-ep99-r_duck-v1",)
            )
            row = await cursor.fetchone()
            assert row[0] == 1, "Should have committed credits"
    
    finally:
        temporalio.activity.info = original_info
        ref_generation._check_ref_qc = original_qc


@pytest.mark.asyncio
async def test_ref_version_bump_forces_regeneration(tmp_path, monkeypatch):
    """Test: Version bump forces regeneration even if v1 exists and passes QC."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Generate v1 (no existing file)
    original_info = temporalio.activity.info
    
    from hfvg.activities import ref_generation
    original_qc = ref_generation._check_ref_qc
    
    async def mock_qc_pass(*args, **kwargs):
        return {"passed": True}
    
    ref_generation._check_ref_qc = mock_qc_pass
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate v1
        result_v1 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=str(output_path),
            version=1,
        )
        
        assert result_v1["status"] == "completed"
        # In dry mode, output is in scratch dir, so check the result instead
        v1_job_id = result_v1["job_id"]
        
        # Generate v2 (same output_path, but version bumped)
        result_v2 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet updated",
            output_path=str(output_path),
            version=2,
        )
        
        # v2 should regenerate (not reuse v1)
        assert result_v2["status"] == "completed", "Should regenerate for v2"
        assert result_v2["credits_used"] > 0, "Should spend credits for v2"
        assert result_v2["job_id"] != v1_job_id, "Should have different job_id for v2"
        
        # Check that we have separate credit transactions for v1 and v2
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM credit_transactions WHERE episode_id = 'ep99' AND txn_type = 'commit'"
            )
            row = await cursor.fetchone()
            assert row[0] == 2, "Should have 2 separate credit commits (v1 and v2)"
    
    finally:
        temporalio.activity.info = original_info
        ref_generation._check_ref_qc = original_qc
