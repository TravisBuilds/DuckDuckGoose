"""Integration tests for harness canary bugs (Ep05 A01/A02).

Tests that the fixes for the critical bugs found during the live Ep05 canary are working:
1. Still/clip submission uploads local file paths to URLs before submitting to Higgsfield
2. Returns and polls the provider job id, not the harness job id
3. Provider 'failed' status releases reservations and surfaces the error
"""

import asyncio
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
    
    # Mock the Higgsfield API (real shape per live probe)
    # 1. Mock upload URL generation (requires content_type)
    upload_url_route = respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(
            200,
            json={
                "upload_url": "http://mock.upload/put-here",
                "public_url": "http://mock.cdn/abc123.png",
                "upload_headers": {
                    "Content-Type": "image/png",
                    "x-amz-tagging": "retention=temporary"
                }
            }
        )
    )
    
    # 2. Mock the actual upload (empty body per real API)
    upload_route = respx.put("http://mock.upload/put-here").mock(
        return_value=httpx.Response(200, content=b"")
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
    # 1. Upload URL was requested with content_type
    assert upload_url_route.call_count == 1
    upload_request = upload_url_route.calls[0].request
    assert b"content_type" in upload_request.content
    assert b"image/jpeg" in upload_request.content
    
    # 2. File was uploaded with upload_headers
    assert upload_route.call_count == 1
    uploaded_data = upload_route.calls[0].request.content
    assert uploaded_data == b"fake-duck-image-data"
    # Verify upload_headers were sent
    upload_put_headers = upload_route.calls[0].request.headers
    assert upload_put_headers.get("content-type") == "image/png"
    assert "x-amz-tagging" in upload_put_headers
    
    # 3. Submit was called with public_url, not local path
    assert submit_route.call_count == 1
    submit_request = submit_route.calls[0].request
    import json
    submit_body = json.loads(submit_request.content)
    assert "image_urls" in submit_body
    assert len(submit_body["image_urls"]) == 1
    # Should be the public_url from CDN, not the local path
    assert submit_body["image_urls"][0] == "http://mock.cdn/abc123.png"
    assert str(ref_path) not in submit_body["image_urls"][0]
    
    # 4. Result contains harness job_id and provider_job_id
    assert result["job_id"] == "still-ep05-A01-v1"  # Harness job ID
    assert result["provider_job_id"] == "provider_job_xyz789"  # Provider job ID


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
    
    # Verify submit returned both harness and provider job ids
    assert result["job_id"] == "still-ep05-A01-v1"  # Harness job ID
    assert result["provider_job_id"] == "provider_job_different_id"  # Provider job ID
    
    # Now poll using the HARNESS job id (it will be resolved to provider job id internally)
    poll_result = await poll_job_status(
        job_id=result["job_id"],  # Use harness job id (gets resolved to provider id internally)
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
    
    # Poll the failed job (using harness job ID, which gets resolved to provider ID)
    from hfvg.activities.studio_generation import poll_job_status
    poll_result = await poll_job_status(
        job_id=harness_job_id,  # Harness job ID (gets resolved to provider ID internally)
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
@respx.mock
async def test_workflow_polls_with_provider_id_not_harness_id(tmp_path, monkeypatch):
    """
    BUG 2: await_job_enforced must poll with provider ID, not harness ID.
    
    The workflow activities use harness IDs (still-ep-shot-vN), but provider APIs
    require provider IDs. The await_job_enforced activity must look up the provider
    ID from the harness ID before polling.
    
    This test verifies that when poll_job_status is called with a harness ID,
    it correctly resolves to the provider ID and polls the provider API with it.
    """
    # Setup test environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    monkeypatch.setenv("DRY_RUN", "false")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep06", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep06", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep06", "test")
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    budget_ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep06", cap_credits=1000)
    
    # Mock Higgsfield API
    estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
    )
    
    submit_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "provider_still_xyz123"})
    )
    
    # Mock poll with provider ID - this is the critical check
    poll_provider_route = respx.get("http://mock.api/requests/provider_still_xyz123/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "image": {"url": "http://example.com/result.jpg"},
            "cost": 1.12
        })
    )
    
    # Mock poll with harness ID - this should NEVER be called
    poll_harness_route = respx.get("http://mock.api/requests/still-ep06-A01-v1/status").mock(
        return_value=httpx.Response(404, json={"error": "Job not found"})
    )
    
    # Submit and poll - bypass submit by directly persisting the mapping
    # Use actual job_id format from submission
    harness_job_id = "still-ep06-A01-v1"
    provider_job_id = "provider_still_xyz123"
    
    # Reserve and persist mapping manually (skip submit which requires lint)
    await credit_ledger.reserve_credits("ep06", harness_job_id, 1.12, "test")
    await credit_ledger.persist_provider_job(harness_job_id, provider_job_id, "higgsfield")
    
    # Poll with harness ID - it should resolve to provider ID internally
    from hfvg.activities.studio_generation import poll_job_status
    poll_result = await poll_job_status(
        job_id=harness_job_id,
        job_type="still",
        episode_id="ep06"
    )
    
    # CRITICAL ASSERTIONS:
    # The key assertion: Provider API was called with PROVIDER ID, not harness ID
    assert poll_provider_route.call_count >= 1, f"Provider API must be called with provider ID (got {poll_provider_route.call_count} calls)"
    assert poll_harness_route.call_count == 0, f"Provider API must NOT be called with harness ID (got {poll_harness_route.call_count} calls)"
    
    # Secondary check: poll completed successfully
    assert poll_result["status"] == "completed"


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


