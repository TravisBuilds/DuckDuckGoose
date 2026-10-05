"""Tests for per-line budget ledger."""

import pytest
import tempfile
import os

from hfvg.budget import BudgetLedger


@pytest.fixture
async def budget_ledger():
    """Create a temporary budget ledger for testing."""
    # Create temp DB
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    ledger = BudgetLedger(db_path=f"file:{path}?mode=memory&cache=shared")
    await ledger.init_episode_budget("ep04")
    
    yield ledger
    
    # Cleanup
    try:
        os.unlink(path)
    except:
        pass


@pytest.mark.asyncio
async def test_init_episode_budget():
    """Test episode budget initialization."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    try:
        ledger = BudgetLedger(db_path=path)
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
async def test_reserve_commit_flow(budget_ledger):
    """Test reserve → commit flow."""
    # Reserve 50 credits from L1
    reserved = await budget_ledger.reserve("ep04", "L1_refs", 50.0, "Test refs")
    assert reserved is True
    
    # Check status
    status = await budget_ledger.get_line_status("ep04", "L1_refs")
    assert status["spent"] == 0
    assert status["reserved"] == 50
    assert status["total_committed"] == 50
    assert status["available"] == 46  # 96 - 50
    
    # Commit 45 actual (slightly under reserve)
    await budget_ledger.commit("ep04", "L1_refs", 45.0, usd_micros=2137500, 
                              reason="Actual refs cost")
    
    # Check status after commit
    status = await budget_ledger.get_line_status("ep04", "L1_refs")
    assert status["spent"] == 45
    assert status["reserved"] == 0  # Released
    assert status["total_committed"] == 45


@pytest.mark.asyncio
async def test_80_percent_stop(budget_ledger):
    """Test 80% stop threshold."""
    # L1_refs: cap 120, stop 96
    
    # Reserve 90 - should succeed
    reserved = await budget_ledger.reserve("ep04", "L1_refs", 90.0)
    assert reserved is True
    
    # Try to reserve 10 more (total 100, exceeds stop 96) - should fail
    reserved = await budget_ledger.reserve("ep04", "L1_refs", 10.0)
    assert reserved is False
    
    # Check at_stop flag
    status = await budget_ledger.get_line_status("ep04", "L1_refs")
    assert status["at_stop"] is True


@pytest.mark.asyncio
async def test_release_refund(budget_ledger):
    """Test release (refund) flow."""
    # Reserve 50
    await budget_ledger.reserve("ep04", "L1_refs", 50.0)
    
    status = await budget_ledger.get_line_status("ep04", "L1_refs")
    assert status["reserved"] == 50
    
    # Release 50 (block or refund)
    await budget_ledger.release("ep04", "L1_refs", 50.0, "Content blocked")
    
    # Check released without committing to spent
    status = await budget_ledger.get_line_status("ep04", "L1_refs")
    assert status["spent"] == 0
    assert status["reserved"] == 0


@pytest.mark.asyncio
async def test_episode_summary(budget_ledger):
    """Test episode budget summary."""
    # Add some spend to multiple lines
    await budget_ledger.reserve("ep04", "L1_refs", 50.0)
    await budget_ledger.commit("ep04", "L1_refs", 50.0)
    
    await budget_ledger.reserve("ep04", "L4_video", 100.0)
    await budget_ledger.commit("ep04", "L4_video", 100.0)
    
    await budget_ledger.reserve("ep04", "el_vo_takes", 200.0)
    await budget_ledger.commit("ep04", "el_vo_takes", 200.0)
    
    # Get summary
    summary = await budget_ledger.get_episode_summary("ep04")
    
    assert summary["episode_id"] == "ep04"
    assert summary["higgsfield_total"] == 150  # 50 + 100
    assert summary["elevenlabs_total"] == 200
    assert len(summary["lines"]) > 0


@pytest.mark.asyncio
async def test_hard_cap_enforcement(budget_ledger):
    """Test hard cap cannot be exceeded."""
    # L1_refs: cap 120
    
    # Try to reserve 130 (exceeds hard cap) - should raise
    with pytest.raises(ValueError, match="Budget cap exceeded"):
        await budget_ledger.reserve("ep04", "L1_refs", 130.0)
