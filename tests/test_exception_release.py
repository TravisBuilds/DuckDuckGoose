"""
Test that reservations are released on all exception paths (P0.1c).

Tests cover:
- submit 500 error
- poll 502 error
- bad start_image (ValueError)
- actual cost > reserved (overage handling)
"""

import pytest
from tests.ledger_helpers import insert_line, set_balance
import asyncio
from unittest.mock import AsyncMock, Mock
import httpx

from hfvg.activities.studio_generation import (
    submit_still_job_enforced,
    submit_clip_job_enforced,
    await_job_enforced,
)
from hfvg.budget import BudgetLedger
from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108


@pytest.fixture
async def test_episode(tmp_path):
    """Create test episode with budget."""
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize budget
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        # Add budget lines
        await insert_line(db, "ep99", "L2_drafts", 10_000_000, 8_000_000)
        
        await insert_line(db, "ep99", "L4_video", 20_000_000, 16_000_000)
        
        await db.commit()
    
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)  # GC.01: live paid jobs need a manual balance
    
    yield db_path


@pytest.mark.asyncio
async def test_still_submit_500_releases_reservation(test_episode, monkeypatch, respx_mock):
    """Test: submit returns 500 → reservation released."""
    import httpx
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_episode)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Mock estimate endpoint
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
    )
    
    # Mock provider to return 500 (POST /{model_path})
    respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(500, json={"error": "Internal server error"})
    )
    
    ledger = BudgetLedger(test_episode)
    await ledger.init_db()
    
    # Get initial reserved
    status_before = await ledger.get_line_status("ep99", "L2_drafts")
    reserved_before = status_before["reserved"]
    
    # Submit should fail and release reservation
    with pytest.raises(httpx.HTTPStatusError):
        await submit_still_job_enforced("ep99", "A01", "Test prompt", 1)
    
    # Check reservation was released
    status_after = await ledger.get_line_status("ep99", "L2_drafts")
    reserved_after = status_after["reserved"]
    
    assert reserved_after == reserved_before, f"Reserved should be unchanged: was {reserved_before}, now {reserved_after}"


@pytest.mark.asyncio
async def test_clip_bad_start_image_releases_reservation(test_episode, monkeypatch, respx_mock):
    """Test: bad start_image URL → reservation released."""
    import httpx
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_episode)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_KLING_VIDEO", "kling-video/v1/videos/image2video")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Mock estimate endpoint
    respx_mock.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.075"})
    )
    
    # Mock provider (won't be called due to early validation)
    respx_mock.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"job_id": "test-123"})
    )
    
    ledger = BudgetLedger(test_episode)
    await ledger.init_db()
    
    # Get initial reserved
    status_before = await ledger.get_line_status("ep99", "L4_video")
    reserved_before = status_before["reserved"]
    
    # Submit with bad URL should fail and release reservation
    with pytest.raises(ValueError, match="Invalid start_image"):
        await submit_clip_job_enforced("ep99", "A01", "not-a-url", "Test prompt", 5.0, 1)
    
    # Check reservation was released
    status_after = await ledger.get_line_status("ep99", "L4_video")
    reserved_after = status_after["reserved"]
    
    assert reserved_after == reserved_before, f"Reserved should be unchanged: was {reserved_before}, now {reserved_after}"


