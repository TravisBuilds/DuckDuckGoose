"""Tests for model selection and pricing."""

import pytest


def test_ref_model_pricing_table():
    """Test: ref pricing table includes SOUL V2 with documented cost."""
    from hfvg.credit_pricing import REF_CREDITS
    
    # SOUL V2 should be in the table
    assert "higgsfield-ai/soul/v2/standard" in REF_CREDITS
    # Price should be ~0.09 credits (from /estimate endpoint)
    assert REF_CREDITS["higgsfield-ai/soul/v2/standard"] == 0.09


def test_still_model_pricing_table():
    """Test: still pricing table includes Grok Image 2.0 with documented costs."""
    from hfvg.credit_pricing import STILL_CREDITS
    
    # Grok Image 2.0 should be in the table
    assert "xai/grok-imagine-image-2.0" in STILL_CREDITS
    # Real cost: ~1.12 credits
    assert STILL_CREDITS["xai/grok-imagine-image-2.0"]["high"] == 1.12
    assert STILL_CREDITS["xai/grok-imagine-image-2.0"]["standard"] == 1.12


def test_estimate_ref_credits():
    """Test: estimate_ref_credits returns correct price for SOUL V2."""
    from hfvg.credit_pricing import estimate_ref_credits
    
    # SOUL V2 model path
    cost = estimate_ref_credits("higgsfield-ai/soul/v2/standard")
    assert cost == 0.09
    
    # Unknown model falls back to default
    cost = estimate_ref_credits("unknown/model")
    assert cost == 0.1  # Default


def test_estimate_still_credits():
    """Test: estimate_still_credits returns correct price for Grok Image 2.0."""
    from hfvg.credit_pricing import estimate_still_credits
    
    # Grok Image 2.0 (~1.12 credits)
    cost = estimate_still_credits("xai/grok-imagine-image-2.0", "high")
    assert cost == 1.12
    
    cost = estimate_still_credits("xai/grok-imagine-image-2.0", "standard")
    assert cost == 1.12
    
    # Unknown model falls back to default
    cost = estimate_still_credits("unknown/model")
    assert cost == 5


@pytest.mark.asyncio
async def test_ref_generation_uses_soul_v2_pricing(tmp_path, monkeypatch):
    """Test: ref generation uses SOUL V2 model and correct pricing."""
    from hfvg.activities.ref_generation import generate_ref_activity
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck character sheet",
            output_path=output_path,
            version=1,
        )
        
        # Check that credits used is the SOUL V2 price (0.09)
        assert result["credits_used"] == 0.09
        
        # Verify in DB
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT amount_credits FROM credit_transactions WHERE job_id = ? AND txn_type = 'reserve'",
                (result["job_id"],)
            )
            row = await cursor.fetchone()
            assert row[0] == 0.09
    finally:
        temporalio.activity.info = original_info


def test_still_model_path_configuration():
    """Test: still_model_path returns Grok Image 2.0 by default."""
    from hfvg.config import still_model_path
    
    model = still_model_path()
    assert model == "xai/grok-imagine-image-2.0"


def test_models_doc_exists():
    """Test: MODELS.md documentation exists with model choices."""
    from pathlib import Path
    
    # Find repo root (test runs in tmp_path via autouse fixture)
    test_file = Path(__file__)
    repo_root = test_file.parent.parent  # /workspace
    models_doc = repo_root / "docs" / "MODELS.md"
    assert models_doc.exists(), f"MODELS.md not found at {models_doc}"
    
    content = models_doc.read_text()
    
    # Should document SOUL V2
    assert "higgsfield-ai/soul/v2/standard" in content
    assert "SOUL V2" in content
    
    # Should document Grok Image 2.0
    assert "xai/grok-imagine-image-2.0" in content
    assert "Grok Image 2.0" in content
    
    # Should have pricing info
    assert "0.09" in content  # SOUL V2 price
    assert "1.12" in content  # Grok price
    
    # Should have API doc links
    assert "https://docs.higgsfield.ai" in content


