"""Test episode live mode and G1.08 approval workflow."""

import pytest


@pytest.mark.asyncio
async def test_upsert_episode_sets_live_mode(tmp_path):
    """Test: upsert_episode sets live_mode based on dry_run parameter."""
    from hfvg.studio_db import init_studio_db, upsert_episode
    import aiosqlite
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Dry run: live_mode = 0
    await upsert_episode(db_path, "ep99", beatmap_path="/path/to/beat.md", live_mode=False)
    
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 0  # live_mode = 0 (dry run)
        assert row[1] == 0  # g108_approved = 0 (not yet approved)
    
    # Live run: live_mode = 1
    await upsert_episode(db_path, "ep99", beatmap_path="/path/to/beat.md", live_mode=True)
    
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT live_mode FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # live_mode = 1 (live)


@pytest.mark.asyncio
async def test_g108_approved_on_g1_approval(tmp_path):
    """Test: G1.08 is approved when G1_SHOT_PLAN is approved."""
    from hfvg.studio_db import init_studio_db, upsert_episode, approve_g108
    import aiosqlite
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    # Create episode in live mode
    await upsert_episode(db_path, "ep99", beatmap_path="/path/to/beat.md", live_mode=True)
    
    # Initially g108_approved = 0
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT g108_approved FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 0
    
    # Approve G1.08 (called when G1_SHOT_PLAN is approved)
    await approve_g108(db_path, "ep99", user="workflow")
    
    # Now g108_approved = 1
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT g108_approved FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 1


@pytest.mark.asyncio
async def test_upsert_episode_activity(tmp_path, monkeypatch):
    """Test: upsert_episode_activity creates episode row with correct live_mode."""
    from hfvg.activities.workflow_support import upsert_episode_activity
    from hfvg.studio_db import init_studio_db
    import aiosqlite
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    await init_studio_db(db_path)
    
    # Test dry run
    await upsert_episode_activity("ep99", "/path/to/beat.md", dry_run=True)
    
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT live_mode FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 0  # dry run -> live_mode = 0
    
    # Test live run (upsert updates)
    await upsert_episode_activity("ep99", "/path/to/beat.md", dry_run=False)
    
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT live_mode FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 1  # live run -> live_mode = 1


@pytest.mark.asyncio
async def test_approve_g108_activity(tmp_path, monkeypatch):
    """Test: approve_g108_activity sets g108_approved flag."""
    from hfvg.activities.workflow_support import approve_g108_activity
    from hfvg.studio_db import init_studio_db, upsert_episode
    import aiosqlite
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    await init_studio_db(db_path)
    await upsert_episode(db_path, "ep99", live_mode=True)
    
    # Before approval
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT g108_approved FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 0
    
    # Approve
    await approve_g108_activity("ep99")
    
    # After approval
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT g108_approved FROM episodes WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 1