@pytest.mark.asyncio
async def test_poll_502_releases_reservation(test_episode, monkeypatch):
    """Test: poll returns 502 → retries, then releases reservation."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_episode)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    
    # Mock provider that raises on poll
    from unittest.mock import patch, AsyncMock, Mock
    
    # Create a proper httpx exception
    mock_request = Mock()
    mock_request.url = "https://api.higgsfield.ai/status"
    mock_response = Mock()
    mock_response.status_code = 502
    mock_response.text = "Bad Gateway"
    
    mock_provider = AsyncMock()
    mock_provider.get_job_status = AsyncMock(
        side_effect=httpx.HTTPStatusError("Bad Gateway", request=mock_request, response=mock_response)
    )
    mock_provider.close = AsyncMock()
    
    # Mock heartbeat to avoid Temporal context error
    with patch("hfvg.activities.studio_generation.HiggsfieldStillProvider", return_value=mock_provider):
        with patch("hfvg.activities.studio_generation.activity.heartbeat"):
            ledger = BudgetLedger(test_episode)
            await ledger.init_db()
            
            # Reserve first (simulating what submit would do)
            reserved = await ledger.reserve("ep99", "L2_drafts", 4_000_000, "test reserve")
            assert reserved
            
            status_before = await ledger.get_line_status("ep99", "L2_drafts")
            reserved_before = status_before["reserved"]
            assert reserved_before == 4_000_000, "Should have 4_000_000 usd_micros reserved"
            
            # Poll should fail with HTTPStatusError after retries
            with pytest.raises(httpx.HTTPStatusError):
                await await_job_enforced("test-job", "still", "ep99", "A01", "L2_drafts", 4_000_000)
            
            # Verify retries happened (should be called 4 times: initial + 3 retries)
            assert mock_provider.get_job_status.call_count == 4, \
                f"Should retry 3 times after initial failure (4 total calls), got {mock_provider.get_job_status.call_count}"
            
            # Check reservation was released by the exception handler
            status_after = await ledger.get_line_status("ep99", "L2_drafts")
            reserved_after = status_after["reserved"]
            
            assert reserved_after == 0, f"Reserved should be 0 after release: was {reserved_before}, now {reserved_after}"


@pytest.mark.asyncio
async def test_commit_handles_overage(test_episode):
    """Test: actual cost > reserved → commits reserved, logs overage."""
    ledger = BudgetLedger(test_episode)
    await ledger.init_db()
    
    # Reserve 1.00 USD
    reserved = await ledger.reserve("ep99", "L2_drafts", 1_000_000, "test reserve")
    assert reserved
    
    status_before = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_before["reserved"] == 1_000_000
    assert status_before["spent"] == 0
    
    # Commit with actual cost 1.20 USD (overage of 0.20 USD)
    await ledger.commit(
        episode_id="ep99",
        line_name="L2_drafts",
        reserved_usd_micros=1_000_000,
        actual_usd_micros=1_200_000,
        reason="test overage"
    )
    
    # Check: reserved goes to 0, spent goes to 1_200_000 (actual cost, not reserved)
    status_after = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_after["reserved"] == 0, f"Reserved should be 0, got {status_after['reserved']}"
    assert status_after["spent"] == 1_200_000, f"Spent should be 1_200_000 (actual), got {status_after['spent']}"
    
    # Check overage_warning transaction logged
    import aiosqlite
    async with aiosqlite.connect(test_episode) as db:
        async with db.execute(
            "SELECT amount FROM budget_transactions WHERE txn_type = 'overage_warning'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None, "overage_warning transaction should exist"
            assert row[0] == 200_000, f"Overage should be 200_000, got {row[0]}"
        
        # Also check the commit transaction has the actual cost (1_200_000), not reserved (1_000_000)
        async with db.execute(
            "SELECT amount FROM budget_transactions WHERE txn_type = 'commit'"
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None, "commit transaction should exist"
            assert row[0] == 1_200_000, f"Commit should be 1_200_000 (actual cost), got {row[0]}"


@pytest.mark.asyncio
async def test_idempotent_release_safe(test_episode):
    """Test: Release is idempotent and clamps to current reserved amount."""
    ledger = BudgetLedger(test_episode)
    await ledger.init_db()
    
    # Reserve 1.00 USD
    reserved = await ledger.reserve("ep99", "L2_drafts", 1_000_000, "test reserve")
    assert reserved
    
    status_before = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_before["reserved"] == 1_000_000
    
    # Release 0.50 USD (normal)
    await ledger.release(
        episode_id="ep99",
        line_name="L2_drafts",
        amount_usd_micros=500_000,
        reason="partial release"
    )
    
    status_after_first = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_after_first["reserved"] == 500_000, "Should have 500_000 remaining after releasing 500_000"
    
    # Try to release 1_000_000 more (but only 500_000 is reserved) - should clamp to 500_000
    await ledger.release(
        episode_id="ep99",
        line_name="L2_drafts",
        amount_usd_micros=1_000_000,  # Trying to release more than reserved
        reason="over-release attempt"
    )
    
    # Should have released only the remaining 500_000, not go negative
    status_final = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_final["reserved"] == 0, f"Reserved should be 0 (clamped), got {status_final['reserved']}"
    
    # Additional release should do nothing (idempotent)
    await ledger.release(
        episode_id="ep99",
        line_name="L2_drafts",
        amount_usd_micros=500_000,
        reason="double release"
    )
    
    status_idempotent = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_idempotent["reserved"] == 0, "Reserved should remain 0 (idempotent)"
    # Verify we don't go negative
    assert status_idempotent["reserved"] >= 0, "Reserved should never go negative"
