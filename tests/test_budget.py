"""Tests for per-line budget ledger."""

import pytest
import tempfile
import os

from hfvg.budget import BudgetLedger


def create_test_ledger():
    """Create a fresh temporary budget ledger for testing."""
    # Create temp DB with unique name
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    ledger = BudgetLedger(db_path=path)
    return ledger, path


@pytest.mark.asyncio
async def test_init_episode_budget():
    """Test episode budget initialization."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Check Higgsfield lines were created (converted from app credits to API credits)
        # Policy: L1_refs=120 app credits * 0.76 = 91.2 API credits
        l1_status = await ledger.get_line_status("ep04", "L1_refs")
        assert l1_status["budget_cap"] == 91.2  # API credits (from 120 app credits)
        assert l1_status["stop_threshold"] == 72.96  # 80% of 91.2
        assert l1_status["unit"] == "Higgsfield API credits"
        
        # Policy: L4_video=300 app credits * 0.76 = 228 API credits
        l4_status = await ledger.get_line_status("ep04", "L4_video")
        assert l4_status["budget_cap"] == 228.0  # API credits (from 300 app credits)
        assert l4_status["stop_threshold"] == 182.4  # 80% of 228
        
        # Check ElevenLabs lines (no conversion)
        vo_status = await ledger.get_line_status("ep04", "el_vo_takes")
        assert vo_status["budget_cap"] == 700
        assert vo_status["stop_threshold"] == 560  # 80%
        assert vo_status["provider"] == "elevenlabs"
        
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_reserve_commit_flow():
    """Test reserve → commit flow."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Reserve 50 credits from L1
        reserved = await ledger.reserve("ep04", "L1_refs", 50.0, "Test refs")
        assert reserved is True
        
        # Check status
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 0
        assert status["reserved"] == 50
        assert status["total_committed"] == 50
        # available = stop_threshold - total_committed = 72.96 - 50 = 22.96
        assert abs(status["available"] - 22.96) < 0.01  # Use tolerance for float comparison
        
        # Commit 45 actual (slightly under reserve)
        await ledger.commit("ep04", "L1_refs", 45.0, usd_micros=2137500, 
                           reason="Actual refs cost")
        
        # Release the remaining 5 (difference between reserve and actual)
        await ledger.release("ep04", "L1_refs", 5.0, "Refund unused reserve")
        
        # Check status after commit + release
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 45
        assert status["reserved"] == 0  # Released
        assert status["total_committed"] == 45
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_80_percent_stop():
    """Test 80% stop threshold."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Verify line was created correctly (converted from app to API credits)
        # Policy: L1_refs=120 app credits * 0.76 = 91.2 API credits, stop=72.96
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 91.2  # API credits (from 120 app credits)
        assert abs(status["stop_threshold"] - 72.96) < 0.01  # 72.96 with tolerance
        assert status["spent"] == 0
        assert status["reserved"] == 0
        
        # Reserve 70 - should succeed (below stop of 72.96)
        reserved = await ledger.reserve("ep04", "L1_refs", 70.0)
        assert reserved is True
        
        # Verify reserve took effect
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 70
        assert status["at_stop"] is False  # Not at stop yet (70 < 72.96)
        
        # Try to reserve 5 more (total 75, exceeds stop 72.96) - should fail
        reserved = await ledger.reserve("ep04", "L1_refs", 5.0)
        assert reserved is False, "Reserve should fail when exceeding stop"
        
        # Reserve close to the stop threshold (2.5 more, total 72.5 < 72.96)
        reserved = await ledger.reserve("ep04", "L1_refs", 2.5)
        assert reserved is True
        
        # Check status - should not be at stop yet (72.5 < 72.96)
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert abs(status["reserved"] - 72.5) < 0.01
        assert status["at_stop"] is False  # 72.5 < 72.96
        
        # Now reserve exactly to the stop threshold
        # Available = 72.96 - 72.5 = 0.46
        # Reserve 0.46 to hit exactly 72.96
        reserved = await ledger.reserve("ep04", "L1_refs", 0.46)
        assert reserved is True
        
        # Check at_stop flag - should be True when at exactly the stop threshold
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert abs(status["reserved"] - 72.96) < 0.01
        assert status["at_stop"] is True  # At exactly the stop threshold
        
        # Try to reserve more to exceed stop
        reserved = await ledger.reserve("ep04", "L1_refs", 1.0)
        assert reserved is False  # Would exceed stop
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_release_refund():
    """Test release (refund) flow."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Reserve 50
        await ledger.reserve("ep04", "L1_refs", 50.0)
        
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 50
        
        # Release 50 (block or refund)
        await ledger.release("ep04", "L1_refs", 50.0, "Content blocked")
        
        # Check released without committing to spent
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 0
        assert status["reserved"] == 0
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_episode_summary():
    """Test episode budget summary."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Add some spend to multiple lines
        await ledger.reserve("ep04", "L1_refs", 50.0)
        await ledger.commit("ep04", "L1_refs", 50.0)
        
        await ledger.reserve("ep04", "L4_video", 100.0)
        await ledger.commit("ep04", "L4_video", 100.0)
        
        await ledger.reserve("ep04", "el_vo_takes", 200.0)
        await ledger.commit("ep04", "el_vo_takes", 200.0)
        
        # Get summary
        summary = await ledger.get_episode_summary("ep04")
        
        assert summary["episode_id"] == "ep04"
        assert summary["higgsfield_total"] == 150  # 50 + 100
        assert summary["elevenlabs_total"] == 200
        assert len(summary["lines"]) > 0
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_hard_cap_enforcement():
    """Test hard cap cannot be exceeded."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Verify line was created correctly (converted from app to API credits)
        # Policy: L1_refs=120 app credits * 0.76 = 91.2 API credits
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 91.2  # API credits (from 120 app credits)
        
        # Try to reserve 100 (exceeds hard cap of 91.2) - should raise ValueError
        exception_raised = False
        error_message = ""
        try:
            await ledger.reserve("ep04", "L1_refs", 100.0)
        except ValueError as e:
            exception_raised = True
            error_message = str(e)
        
        # MUST raise ValueError (not just return False)
        assert exception_raised, \
            "Hard cap check must raise ValueError, not silently return False"
        assert "cap exceeded" in error_message.lower(), \
            f"Error should mention cap exceeded, got: {error_message}"
        
        # Verify nothing was reserved
        status_after = await ledger.get_line_status("ep04", "L1_refs")
        assert status_after["reserved"] == 0.0
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