@pytest.mark.asyncio
async def test_successful_still_submit_returns_dict_and_writes_rows(tmp_path, monkeypatch):
    """Test: Successful still submit returns dict with provider_job_id and writes DB rows."""
    import respx
    import httpx
    import os
    
    # Setup test environment
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99", None)
    await set_live_mode(db_path, "ep99", True, "test")
    await approve_g108(db_path, "ep99", "test")
    
    # Initialize budget
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.set_line("ep99", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=100)
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock estimate
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
        )
        
        # Mock submit - returns provider job id
        respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"request_id": "provider_xyz123"})
        )
        
        from hfvg.activities.studio_generation import submit_still_job_enforced
        
        result = await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A01",
            prompt="Test duck, locked-off tripod, no camera movement, real-time, no slow motion. Exactly 1 animals in frame. synthetic duck clause: one duck on the water",
            version=1,
            refs=None,
            resolution="1k",
            quality="medium",
            shot_meta={"count": 1, "exterior": False, "water": False, "characters": ["duck"]}
        )
        
        # Verify result dict has all expected fields
        assert result is not None, "Submit returned None instead of dict"
        assert "job_id" in result
        assert "provider_job_id" in result
        assert "line_name" in result
        assert "reserved_amount" in result
        
        assert result["job_id"] == "still-ep99-A01-v1"
        assert result["provider_job_id"] == "provider_xyz123"
        assert result["line_name"] == "L2_drafts"
        
        # Verify provider_jobs row was written
        provider_job_id = await credit_ledger.get_provider_job(result["job_id"])
        assert provider_job_id == "provider_xyz123", "Provider job mapping not persisted"
        
        # Verify job_ledger row was written
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT job_id FROM job_ledger WHERE job_id = ?",
                (result["job_id"],)
            )
            row = await cursor.fetchone()
            assert row is not None, "job_ledger row not written"
            assert row[0] == result["job_id"]


@pytest.mark.asyncio
async def test_422_error_releases_each_ledger_exactly_once(tmp_path, monkeypatch):
    """Test: A 422 provider error releases budget and credit ledgers exactly once."""
    import respx
    import httpx
    import os
    
    # Setup test environment
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99", None)
    await set_live_mode(db_path, "ep99", True, "test")
    await approve_g108(db_path, "ep99", "test")
    
    # Initialize budget
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.set_line("ep99", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=100)
    
    # Mock Higgsfield API
    with respx.mock:
        # Mock estimate
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
        )
        
        # Mock submit - returns 422 (idempotency conflict)
        respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(422, json={"error": "Idempotency key conflict"})
        )
        
        from hfvg.activities.studio_generation import submit_still_job_enforced
        
        # Submit should raise ValueError with provider error
        with pytest.raises(ValueError) as exc_info:
            await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A01",
                prompt="Test duck, locked-off tripod, no camera movement, real-time, no slow motion. Exactly 1 animals in frame. synthetic duck clause: one duck on the water",
                version=1,
                refs=None,
                resolution="1k",
                quality="medium",
                shot_meta={"count": 1, "exterior": False, "water": False, "characters": ["duck"]}
            )
        
        assert "422" in str(exc_info.value)
        
        # Verify budget was released exactly once (not twice)
        budget_status = await ledger.get_line_status("ep99", "L2_drafts")
        assert budget_status["reserved"] == 0, "Budget should be released"
        
        # Count budget release transactions - should be exactly 1
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM budget_transactions WHERE txn_type = 'release' AND line_id LIKE '%L2_drafts'"
            )
            release_count = (await cursor.fetchone())[0]
            assert release_count == 1, f"Budget released {release_count} times, expected exactly 1"
        
        # Verify credits were released exactly once (not twice)
        credit_status = await credit_ledger.get_episode_status("ep99")
        assert credit_status["reserved_credits"] == 0, "Credits should be released"
        
        # Count credit release transactions - should be exactly 1
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM credit_transactions WHERE episode_id = 'ep99' AND txn_type = 'release'"
            )
            release_count = (await cursor.fetchone())[0]
            assert release_count == 1, f"Credits released {release_count} times, expected exactly 1"
