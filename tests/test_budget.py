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
        
        # Check Higgsfield lines were created
        l1_status = await ledger.get_line_status("ep04", "L1_refs")
        assert l1_status["budget_cap"] == 120
        assert l1_status["stop_threshold"] == 96  # 80%
        assert l1_status["unit"] == "Higgsfield app credits"
        
        l4_status = await ledger.get_line_status("ep04", "L4_video")
        assert l4_status["budget_cap"] == 300
        assert l4_status["stop_threshold"] == 240  # 80%
        
        # Check ElevenLabs lines
        vo_status = await ledger.get_line_status("ep04", "el_vo_takes")
        assert vo_status["budget_cap"] == 700
        assert vo_status["stop_threshold"] == 560  # 80%
        assert vo_status["provider"] == "elevenlabs"
        
    finally:
        try:
            os.unlink(path)
        except:
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
        assert status["available"] == 46  # 96 - 50
        
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
        except:
            pass


@pytest.mark.asyncio
async def test_80_percent_stop():
    """Test 80% stop threshold."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Verify line was created correctly
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 120
        assert status["stop_threshold"] == 96
        assert status["spent"] == 0
        assert status["reserved"] == 0
        
        # Reserve 90 - should succeed (below stop of 96)
        reserved = await ledger.reserve("ep04", "L1_refs", 90.0)
        assert reserved is True
        
        # Verify reserve took effect
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 90
        assert status["at_stop"] is False  # Not at stop yet (90 < 96)
        
        # Try to reserve 10 more (total 100, exceeds stop 96) - should fail
        reserved = await ledger.reserve("ep04", "L1_refs", 10.0)
        assert reserved is False, "Reserve should fail when exceeding stop"
        
        # Reserve right up to the stop threshold (6 more to reach exactly 96)
        reserved = await ledger.reserve("ep04", "L1_refs", 6.0)
        assert reserved is True
        
        # Now check at_stop flag - should be True (exactly at stop)
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 96
        assert status["at_stop"] is True
    
    finally:
        try:
            os.unlink(path)
        except:
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
        except:
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
        except:
            pass


@pytest.mark.asyncio
async def test_hard_cap_enforcement():
    """Test hard cap cannot be exceeded."""
    ledger, path = create_test_ledger()
    
    try:
        await ledger.init_episode_budget("ep04")
        
        # Verify line was created correctly
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 120
        
        # Try to reserve 130 (exceeds hard cap) - should raise ValueError
        exception_raised = False
        error_message = ""
        try:
            await ledger.reserve("ep04", "L1_refs", 130.0)
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
        except:
            pass
