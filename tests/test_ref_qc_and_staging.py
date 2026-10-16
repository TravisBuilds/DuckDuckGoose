"""Tests for ref QC, staging paths, and credit handling."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_ref_commits_credits_even_when_qc_fails(tmp_path, monkeypatch):
    """Test: Provider completion commits credits even if QC then fails."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from hfvg.errors import QCFailedError
    import temporalio.activity
    import respx
    import httpx
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    # Initialize budget ledger with manual balance for GC.01 check
    from hfvg.budget import BudgetLedger
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    # Create L1_refs line for ref generation
    await budget_ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Mock HTTP responses - provider succeeds, returns tiny image that will fail QC
        with respx.mock:
            # Mock estimate endpoint (needed for live mode USD estimate)
            respx.post("https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
            )
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-qc-fail-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-qc-fail-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "https://example.com/tiny.png"}]
                })
            )
            respx.get("https://example.com/tiny.png").mock(
                return_value=httpx.Response(200, content=b"tiny")  # Too small for QC
            )
            
            # Should raise QCFailedError due to QC failure
            with pytest.raises(QCFailedError) as exc_info:
                await generate_ref_activity(
                    episode_id="ep99",
                    ref_key="r_duck",
                    prompt="Duck character sheet",
                    output_path=output_path,
                    version=1,
                )
            
            assert "QC failed" in str(exc_info.value)
            
            # Check that credits were committed (provider completed)
            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'commit'",
                    ("ref-ep99-r_duck-v1",)
                )
                row = await cursor.fetchone()
                assert row[0] == 1, "Credits should be committed even though QC failed"
                
                # Check that credits were NOT released
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'release'",
                    ("ref-ep99-r_duck-v1",)
                )
                row = await cursor.fetchone()
                assert row[0] == 0, "Credits should not be released when provider completed"
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_ref_uses_staging_path_before_qc(tmp_path, monkeypatch):
    """Test: Ref downloads to staging path, only moves to final after QC passes."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from hfvg.errors import QCFailedError
    import temporalio.activity
    import respx
    import httpx
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    # Initialize budget ledger with manual balance for GC.01 check
    from hfvg.budget import BudgetLedger
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    # Create L1_refs line for ref generation
    await budget_ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    staging_path = output_path.parent / f".staging_{output_path.name}"
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Mock HTTP responses - QC will fail
        with respx.mock:
            # Mock estimate endpoint (needed for live mode USD estimate)
            respx.post("https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
            )
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-staging-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-staging-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "https://example.com/bad.png"}]
                })
            )
            respx.get("https://example.com/bad.png").mock(
                return_value=httpx.Response(200, content=b"bad")  # Fails QC
            )
            
            # Should raise QCFailedError
            with pytest.raises(QCFailedError):
                await generate_ref_activity(
                    episode_id="ep99",
                    ref_key="r_duck",
                    prompt="Duck character sheet",
                    output_path=str(output_path),
                    version=1,
                )
            
            # Final path should NOT exist
            assert not output_path.exists(), "Final path should not exist when QC fails"
            
            # Staging path SHOULD exist (kept for review)
            assert staging_path.exists(), "Staging file should be kept for review"
            assert staging_path.read_bytes() == b"bad"
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_ref_moves_to_final_path_when_qc_passes(tmp_path, monkeypatch):
    """Test: Ref moves from staging to final path when QC passes."""
    from hfvg.activities.ref_generation import generate_ref_activity, _check_ref_qc
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    import respx
    import httpx
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    # Initialize budget ledger with manual balance for GC.01 check
    from hfvg.budget import BudgetLedger
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    # Create L1_refs line for ref generation
    await budget_ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    staging_path = output_path.parent / f".staging_{output_path.name}"
    
    # Mock QC to pass
    from hfvg.activities import ref_generation
    original_qc = ref_generation._check_ref_qc
    
    async def mock_qc_pass(*args, **kwargs):
        return {"passed": True}
    
    ref_generation._check_ref_qc = mock_qc_pass
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Mock HTTP responses
        with respx.mock:
            # Mock estimate endpoint (needed for live mode USD estimate)
            respx.post("https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
            )
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-pass-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-pass-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "https://example.com/good.png"}]
                })
            )
            respx.get("https://example.com/good.png").mock(
                return_value=httpx.Response(200, content=b"good-image-data")
            )
            
            result = await generate_ref_activity(
                episode_id="ep99",
                ref_key="r_duck",
                prompt="Duck character sheet",
                output_path=str(output_path),
                version=1,
            )
            
            assert result["status"] == "completed"
            
            # Final path SHOULD exist
            assert output_path.exists(), "Final path should exist when QC passes"
            assert output_path.read_bytes() == b"good-image-data"
            
            # Staging path should NOT exist (moved to final)
            assert not staging_path.exists(), "Staging file should be moved to final"
    
    finally:
        temporalio.activity.info = original_info
        ref_generation._check_ref_qc = original_qc


@pytest.mark.asyncio
async def test_provider_failure_releases_credits(tmp_path, monkeypatch):
    """Test: Provider failure (not QC) releases credits."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    import respx
    import httpx
    import pytest
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    # Initialize budget ledger with manual balance for GC.01 check
    from hfvg.budget import BudgetLedger
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    # Create L1_refs line for ref generation
    await budget_ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Mock HTTP responses - provider fails
        with respx.mock:
            # Mock estimate endpoint (needed for live mode USD estimate)
            respx.post("https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
            )
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-fail-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-fail-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "failed",
                    "error": "Generation failed"
                })
            )
            
            # Should raise RuntimeError (provider failure)
            with pytest.raises(RuntimeError) as exc_info:
                await generate_ref_activity(
                    episode_id="ep99",
                    ref_key="r_duck",
                    prompt="Duck character sheet",
                    output_path=output_path,
                    version=1,
                )
            
            assert "failed" in str(exc_info.value).lower()
            
            # Check that credits were released (provider failed, not charged)
            import aiosqlite
            async with aiosqlite.connect(db_path) as db:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'release'",
                    ("ref-ep99-r_duck-v1",)
                )
                row = await cursor.fetchone()
                assert row[0] == 1, "Credits should be released when provider fails"
                
                # Check that credits were NOT committed
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'commit'",
                    ("ref-ep99-r_duck-v1",)
                )
                row = await cursor.fetchone()
                assert row[0] == 0, "Credits should not be committed when provider fails"
    
    finally:
        temporalio.activity.info = original_info
