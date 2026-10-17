"""Test anchor still feature."""

import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_set_anchor_still_from_g2_approved(tmp_path):
    """Test setting an anchor still from a G2-approved shot."""
    from hfvg.studio_db import init_studio_db, create_episode, create_shot, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Create episode and shots
    await create_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True, g108_approved=True)
    await create_shot(db_path, "ep99", "A01", status="approved", still_url="https://example.com/a01.png")
    await create_shot(db_path, "ep99", "A02", status="pending")
    await create_shot(db_path, "ep99", "A03", status="pending")
    
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
    from hfvg.studio_db import init_studio_db, create_episode, set_anchor_still, get_anchor_still
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    await create_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True, g108_approved=True)
    
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
async def test_anchor_still_injected_into_refs(tmp_path, respx_mock):
    """Test that anchor still is injected into refs at submit time."""
    import os
    import respx
    import httpx
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, create_episode, set_anchor_still
    from hfvg.credit_ledger import CreditLedger
    from hfvg.budget import BudgetLedger
    
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["DRY_RUN"] = "false"
    os.environ["HIGGSFIELD_API_KEY"] = "test:secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True, g108_approved=True)
    
    # Set up budget and credit ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget_lines("ep99", 60_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Create anchor still file
    anchor_file = tmp_path / "anchor.png"
    anchor_file.write_bytes(b"anchor image")
    
    import hashlib
    sha256 = hashlib.sha256(b"anchor image").hexdigest()
    
    # Set anchor
    await set_anchor_still(
        db_path, "ep99", "A01", ["A02"],
        str(anchor_file), 1, sha256, None
    )
    
    # Mock provider endpoints
    respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "1.25", "credits": "1.25"})
    )
    
    upload_called = {"count": 0, "paths": []}
    def capture_upload(request):
        upload_called["count"] += 1
        # Extract filename from request if possible
        import json
        body = json.loads(request.content)
        return httpx.Response(200, json={"upload_url": "http://upload.example.com", "public_url": f"http://example.com/ref{upload_called['count']}.png"})
    
    respx.post("http://mock.api/upload").mock(side_effect=capture_upload)
    respx.put(url__regex=r"http://upload\.example\.com").mock(return_value=httpx.Response(200))
    
    submit_called = {"count": 0, "refs": []}
    def capture_submit(request):
        import json
        body = json.loads(request.content)
        submit_called["count"] += 1
        submit_called["refs"] = body.get("image_urls", [])
        return httpx.Response(200, json={"job_id": "provider_job_123"})
    
    respx.post("http://mock.api/generate/xai/grok-imagine-image-2.0").mock(side_effect=capture_submit)
    
    # Create a regular ref file
    ref_file = tmp_path / "ref.png"
    ref_file.write_bytes(b"regular ref")
    
    # Submit with refs (anchor should be injected)
    result = await submit_still_job_enforced(
        episode_id="ep99",
        shot_id="A02",
        prompt="Test shot",
        version=1,
        refs=[str(ref_file)],
        resolution="1k",
        quality="medium",
    )
    
    # Verify anchor was uploaded (should be 2 uploads: anchor + regular ref)
    assert upload_called["count"] == 2
    
    # Verify submit received both refs
    assert len(submit_called["refs"]) == 2
    
    # Verify job_id includes anchor version
    assert "anchor1" in result["job_id"]


@pytest.mark.asyncio
async def test_anchor_still_enforces_ref_cap(tmp_path):
    """Test that anchor still + refs respects model ref cap."""
    import os
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, create_episode, set_anchor_still
    from hfvg.credit_ledger import CreditLedger
    from hfvg.budget import BudgetLedger
    
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["DRY_RUN"] = "false"
    os.environ["STILL_MAX_REFS"] = "3"  # Cap at 3 refs
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True, g108_approved=True)
    
    # Set up ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget_lines("ep99", 60_000_000)
    
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
    
    # Try to submit with 3 regular refs (+ anchor = 4 total, over cap)
    ref1 = tmp_path / "ref1.png"
    ref1.write_bytes(b"ref1")
    ref2 = tmp_path / "ref2.png"
    ref2.write_bytes(b"ref2")
    ref3 = tmp_path / "ref3.png"
    ref3.write_bytes(b"ref3")
    
    with pytest.raises(ValueError, match="over the .* limit of 3"):
        await submit_still_job_enforced(
            episode_id="ep99",
            shot_id="A02",
            prompt="Test",
            version=1,
            refs=[str(ref1), str(ref2), str(ref3)],
        )


@pytest.mark.asyncio
async def test_anchor_still_changes_idempotency(tmp_path, respx_mock):
    """Test that changing anchor still changes idempotency key."""
    import os
    import respx
    import httpx
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, create_episode, set_anchor_still
    from hfvg.credit_ledger import CreditLedger
    from hfvg.budget import BudgetLedger
    
    db_path = str(tmp_path / "test.db")
    os.environ["DATABASE_PATH"] = db_path
    os.environ["DRY_RUN"] = "false"
    os.environ["HIGGSFIELD_API_KEY"] = "test:secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99", beatmap_path="test.md", live_mode=True, g108_approved=True)
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget_lines("ep99", 60_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=1000)
    
    # Mock endpoints
    respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"usd": "1.25", "credits": "1.25"})
    )
    respx.post("http://mock.api/upload").mock(
        return_value=httpx.Response(200, json={"upload_url": "http://upload", "public_url": "http://example.com/ref.png"})
    )
    respx.put("http://upload").mock(return_value=httpx.Response(200))
    
    idempotency_keys = []
    def capture_idempotency(request):
        import json
        body = json.loads(request.content)
        idempotency_keys.append(body.get("idempotency_key"))
        return httpx.Response(200, json={"job_id": f"job_{len(idempotency_keys)}"})
    
    respx.post("http://mock.api/generate/xai/grok-imagine-image-2.0").mock(side_effect=capture_idempotency)
    
    # Submit without anchor
    anchor1 = tmp_path / "anchor1.png"
    anchor1.write_bytes(b"anchor version 1")
    import hashlib
    sha1 = hashlib.sha256(b"anchor version 1").hexdigest()
    
    await set_anchor_still(db_path, "ep99", "A01", ["A02"], str(anchor1), 1, sha1, None)
    
    result1 = await submit_still_job_enforced("ep99", "A02", "Test", version=1)
    
    # Change anchor (different content)
    anchor2 = tmp_path / "anchor2.png"
    anchor2.write_bytes(b"anchor version 2 DIFFERENT")
    sha2 = hashlib.sha256(b"anchor version 2 DIFFERENT").hexdigest()
    
    await set_anchor_still(db_path, "ep99", "A01", ["A02"], str(anchor2), 2, sha2, None)
    
    result2 = await submit_still_job_enforced("ep99", "A02", "Test", version=2)
    
    # Verify different idempotency keys
    assert len(idempotency_keys) == 2
    assert idempotency_keys[0] != idempotency_keys[1]
    
    # Verify job_ids are different (include different anchor versions)
    assert result1["job_id"] != result2["job_id"]
    assert "anchor1" in result1["job_id"]
    assert "anchor2" in result2["job_id"]
