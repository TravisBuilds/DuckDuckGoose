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
    # High quality: ~6.5 credits
    assert STILL_CREDITS["xai/grok-imagine-image-2.0"]["high"] == 6.5
    # Standard quality: ~4.0 credits
    assert STILL_CREDITS["xai/grok-imagine-image-2.0"]["standard"] == 4.0


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
    
    # Grok Image 2.0 high quality
    cost = estimate_still_credits("xai/grok-imagine-image-2.0", "high")
    assert cost == 6
    
    # Grok Image 2.0 standard quality
    cost = estimate_still_credits("xai/grok-imagine-image-2.0", "standard")
    assert cost == 4
    
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
    
    models_doc = Path("docs/MODELS.md")
    assert models_doc.exists()
    
    content = models_doc.read_text()
    
    # Should document SOUL V2
    assert "higgsfield-ai/soul/v2/standard" in content
    assert "SOUL V2" in content
    
    # Should document Grok Image 2.0
    assert "xai/grok-imagine-image-2.0" in content
    assert "Grok Image 2.0" in content
    
    # Should have pricing info
    assert "0.09" in content  # SOUL V2 price
    assert "6.5" in content   # Grok high quality
    assert "4.0" in content   # Grok standard
    
    # Should have API doc links
    assert "https://docs.higgsfield.ai" in content