@pytest.mark.asyncio
@respx.mock
async def test_clip_submit_persists_provider_job_mapping(tmp_path):
    """Test that clip submit persists the provider job mapping for polling."""
    
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
    await ledger.set_line("ep05", "L4_video", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep05", cap_credits=10000)
    
    # Mock Kling API
    # Mock estimate
    estimate_route = respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(
            200,
            json={"credits": "5.0", "usd": "5.0"}
        )
    )
    
    # Mock submit
    submit_route = respx.post("http://mock.api/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(
            200,
            json={"request_id": "provider_clip_xyz789"}
        )
    )
    
    # Mock poll
    poll_route = respx.get("http://mock.api/requests/provider_clip_xyz789/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "video": {"url": "http://example.com/clip.mp4"},
                "cost": 5.0
            }
        )
    )
    
    # Submit clip job
    from hfvg.activities.studio_generation import submit_clip_job_enforced, poll_job_status
    
    result = await submit_clip_job_enforced(
        episode_id="ep05",
        shot_id="A01",
        start_image_url="http://example.com/still.jpg",
        prompt="A duck swimming, locked-off tripod, no camera movement, real-time, no slow motion. Exactly 1 animals in frame. synthetic duck clause: one duck on the water",
        duration=5.0,
        version=1,
        shot_meta={
            "count": 1,
            "exterior": True,
            "water": True,
            "characters": ["duck"]
        },
        model="kling"
    )
    
    # Verify mapping was persisted
    harness_job_id = result["job_id"]
    assert harness_job_id == "clip-ep05-A01-v1"
    
    provider_job_id = await credit_ledger.get_provider_job(harness_job_id)
    assert provider_job_id == "provider_clip_xyz789", "Provider job mapping must be persisted"
    
    # Now poll using the HARNESS job ID - it should resolve to provider ID internally
    poll_result = await poll_job_status(
        job_id=harness_job_id,
        job_type="clip",
        episode_id="ep05",
    )
    
    # Verify poll succeeded and used the correct provider ID
    assert poll_result["status"] == "completed"
    assert poll_result["output_url"] == "http://example.com/clip.mp4"
    assert poll_route.call_count == 1, "Poll should use the provider job ID"



