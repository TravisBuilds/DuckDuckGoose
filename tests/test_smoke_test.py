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
    
    # Generate valid PNG with PIL for upload probe
    from PIL import Image
    from io import BytesIO
    img = Image.new('RGB', (1, 1), color='red')
    png_buffer = BytesIO()
    img.save(png_buffer, 'PNG')
    png_bytes = png_buffer.getvalue()
    
    # Verify uploaded bytes decode as PNG
    upload_capture = []
    
    def capture_upload(request: httpx.Request):
        upload_capture.append(request.content)
        # Verify uploaded bytes decode as PNG
        try:
            img_check = Image.open(BytesIO(request.content))
            img_check.verify()
        except Exception as e:
            raise AssertionError(f"Uploaded bytes don't decode as PNG: {e}")
        return httpx.Response(200)
    
    respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put",
            "public_url": "http://mock.cdn/probe.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    )
    respx.put("http://mock.upload/put").mock(side_effect=capture_upload)
    respx.get("http://mock.cdn/probe.png").mock(
        return_value=httpx.Response(200, content=png_bytes)
    )
    
    # Mock generation probe - assert submit JSON matches SOUL V2 schema
    submit_capture = []
    
    def capture_submit(request: httpx.Request):
        import json
        payload = json.loads(request.content)
        submit_capture.append(payload)
        
        # Assert SOUL V2 schema: {prompt, resolution, aspect_ratio} - no quality field
        assert "prompt" in payload, "SOUL V2 submit must include prompt"
        assert "resolution" in payload, "SOUL V2 submit must include resolution"
        assert payload["resolution"] in ("720p", "1080p"), f"SOUL V2 only allows 720p/1080p, got {payload['resolution']}"
        assert "aspect_ratio" in payload, "SOUL V2 submit must include aspect_ratio"
        assert "quality" not in payload, "SOUL V2 submit must NOT include quality field"
        
        return httpx.Response(200, json={"request_id": "smoke_job_123"})
    
    # Generate valid output PNG for result
    result_img = Image.new('RGB', (128, 128), color='blue')
    result_buffer = BytesIO()
    result_img.save(result_buffer, 'PNG')
    result_bytes = result_buffer.getvalue()
    
    respx.post("http://mock.api/estimate/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
    )
    respx.post("http://mock.api/higgsfield-ai/soul/v2/standard").mock(side_effect=capture_submit)
    respx.get("http://mock.api/requests/smoke_job_123/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "images": [{"url": "http://mock.cdn/result.png"}],
            "cost": 0.09
        })
    )
    respx.get("http://mock.cdn/result.png").mock(
        return_value=httpx.Response(200, content=result_bytes)
    )
    
    # Run smoke test
    from hfvg.activities.smoke_test import run_smoke_test
    result = await run_smoke_test("ep01", str(tmp_path / "test.db"))
    
    # Debug: print result if failed
    if not result["passed"]:
        import json
        print(f"SMOKE TEST FAILED: {json.dumps(result, indent=2)}")
    
    # Verify results
    assert result["passed"] is True
    assert result["upload_probe"]["passed"] is True
    assert result["generation_probe"]["passed"] is True
    assert result["generation_probe"]["cost"] == 0.09
    
    # Verify upload was captured and valid
    assert len(upload_capture) == 1
    assert len(submit_capture) == 1


@pytest.mark.asyncio
@respx.mock
async def test_smoke_test_releases_on_submit_exception(tmp_path, monkeypatch):
    """Test that budget and credits are released when submit raises an exception."""
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
    
    # Mock upload probe (succeeds)
    from PIL import Image
    from io import BytesIO
    img = Image.new('RGB', (1, 1), color='red')
    png_buffer = BytesIO()
    img.save(png_buffer, 'PNG')
    png_bytes = png_buffer.getvalue()
    
    respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put",
            "public_url": "http://mock.cdn/probe.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    )
    respx.put("http://mock.upload/put").mock(return_value=httpx.Response(200))
    respx.get("http://mock.cdn/probe.png").mock(
        return_value=httpx.Response(200, content=png_bytes)
    )
    
    # Mock estimate (succeeds)
    respx.post("http://mock.api/estimate/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
    )
    
    # Mock submit (fails with exception)
    respx.post("http://mock.api/higgsfield-ai/soul/v2/standard").mock(
        side_effect=httpx.HTTPError("Submit failed")
    )
    
    # Check initial budget/credit state
    status_before = await ledger.get_line_status("ep01", "L2_drafts")
    credits_before = await credit_ledger.get_episode_status("ep01")
    
    # Run smoke test (should fail but release holds)
    from hfvg.activities.smoke_test import run_smoke_test
    result = await run_smoke_test("ep01", str(tmp_path / "test.db"))
    
    # Should fail
    assert result["passed"] is False
    assert "Submit failed" in result["generation_probe"]["error"]
    
    # Verify budget and credits were released
    status_after = await ledger.get_line_status("ep01", "L2_drafts")
    credits_after = await credit_ledger.get_episode_status("ep01")
    
    assert status_after["reserved"] == status_before["reserved"], "Budget should be released"
    assert credits_after["reserved"] == credits_before["reserved"], "Credits should be released"


@pytest.mark.asyncio
@respx.mock
async def test_smoke_test_releases_on_poll_exception(tmp_path, monkeypatch):
    """Test that budget and credits are released when polling raises an exception."""
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
    
    # Mock upload probe (succeeds)
    from PIL import Image
    from io import BytesIO
    img = Image.new('RGB', (1, 1), color='red')
    png_buffer = BytesIO()
    img.save(png_buffer, 'PNG')
    png_bytes = png_buffer.getvalue()
    
    respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put",
            "public_url": "http://mock.cdn/probe.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    )
    respx.put("http://mock.upload/put").mock(return_value=httpx.Response(200))
    respx.get("http://mock.cdn/probe.png").mock(
        return_value=httpx.Response(200, content=png_bytes)
    )
    
    # Mock estimate and submit (succeed)
    respx.post("http://mock.api/estimate/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"credits": "0.09", "usd": "0.09"})
    )
    respx.post("http://mock.api/higgsfield-ai/soul/v2/standard").mock(
        return_value=httpx.Response(200, json={"request_id": "smoke_job_123"})
    )
    
    # Mock status poll (fails with exception)
    respx.get("http://mock.api/requests/smoke_job_123/status").mock(
        side_effect=httpx.HTTPError("Poll failed")
    )
    
    # Check initial budget/credit state
    status_before = await ledger.get_line_status("ep01", "L2_drafts")
    credits_before = await credit_ledger.get_episode_status("ep01")
    
    # Run smoke test (should fail but release holds)
    from hfvg.activities.smoke_test import run_smoke_test
    result = await run_smoke_test("ep01", str(tmp_path / "test.db"))
    
    # Should fail
    assert result["passed"] is False
    assert "Poll failed" in result["generation_probe"]["error"]
    
    # Verify budget and credits were released
    status_after = await ledger.get_line_status("ep01", "L2_drafts")
    credits_after = await credit_ledger.get_episode_status("ep01")
    
    assert status_after["reserved"] == status_before["reserved"], "Budget should be released"
    assert credits_after["reserved"] == credits_before["reserved"], "Credits should be released"
