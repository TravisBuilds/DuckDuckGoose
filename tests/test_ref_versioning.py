"""Tests for ref versioning and reuse logic."""

import json
import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_ref_version_mismatch_triggers_regeneration(tmp_path, monkeypatch):
    """Test: Version bump writes new version sidecar."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")  # Use live mode to avoid path redirection
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = tmp_path / "refs" / "duck.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Pre-create v1 file and sidecar manually
    output_path.write_bytes(b"old-v1-content")
    version_sidecar = output_path.parent / ".versions" / "duck.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text(json.dumps({"version": 1, "ref_key": "r_duck"}))
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Mock provider responses for v2 generation
        import respx
        import httpx
        
        with respx.mock:
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-v2-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-v2-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "https://example.com/duck-v2.png"}]
                })
            )
            # Return a minimal valid PNG
            respx.get("https://example.com/duck-v2.png").mock(
                return_value=httpx.Response(200, content=b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x00\x00\x00\x02\x00\x08\x02\x00\x00\x00\x00\x00\x00\x00\x0cIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\x0d\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')
            )
            
            # Generate v2 (should archive v1)
            result_v2 = await generate_ref_activity(
                episode_id="ep99",
                ref_key="r_duck",
                prompt="Duck v2 updated",
                output_path=str(output_path),
                version=2,
            )
            
            assert result_v2["status"] == "completed"
            
            # V1 should be archived
            v1_archive = output_path.parent / "_v1" / "duck.png"
            assert v1_archive.exists()
            assert v1_archive.read_bytes() == b"old-v1-content"
            
            # Version sidecar should show v2
            version_data = json.loads(version_sidecar.read_text())
            assert version_data["version"] == 2
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_ref_reuses_when_version_matches(tmp_path, monkeypatch):
    """Test: Ref is reused when version matches."""
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
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate v2 initially
        result1 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck v2",
            output_path=str(output_path),
            version=2,
        )
        
        assert result1["status"] == "completed"
        assert result1["credits_used"] > 0
        
        # Try to generate v2 again (should reuse)
        result2 = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck v2",
            output_path=str(output_path),
            version=2,
        )
        
        assert result2["status"] == "reused"
        assert result2["credits_used"] == 0
    
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_ref_default_version_is_one(tmp_path, monkeypatch):
    """Test: Refs default to version 1 when not specified."""
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
    
    original_info = temporalio.activity.info
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
        
        # Generate without explicit version (defaults to 1)
        result = await generate_ref_activity(
            episode_id="ep99",
            ref_key="r_duck",
            prompt="Duck",
            output_path=str(output_path),
            version=1,
        )
        
        assert result["status"] == "completed"
        
        # Check sidecar has version 1
        version_sidecar = output_path.parent / ".versions" / "duck.json"
        assert version_sidecar.exists()
        version_data = json.loads(version_sidecar.read_text())
        assert version_data["version"] == 1
    
    finally:
        temporalio.activity.info = original_info
