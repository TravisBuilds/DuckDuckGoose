"""Live-path still generation test with real DB setup (no budget line mocks)."""

import json
import pytest
import aiosqlite
from pathlib import Path
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_live_still_uses_credit_ledger_only_no_budget_lines(tmp_path, monkeypatch):
    """
    Test: Live still generation uses credit ledger only, does NOT require budget lines.
    
    This test would have caught the ep05 live bug where stills failed with
    "ValueError: Budget line ep05:L2_drafts not found" because budget_lines table was empty.
    
    In live mode:
    - Credit ledger must exist and have credits
    - Budget lines are NOT required (not created, not checked)
    - Only credit reservations/releases happen
    """
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    episode_id = "ep99"
    shot_id = "A01"
    
    # Set up test environment
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("DRY_RUN", "false")  # Live mode
    monkeypatch.chdir(tmp_path)
    
    # Initialize DB
    await init_studio_db(db_path)
    
    # Set up credit ledger with sufficient credits (NO budget lines)
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.add_credits(episode_id, 1000.0, notes="test credits")
    await credit_ledger.set_cap(episode_id, 1000.0)
    
    # Set episode to live mode with G1.08 approved
    import aiosqlite
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute("""
            INSERT OR REPLACE INTO episode_status (episode_id, live_mode, g108_approved)
            VALUES (?, TRUE, TRUE)
        """, (episode_id,))
        await db.commit()
    
    # Verify budget_lines table is EMPTY (simulates live setup that didn't create lines)
    async with aiosqlite.connect(db_path, uri=True) as db:
        async with db.execute("SELECT COUNT(*) FROM budget_lines") as cur:
            count = (await cur.fetchone())[0]
            assert count == 0, "Budget lines table must be empty (simulates live bug)"
    
    # Mock activity info
    original_info = temporalio.activity.info
    temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
    
    # Mock provider to return immediately without real API call
    mock_provider = AsyncMock()
    mock_provider.model_path = "xai/grok-imagine-image-2.0"
    mock_provider.estimate_usd_micros = AsyncMock(return_value=100_000)  # $0.10
    mock_provider.submit_image = AsyncMock(return_value="still-ep99-A01-v1")
    mock_provider.close = AsyncMock()
    
    try:
        with patch('hfvg.activities.studio_generation.HiggsfieldStillProvider', return_value=mock_provider):
            # This should NOT raise "Budget line not found" error
            # It should use credit ledger only and succeed
            result = await submit_still_job_enforced(
                episode_id=episode_id,
                shot_id=shot_id,
                prompt="Test still",
                quality="medium",
                resolution="1k",
                version=1,
                shot_meta={"wet": False},
                missing_refs=[],  # No refs, simplifies test
            )
        
        # Verify result
        assert result["job_id"] == "still-ep99-A01-v1"
        assert result["line_name"] == "L2_drafts"  # Tracked for logging only
        
        # Verify credits were reserved
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute("""
                SELECT tx_type, amount FROM credits WHERE episode_id = ? ORDER BY id
            """, (episode_id,)) as cur:
                rows = await cur.fetchall()
        
        tx_types = [row[0] for row in rows]
        assert "add" in tx_types, "Initial add_credits"
        assert "reserve" in tx_types, "Should have reserved credits"
        
        # Verify budget_lines table is STILL empty (never touched in live mode)
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute("SELECT COUNT(*) FROM budget_lines") as cur:
                count = (await cur.fetchone())[0]
                assert count == 0, "Budget lines must remain untouched in live mode"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_live_still_releases_credits_on_submit_failure(tmp_path, monkeypatch):
    """
    Test: Live still generation releases credit reservation when submit fails before provider.
    
    This test verifies fix for issue 2: credit reservations leaked (4+4 credits)
    when steps failed before provider submit.
    """
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    episode_id = "ep99"
    shot_id = "A01"
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.add_credits(episode_id, 1000.0, notes="test credits")
    await credit_ledger.set_cap(episode_id, 1000.0)
    
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute("""
            INSERT OR REPLACE INTO episode_status (episode_id, live_mode, g108_approved)
            VALUES (?, TRUE, TRUE)
        """, (episode_id,))
        await db.commit()
    
    original_info = temporalio.activity.info
    temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
    
    # Mock provider to fail on submit (simulates network error before provider accepts job)
    mock_provider = AsyncMock()
    mock_provider.model_path = "xai/grok-imagine-image-2.0"
    mock_provider.estimate_usd_micros = AsyncMock(return_value=100_000)
    mock_provider.submit_image = AsyncMock(side_effect=Exception("Network error before submit"))
    mock_provider.close = AsyncMock()
    
    try:
        with patch('hfvg.activities.studio_generation.HiggsfieldStillProvider', return_value=mock_provider):
            # Submit should fail
            with pytest.raises(Exception, match="Network error"):
                await submit_still_job_enforced(
                    episode_id=episode_id,
                    shot_id=shot_id,
                    prompt="Test still",
                    quality="medium",
                    resolution="1k",
                    version=1,
                    shot_meta={"wet": False},
                    missing_refs=[],
                )
        
        # Verify credits were reserved then released (no leak)
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute("""
                SELECT tx_type, amount FROM credits WHERE episode_id = ? ORDER BY id
            """, (episode_id,)) as cur:
                rows = await cur.fetchall()
        
        tx_types = [row[0] for row in rows]
        tx_amounts = [row[1] for row in rows]
        
        assert "add" in tx_types, "Initial add_credits"
        assert "reserve" in tx_types, "Should have reserved credits"
        assert "release" in tx_types, "Should have released credits on failure"
        
        # Net effect: initial add, reserve (negative), release (positive) = back to initial
        # Verify no credit leak
        status = await credit_ledger.get_balance_status(episode_id)
        assert status["available"] == 1000.0, "Credits should be fully released (no leak)"
        
    finally:
        temporalio.activity.info = original_info