@pytest.mark.asyncio
async def test_conversion_constant_is_076():
    """
    Test: APP_TO_API_CREDIT_CONVERSION constant is 0.76.
    
    M-R4a will change it to 1.0 - this test must fail.
    """
    from hfvg.budget import APP_TO_API_CREDIT_CONVERSION
    
    assert APP_TO_API_CREDIT_CONVERSION == 0.76, \
        "Conversion constant must be 0.76 (from $0.0475 app / $0.0625 API)"


@pytest.mark.asyncio
async def test_episode_cap_converted():
    """
    Test: EPISODE_CAP is converted to API credits (950.0 from 1,250 app credits).
    
    M-R4b will leave it at 1250 - this test must fail.
    """
    from hfvg.budget import EPISODE_CAP
    
    # First check the constant itself
    assert EPISODE_CAP == 950.0, \
        f"EPISODE_CAP must be 950.0 API credits (1,250 app * 0.76), got {EPISODE_CAP}"
    
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Try to reserve 951.0 credits (exceeds 950.0 cap)
        try:
            await ledger.reserve("ep04", "L1_refs", 951.0, "Test over cap")
            assert False, "Should have raised ValueError for exceeding 950.0 episode cap"
        except ValueError as e:
            # Must explicitly mention the cap value
            error_msg = str(e).lower()
            assert "950" in error_msg or "episode cap" in error_msg, \
                f"Error must mention 950 or episode cap, got: {e}"
            # Must NOT mention 1250 (the old app credit cap)
            assert "1250" not in str(e) and "1,250" not in str(e), \
                f"Error must not mention 1,250 (old app credit cap), got: {e}"
    
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
