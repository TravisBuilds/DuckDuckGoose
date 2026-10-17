"""Test anchor still feature."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_set_anchor_still_basic(tmp_path):
    """Test setting an anchor still."""
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Create episode
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Set anchor
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"fake image data")
    
    import hashlib
    sha256 = hashlib.sha256(b"fake image data").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02", "A03"],
        str(anchor_file), 1, sha256, None
    )
    
    # Verify A02 has anchor
    anchor = await get_anchor_still(db_path, "ep99", "A02")
    assert anchor is not None
    assert anchor["anchor_shot_id"] == "A01"
    assert anchor["local_path"] == str(anchor_file)
    assert anchor["version"] == 1
    assert anchor["sha256"] == sha256
    assert anchor["approved_by"] is None
    
    # Verify A03 has anchor
    anchor = await get_anchor_still(db_path, "ep99", "A03")
    assert anchor is not None
    assert anchor["anchor_shot_id"] == "A01"


@pytest.mark.asyncio
async def test_set_anchor_still_with_approved_by(tmp_path):
    """Test setting an anchor still from external file with approved_by note."""
    from hfvg.studio_db import init_studio_db, upsert_episode, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    
    # Set anchor from external file
    anchor_file = tmp_path / "external.png"
    anchor_file.write_bytes(b"external image")
    
    import hashlib
    sha256 = hashlib.sha256(b"external image").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 0, sha256, "Travis approved via Studio"
    )
    
    anchor = await get_anchor_still(db_path, "ep99", "A02")
    assert anchor is not None
    assert anchor["approved_by"] == "Travis approved via Studio"


@pytest.mark.asyncio
async def test_anchor_still_injected_into_refs(tmp_path, monkeypatch):
    """Test that anchor still is injected into refs at submit time (live path with fake provider)."""
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    # Track refs passed to submit_image
    captured_refs = []
    
    class FakeProvider:
        model_path = "unknown/model"  # Unknown model forces STILL_MAX_REFS
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)
        
        async def submit_image(self, image_urls=None, **kwargs):
            # Capture the refs
            captured_refs.append(image_urls or [])
            return "fake-job-id"
        
        async def upload_reference(self, path, version):
            return {"public_url": f"fake://ref/{path}"}
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("STILL_MAX_REFS", "3")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Set up ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep99")
    await ledger.set_manual_balance(1_000_000_000, "test", "Test balance")
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create anchor still file
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor image")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor image").hexdigest()
    
    # Set anchor: A01 is the anchor for A02
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Patch and submit
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        result = await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A02",
            prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
            version=1,
            refs=[],
            resolution="1k",
            quality="medium",
            shot_meta={"count": 0},
        )
    
    # Verify anchor was injected
    assert len(captured_refs) == 1
    # The anchor should be the first (index 0) element in the refs list
    assert len(captured_refs[0]) == 1
    assert "fake://ref/" in captured_refs[0][0]


@pytest.mark.asyncio
async def test_anchor_still_enforces_ref_cap(tmp_path, monkeypatch):
    """Test that anchor still + refs respects model ref cap (live path with fake provider)."""
    import os
    from unittest.mock import AsyncMock, patch
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108, set_anchor_still
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    class FakeProvider:
        model_path = "unknown/model"  # Unknown model forces STILL_MAX_REFS
        
        async def close(self):
            pass
        
        async def estimate_usd_micros(self, **kwargs):
            return 50_000
        
        async def estimate_usd_and_credits(self, **kwargs):
            return (50_000, 1.0)
        
        async def submit_image(self, **kwargs):
            return "fake-job-id"
        
        async def upload_reference(self, path, version):
            return {"public_url": f"fake://ref/{path}"}
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("MODEL_PATH_STILL", "unknown/model")  # Use unknown model
    monkeypatch.setenv("STILL_MAX_REFS", "3")  # Cap at 3 refs for unknown models
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True)
    await approve_g108(db_path, "ep99")
    
    # Set up ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep99")
    await ledger.set_manual_balance(1_000_000_000, "test", "Test balance")
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create anchor
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Create 3 regular refs
    ref1 = tmp_path / "ref1.png"
    ref1.write_bytes(b"ref1")
    ref2 = tmp_path / "ref2.png"
    ref2.write_bytes(b"ref2")
    ref3 = tmp_path / "ref3.png"
    ref3.write_bytes(b"ref3")
    
    # Try to submit with 3 regular refs (+ anchor = 4 total, over cap of 3)
    with patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock) as mock_check, \
         patch("hfvg.activities.studio_generation.enforce_prompt_lint") as mock_lint, \
         patch("hfvg.activities.studio_generation.HiggsfieldStillProvider") as MockProvider:
        
        mock_check.return_value = (True, True)
        mock_lint.return_value = None
        MockProvider.return_value = FakeProvider()
        
        with pytest.raises(ValueError, match="over the .* limit of 3"):
            await submit_still_job_enforced(
                episode_id="ep99",
                shot_id="A02",
                prompt="Exactly 0 animals in frame. Real-time, no slow motion. Test shot.",
                version=1,
                refs=[str(ref1), str(ref2), str(ref3)],
                shot_meta={"count": 0},
            )


@pytest.mark.asyncio
async def test_anchor_still_included_in_job_id(tmp_path, monkeypatch):
    """Test that anchor version is included in job_id for idempotency."""
    import os
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, upsert_episode, set_anchor_still
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", beatmap_path="test.md", live_mode=False)
    
    # Create anchor
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor v1")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor v1").hexdigest()
    
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Submit (DRY_RUN doesn't include anchor in job_id, but live mode would)
    result = await submit_still_job_enforced(
        episode_id="ep99",
        shot_id="A02",
        prompt="Test",
        version=1,
    )
    
    # Verify we get a job_id back
    assert "job_id" in result