@pytest.mark.asyncio
@respx.mock
async def test_upload_cache_keys_on_ref_version_not_shot_version(tmp_path):
    """
    Test Bug 1: Upload cache must key on ref's own version (from sidecar), not shot version.
    
    A Mochi v3 still (shot version=3) using a ref that's v2 should cache with ref_version=2.
    If shot version were used, a v1 ref would be wrongly cached as v3 and reused for v1 shots.
    """
    # Create ref with version 2 sidecar
    ref_path = tmp_path / "refs" / "r_family.png"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_bytes(b"family-image-v2")
    
    version_sidecar = ref_path.parent / ".versions" / "r_family.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text('{"version": 2, "ref_key": "r_family"}')
    
    # Mock Higgsfield API
    respx.post("http://mock.api/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put-v2",
            "public_url": "http://mock.cdn/family-v2.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    )
    respx.put("http://mock.upload/put-v2").mock(return_value=httpx.Response(200, content=b""))
    respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
    )
    respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "prov_still_v2"})
    )
    
    # Setup environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DRY_RUN"] = "false"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep99", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep99", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep99", "test")
    
    # Initialize budget
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    await ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    await ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    # Initialize credit ledger
    from hfvg.credit_ledger import CreditLedger
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=10000)
    
    # Submit still with Mochi v3 (shot version=3) but ref is v2
    result = await submit_still_job_enforced(
        episode_id="ep99",
        shot_id="A01",
        prompt="A still locked-off tripod, no camera movement. Real-time speed, no slow motion. Exactly 1 animals in frame.",
        refs=[str(ref_path)],
        resolution="1k",
        quality="high",
        aspect_ratio="16:9",
        version=3,  # Shot is using Mochi v3
        tier="final",
        shot_meta={"count": 1, "antler": False, "duck": False}
    )
    
    # Extract job_id from result
    job_id = result["job_id"]
    assert "ep99" in job_id and "A01" in job_id
    
    # Check upload_cache: entry should be keyed with ref_version=2, NOT shot_version=3
    from hfvg.upload_cache import UploadCache
    cache = UploadCache(os.environ["DATABASE_PATH"])
    
    import aiosqlite
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        async with db.execute(
            "SELECT file_path, file_version, public_url FROM upload_cache"
        ) as cursor:
            rows = await cursor.fetchall()
    
    assert len(rows) == 1, "One cache entry expected"
    cached_path, cached_version, cached_url = rows[0]
    
    # CRITICAL: version must be 2 (ref's version), not 3 (shot's version)
    assert cached_version == 2, f"Cache must key on ref version 2, got {cached_version}"
    assert cached_url == "http://mock.cdn/family-v2.png"
    assert str(ref_path) in cached_path


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_uploads_deduplicated(tmp_path):
    """
    Test Bug 2: Concurrent shots uploading the same ref must not upload twice.
    
    Two simultaneous uploads of the same (path, version, sha256) should result in:
    - Only one actual upload to Higgsfield
    - Both callers get the same public_url
    """
    # Create a ref
    ref_path = tmp_path / "refs" / "r_shared.png"
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_bytes(b"shared-ref-content")
    
    # Mock Higgsfield API - track how many times upload is called
    upload_call_count = {"count": 0}
    
    def mock_generate_upload_url(request):
        upload_call_count["count"] += 1
        return httpx.Response(200, json={
            "upload_url": "http://mock.upload/put-shared",
            "public_url": "http://mock.cdn/shared-ref.png",
            "upload_headers": {"Content-Type": "image/png"}
        })
    
    respx.post("http://mock.api/files/generate-upload-url").mock(side_effect=mock_generate_upload_url)
    respx.put("http://mock.upload/put-shared").mock(return_value=httpx.Response(200, content=b""))
    
    # Setup environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    
    # Initialize cache
    from hfvg.upload_cache import UploadCache
    cache = UploadCache(os.environ["DATABASE_PATH"])
    await cache.init_db()
    
    # Simulate two concurrent uploads of the same ref (same path, version, sha256)
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    provider = HiggsfieldStillProvider()
    
    async def upload_task():
        result = await provider.upload_reference(str(ref_path), version=1)
        return result["public_url"]
    
    # Launch two uploads concurrently
    results = await asyncio.gather(upload_task(), upload_task())
    
    # Both should get the same URL
    assert results[0] == results[1] == "http://mock.cdn/shared-ref.png"
    
    # CRITICAL: Only ONE actual upload should have occurred
    assert upload_call_count["count"] == 1, f"Expected 1 upload, got {upload_call_count['count']}"
    
    # Cache should have exactly one entry
    import aiosqlite
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        async with db.execute("SELECT COUNT(*) FROM upload_cache") as cursor:
            row = await cursor.fetchone()
            assert row[0] == 1, "Should have exactly one cache entry"


