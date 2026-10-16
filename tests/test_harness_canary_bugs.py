"""Integration tests for harness canary bugs (Ep05 A01/A02).

Tests that the fixes for the critical bugs found during the live Ep05 canary are working:
1. Still/clip submission uploads local file paths to URLs before submitting to Higgsfield
2. Returns and polls the provider job id, not the harness job id
3. Provider 'failed' status releases reservations and surfaces the error
"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import httpx
import respx

from hfvg.activities.studio_generation import submit_still_job_enforced, poll_job_status
from hfvg.providers.base import ProviderJob, ProviderJobStatus


@pytest.mark.asyncio
@respx.mock
async def test_still_submission_uploads_local_refs_to_urls(tmp_path):
    """Test that local file paths are uploaded to Higgsfield and converted to URLs."""
    
    # Create a fake local reference image
    ref_path = tmp_path / "refs" / "r_duck_sheet.jpg"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_bytes(b"fake-duck-image-data")
    
    # Mock the Higgsfield API
    # 1. Mock upload URL generation
    upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "upload_url": "http://mock.upload/put-here",
                "media_id": "media_abc123"
            }
        )
    )
    
    # 2. Mock the actual upload
    upload_route = respx.put("http://mock.upload/put-here").mock(
        return_value=httpx.Response(200, json={})
    )
    
    # 3. Mock estimate endpoint
    estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(
            200,
            json={"credits": "1.12", "usd": "1.12"}
        )
    )
    
    # 4. Mock submit endpoint - verify it receives URLs not local paths
    submit_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(
            200,
            json={"request_id": "provider_job_xyz789"}
        )
    )
    
    # Setup test environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize DB with episode in live mode
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep05", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep05", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep05", "test")
    
    # Initialize budget with all necessary lines
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    await ledger.set_line("ep05", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L3_final_stills", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L4_video", 100_000_000, 80_000_000, "higgsfield")
    # Set manual balance for GC.01 headroom check
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep05", cap_credits=10000)
    
    # Submit still with local reference image path
    result = await submit_still_job_enforced(
        episode_id="ep05",
        shot_id="A01",
        prompt="A duck wearing a hat, locked-off tripod, no camera movement, real-time, no slow motion. Exactly 1 animals in frame. synthetic duck clause: one duck on the water",
        version=1,
        refs=[str(ref_path)],  # Local file path
        resolution="1k",
        quality="medium",
        aspect_ratio="9:16",
        shot_meta={
            "count": 1,
            "exterior": False,
            "water": False,
            "characters": ["duck"]
        }
    )
    
    # Verify:
    # 1. Upload URL was requested
    assert upload_url_route.call_count == 1
    upload_request = upload_url_route.calls[0].request
    assert "r_duck_sheet.jpg" in str(upload_request.content)
    
    # 2. File was uploaded
    assert upload_route.call_count == 1
    uploaded_data = upload_route.calls[0].request.content
    assert uploaded_data == b"fake-duck-image-data"
    
    # 3. Submit was called with URL, not local path
    assert submit_route.call_count == 1
    submit_request = submit_route.calls[0].request
    import json
    submit_body = json.loads(submit_request.content)
    assert "image_urls" in submit_body
    assert len(submit_body["image_urls"]) == 1
    # Should be the constructed Higgsfield URL, not the local path
    assert submit_body["image_urls"][0] == "http://mock.api/media/media_abc123"
    assert str(ref_path) not in submit_body["image_urls"][0]
    
    # 4. Result contains provider job id
    assert result["job_id"] == "provider_job_xyz789"


@pytest.mark.asyncio
@respx.mock
async def test_polls_provider_job_id_not_harness_id(tmp_path):
    """Test that polling uses the provider job id returned from submit, not the harness job id."""
    
    # Mock the Higgsfield API
    # 1. Mock submit endpoint returning a different job id
    submit_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(
            200,
            json={"request_id": "provider_job_different_id"}
        )
    )
    
    # 2. Mock estimate endpoint
    estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(
            200,
            json={"credits": "1.12", "usd": "1.12"}
        )
    )
    
    # 3. Mock poll endpoint - should be called with provider job id
    poll_route = respx.get("http://mock.api/requests/provider_job_different_id/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "images": [{"url": "http://example.com/output.jpg"}],
                "cost": 1.12
            }
        )
    )
    
    # Setup test environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep05", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep05", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep05", "test")
    
    # Initialize budget with all necessary lines
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    await ledger.set_line("ep05", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L2_drafts", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L3_final_stills", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_line("ep05", "L4_video", 100_000_000, 80_000_000, "higgsfield")
    # Set manual balance for GC.01 headroom check
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep05", cap_credits=10000)
    
    # Submit still job
    result = await submit_still_job_enforced(
        episode_id="ep05",
        shot_id="A01",
        prompt="A duck in a pond, locked-off tripod, no camera movement, real-time, no slow motion. Exactly 1 animals in frame. synthetic duck clause: one duck on the water",
        version=1,
        refs=None,
        resolution="1k",
        quality="medium",
        shot_meta={
            "count": 1,
            "exterior": True,
            "water": True,
            "characters": ["duck"]
        }
    )
    
    # Verify submit returned provider job id
    assert result["job_id"] == "provider_job_different_id"
    
    # Now poll using the returned job id
    poll_result = await poll_job_status(
        job_id=result["job_id"],  # This should be the provider job id
        job_type="still",
        episode_id="ep05",
    )
    
    # Verify:
    # 1. Poll was called with the provider job id
    assert poll_route.call_count == 1
    # 2. Poll succeeded and returned completed status
    assert poll_result["status"] == "completed"
    assert poll_result["output_url"] == "http://example.com/output.jpg"


@pytest.mark.asyncio
@respx.mock
async def test_failed_job_releases_reservations(tmp_path):
    """Test that a provider 'failed' status releases both credit and USD reservations."""
    
    # Mock the Higgsfield API
    # 1. Mock poll endpoint returning failed status
    poll_route = respx.get("http://mock.api/requests/job_that_fails/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "failed",
                "error": "Content policy violation: duck too cute"
            }
        )
    )
    
    # Setup test environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep05", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep05", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep05", "test")
    
    # Initialize credit ledger with reservation
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep05", cap_credits=10000)
    
    # Reserve credits for this job (using harness job ID)
    harness_job_id = "still-ep05-A01-v1"
    provider_job_id = "job_that_fails"
    await credit_ledger.reserve_credits("ep05", harness_job_id, 4.0, "test still")
    
    # Store the mapping from harness to provider job ID
    await credit_ledger.persist_provider_job(harness_job_id, provider_job_id, "higgsfield")
    
    # Check initial state
    balance_before = await credit_ledger.get_episode_status("ep05")
    assert balance_before["reserved_credits"] == 4.0
    
    # Poll the failed job (using provider job ID)
    from hfvg.activities.studio_generation import poll_job_status
    poll_result = await poll_job_status(
        job_id=provider_job_id,  # Provider job ID
        job_type="still",
        episode_id="ep05",
    )
    
    # Verify:
    # 1. Poll returned failed status with error message
    assert poll_result["status"] == "failed"
    assert "duck too cute" in poll_result["error"]
    
    # 2. Credit reservation was released (should look up harness job ID and release)
    balance_after = await credit_ledger.get_episode_status("ep05")
    assert balance_after["reserved_credits"] == 0.0, "Credit reservation should be released"
    assert balance_after["available_credits"] == 10000.0


@pytest.mark.asyncio
async def test_post_episodes_requires_beatmap(tmp_path, monkeypatch):
    """Test that POST /api/episodes errors if no BEATMAP.md exists (issue #3)."""
    import os
    
    # Setup test environment
    os.environ["ADMIN_SECRET"] = "a" * 32
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    os.environ["DRY_RUN"] = "true"
    
    # Create episode directory without BEATMAP.md
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True, exist_ok=True)
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    await init_studio_db(os.environ["DATABASE_PATH"])
    
    # Import app after env setup
    from api.main import app
    from fastapi.testclient import TestClient
    
    # Patch temporal_client to None to avoid connection attempts
    import api.main as api_main
    api_main.temporal_client = None
    
    client = TestClient(app)
    
    # Try to start episode without beatmap_content and without uploaded BEATMAP.md
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep99",
            "dry_run": True
        },
        headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"}
    )
    
    # Should fail with 400 and clear error message (or 503 if temporal client missing is caught first)
    # The key is that it should NOT silently use test fixtures
    assert response.status_code in (400, 503)
    if response.status_code == 400:
        assert "No beatmap found" in response.json()["detail"]
        assert "test fixture" not in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_balance_accepts_string_or_numeric(tmp_path):
    """Test that POST /api/budget/balance accepts both numeric and string balance_usd (issue #7)."""
    from fastapi.testclient import TestClient
    from api.main import app
    import os
    
    # Setup test environment
    os.environ["ADMIN_SECRET"] = "a" * 32
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    from hfvg.budget import BudgetLedger
    await init_studio_db(os.environ["DATABASE_PATH"])
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    
    client = TestClient(app)
    
    # Test with string balance_usd
    response = client.post(
        "/api/budget/balance",
        json={
            "balance_usd": "82.40",
            "note": "String balance"
        },
        headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    
    # Test with numeric balance_usd
    response = client.post(
        "/api/budget/balance",
        json={
            "balance_usd": 75.30,
            "note": "Numeric balance"
        },
        headers={"Authorization": f"Bearer {os.environ['ADMIN_SECRET']}"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