@pytest.mark.asyncio
async def test_live_clip_uses_credit_ledger_only_no_budget_lines(tmp_path, monkeypatch):
    """
    Test: Live clip generation uses credit ledger only, does NOT require budget lines.
    
    Same pattern as still test, but for clips (L4_video line would be missing).
    """
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    from hfvg.routing import Route
    import temporalio.activity
    
    episode_id = "ep99"
    shot_id = "A01"
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.chdir(tmp_path)
    
    await init_studio_db(db_path)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.add_credits(episode_id, 1000.0, notes="test credits")
    await credit_ledger.set_cap(episode_id, 1000.0)
    
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute("""
            INSERT OR REPLACE INTO episode_status (episode_id, live_mode, g108_approved)
            VALUES (?, TRUE, TRUE)
        """, (episode_id,))
        await db.commit()
    
    # Verify budget_lines table is EMPTY
    async with aiosqlite.connect(db_path, uri=True) as db:
        async with db.execute("SELECT COUNT(*) FROM budget_lines") as cur:
            count = (await cur.fetchone())[0]
            assert count == 0, "Budget lines table must be empty"
    
    original_info = temporalio.activity.info
    temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf'})()
    
    mock_provider = AsyncMock()
    mock_provider.model_path = "kling-video/v3.0/pro/image-to-video"
    mock_provider.estimate_usd_micros = AsyncMock(return_value=500_000)  # $0.50
    mock_provider.submit_video = AsyncMock(return_value="clip-ep99-A01-v1")
    mock_provider.close = AsyncMock()
    
    # Mock routing
    mock_route = Route(provider="kling", model="kling", reason="test")
    
    try:
        with patch('hfvg.activities.studio_generation.HiggsfieldClipProvider', return_value=mock_provider):
            with patch('hfvg.activities.studio_generation.plan_route', return_value=mock_route):
                result = await submit_clip_job_enforced(
                    episode_id=episode_id,
                    shot_id=shot_id,
                    start_image_url="https://example.com/start.jpg",
                    prompt="Test clip",
                    duration=5.0,
                    version=1,
                    shot_meta={"wet": False, "tags": []},
                    model="kling",
                    missing_refs=[],
                )
        
        assert result["job_id"] == "clip-ep99-A01-v1"
        assert result["line_name"] == "L4_video"  # Tracked for logging only
        
        # Verify credits were reserved
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute("""
                SELECT tx_type FROM credits WHERE episode_id = ?
            """, (episode_id,)) as cur:
                tx_types = [row[0] async for row in cur]
        
        assert "reserve" in tx_types, "Should have reserved credits"
        
        # Verify budget_lines table is STILL empty
        async with aiosqlite.connect(db_path, uri=True) as db:
            async with db.execute("SELECT COUNT(*) FROM budget_lines") as cur:
                count = (await cur.fetchone())[0]
                assert count == 0, "Budget lines must remain untouched in live mode"
        
    finally:
        temporalio.activity.info = original_info
