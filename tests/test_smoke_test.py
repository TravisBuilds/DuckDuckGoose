"""Test smoke_test.py end-to-end with mocked API."""

import pytest
import respx
import httpx


@pytest.mark.asyncio
@respx.mock
async def test_smoke_test_end_to_end(tmp_path, monkeypatch):
    """Test run_smoke_test with mocked Higgsfield API."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    
    # Initialize DB and episode
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    await init_studio_db(str(tmp_path / "test.db"))
    await create_episode(str(tmp_path / "test.db"), "ep01", None)
    await set_live_mode(str(tmp_path / "test.db"), "ep01", True, "test")
    await approve_g108(str(tmp_path / "test.db"), "ep01", "test")
    
    # Set up budget and credits
    ledger = BudgetLedger(str(tmp_path / "test.db"))
    await ledger.init_db()
    await ledger.set_manual_balance(100_000_000, "test", "test")
    await ledger.set_line("ep01", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    
    credit_ledger = CreditLedger(str(tmp_path / "test.db"))
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep01", cap_credits=1000)
    
    # Mock upload probe
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080200000090775"
        "3de0000000c49444154789c6260010000000200010e0d0000000049454e44ae426082"
    )
    
    respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put",
            "public_url": "http://mock.cdn/probe.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    )
    respx.put("http://mock.upload/put").mock(
        return_value=httpx.Response(200)
    )
    respx.get("http://mock.cdn/probe.png").mock(
        return_value=httpx.Response(200, content=png_bytes)
    )
    
    # Mock generation probe
    respx.post("http://mock.api/estimate/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
    )
    respx.post("http://mock.api/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"request_id": "smoke_job_123"})
    )
    respx.get("http://mock.api/requests/smoke_job_123/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "image": {"url": "http://mock.cdn/result.jpg"},
            "cost": 0.09
        })
    )
    
    # Run smoke test
    from hfvg.activities.smoke_test import run_smoke_test
    result = await run_smoke_test("ep01", str(tmp_path / "test.db"))
    
    # Verify results
    assert result["passed"] is True
    assert result["upload_probe"]["passed"] is True
    assert result["generation_probe"]["passed"] is True
    assert result["generation_probe"]["cost"] == 0.09
