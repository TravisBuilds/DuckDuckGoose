"""Tests for smoke test gate enforcement."""

import os
import pytest
from datetime import datetime, timedelta


@pytest.mark.asyncio
async def test_smoke_gate_refuses_without_passing_test(tmp_path, monkeypatch):
    """Test that submit_still_job_enforced refuses when no smoke test has passed."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "false")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep01", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep01", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep01", "test")
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep01", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep01", cap_credits=1000)
    
    # Try to submit without smoke test
    from hfvg.activities.studio_generation import submit_still_job_enforced
    
    call_succeeded = False
    try:
        result = await submit_still_job_enforced(
            episode_id="ep01",
            shot_id="A01",
            prompt="Test, real-time, no slow motion. Exactly 0 animals in frame.",
            version=1,
            refs=[],
            shot_meta={"count": 0, "exterior": False, "water": False, "characters": []}
        )
        call_succeeded = True
    except RuntimeError as exc:
        assert "Smoke test gate failed" in str(exc)
        assert "No smoke test result found" in str(exc)
    
    # CRITICAL ASSERTION: submit should have failed (mutation M-116 will break this)
    assert not call_succeeded, "Submit should fail without passing smoke test"


@pytest.mark.asyncio
async def test_smoke_gate_allows_with_recent_passing_test(tmp_path, monkeypatch):
    """Test that submit allows when smoke test passed recently."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108, store_smoke_test_result
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep01", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep01", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep01", "test")
    
    # Store a recent passing smoke test
    smoke_result = {
        "passed": True,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "upload_probe": {"passed": True, "public_url": "http://test", "error": None},
        "generation_probe": {"passed": True, "provider_job_id": "test", "cost": 0.09, "error": None},
    }
    await store_smoke_test_result(str(tmp_path / "test.db"), "ep01", smoke_result)
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep01", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep01", cap_credits=1000)
    
    # Mock provider
    import respx
    import httpx
    with respx.mock:
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
        )
        respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"request_id": "test_job"})
        )
        
        # Submit should succeed now
        from hfvg.activities.studio_generation import submit_still_job_enforced
        result = await submit_still_job_enforced(
            episode_id="ep01",
            shot_id="A01",
            prompt="Test, real-time, no slow motion. Exactly 0 animals in frame.",
            version=1,
            refs=[],
            shot_meta={"count": 0, "exterior": False, "water": False, "characters": []}
        )
        
        assert result["job_id"] == "still-ep01-A01-v1"


@pytest.mark.asyncio
async def test_smoke_gate_refuses_expired_test(tmp_path, monkeypatch):
    """Test that submit refuses when smoke test is too old."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "false")
    monkeypatch.setenv("SMOKE_TEST_MAX_AGE_HOURS", "24")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108, store_smoke_test_result
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep01", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep01", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep01", "test")
    
    # Store an expired smoke test (25 hours ago)
    old_timestamp = (datetime.utcnow() - timedelta(hours=25)).isoformat() + "Z"
    smoke_result = {
        "passed": True,
        "timestamp": old_timestamp,
        "upload_probe": {"passed": True, "public_url": "http://test", "error": None},
        "generation_probe": {"passed": True, "provider_job_id": "test", "cost": 0.09, "error": None},
    }
    await store_smoke_test_result(str(tmp_path / "test.db"), "ep01", smoke_result)
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep01", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep01", cap_credits=1000)
    
    # Try to submit with expired smoke test
    from hfvg.activities.studio_generation import submit_still_job_enforced
    
    call_succeeded = False
    try:
        result = await submit_still_job_enforced(
            episode_id="ep01",
            shot_id="A01",
            prompt="Test, real-time, no slow motion. Exactly 0 animals in frame.",
            version=1,
            refs=[],
            shot_meta={"count": 0, "exterior": False, "water": False, "characters": []}
        )
        call_succeeded = True
    except RuntimeError as exc:
        assert "Smoke test gate failed" in str(exc)
        assert "too old" in str(exc)
    
    assert not call_succeeded, "Submit should fail with expired smoke test"


@pytest.mark.asyncio
async def test_smoke_gate_override_allows_without_test(tmp_path, monkeypatch):
    """Test that SMOKE_TEST_OVERRIDE bypasses the gate."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Admin override
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep01", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep01", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep01", "test")
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep01", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep01", cap_credits=1000)
    
    # Mock provider
    import respx
    import httpx
    with respx.mock:
        respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
        )
        respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"request_id": "test_job"})
        )
        
        # Submit should succeed with override
        from hfvg.activities.studio_generation import submit_still_job_enforced
        result = await submit_still_job_enforced(
            episode_id="ep01",
            shot_id="A01",
            prompt="Test, real-time, no slow motion. Exactly 0 animals in frame.",
            version=1,
            refs=[],
            shot_meta={"count": 0, "exterior": False, "water": False, "characters": []}
        )
        
        assert result["job_id"] == "still-ep01-A01-v1"