@pytest.mark.asyncio
async def test_refs_json_without_versions_defaults_to_latest_on_disk(tmp_path):
    """
    Test Bug 3: refs.json without versions map should default to latest version on disk.
    
    If a ref exists as v3 (from sidecar or archive), but refs.json has no versions map,
    loading should default to v3, not v1. This prevents regenerating existing v2/v3 refs.
    """
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    import os
    import json
    
    # Create episode structure
    ep_dir = tmp_path / "data" / "episodes" / "ep88"
    refs_dir = ep_dir / "refs"
    refs_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a v3 ref on disk with sidecar
    ref_file = refs_dir / "r_mochi.png"
    ref_file.write_bytes(b"mochi-v3-content")
    
    version_sidecar = refs_dir / ".versions" / "r_mochi.json"
    version_sidecar.parent.mkdir(parents=True, exist_ok=True)
    version_sidecar.write_text('{"version": 3, "ref_key": "r_mochi"}')
    
    # Also create archived v1 and v2
    v1_dir = refs_dir / "_v1"
    v1_dir.mkdir(parents=True, exist_ok=True)
    (v1_dir / "r_mochi.png").write_bytes(b"mochi-v1")
    
    v2_dir = refs_dir / "_v2"
    v2_dir.mkdir(parents=True, exist_ok=True)
    (v2_dir / "r_mochi.png").write_bytes(b"mochi-v2")
    
    # Create refs.json WITHOUT a versions map
    refs_json = ep_dir / "refs.json"
    refs_json.write_text(json.dumps({
        "to_generate": {
            "r_mochi": str(ref_file)
        },
        "prompts": {
            "r_mochi": "A mochi character"
        }
        # NO "versions" key!
    }))
    
    # Set environment
    os.environ["EPISODES_DIR"] = str(tmp_path / "data" / "episodes")
    
    # Load refs
    refs = await load_refs_to_generate_activity("ep88", shots_subset=None, is_live=False)
    
    # Should have one ref
    assert len(refs) == 1
    ref = refs[0]
    
    # CRITICAL: version should be 3 (from sidecar), not 1
    assert ref["version"] == 3, f"Expected version 3 from sidecar, got {ref['version']}"
    assert ref["key"] == "r_mochi"
    assert ref["output_path"] == str(ref_file)
    
    # Test case 2: No sidecar but archived version exists
    ref2_file = refs_dir / "r_duck.png"
    ref2_file.write_bytes(b"duck-v2-content")
    
    # No sidecar, but v2 archive exists
    (v2_dir / "r_duck.png").write_bytes(b"duck-v1-archived")
    
    refs_json.write_text(json.dumps({
        "to_generate": {
            "r_mochi": str(ref_file),
            "r_duck": str(ref2_file)
        },
        "prompts": {
            "r_mochi": "A mochi character",
            "r_duck": "A duck character"
        }
    }))
    
    refs = await load_refs_to_generate_activity("ep88", shots_subset=None, is_live=False)
    
    duck_ref = [r for r in refs if r["key"] == "r_duck"][0]
    # Duck should be v2 (from archive scan), since current file exists and _v2 exists
    assert duck_ref["version"] == 2, f"Expected version 2 from archive, got {duck_ref['version']}"
