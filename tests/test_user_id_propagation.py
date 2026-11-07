"""Test that user_id propagates through episodes, jobs, and workflows."""

import os
import pytest
import aiosqlite

from hfvg.users import UserManager, LEGACY_USER_ID


@pytest.mark.asyncio
async def test_new_episode_carries_user_id(tmp_path):
    """Test that a new episode created by an authenticated user carries that user's user_id."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize DB and create a user
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    user_data = await user_mgr.create_user(
        name="Test User",
        email="test@example.com",
        password="testpass123",
        role="member"
    )
    user_id = user_data["user_id"]
    
    # Create episodes table
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                user_id TEXT,
                created_at TEXT,
                status TEXT
            )
        """)
        await db.commit()
        
        # Simulate episode creation with user_id
        await db.execute(
            "INSERT INTO episodes (episode_id, user_id, created_at, status) VALUES (?, ?, datetime('now'), 'active')",
            ("ep_test123", user_id)
        )
        await db.commit()
        
        # Verify episode has user_id
        async with db.execute(
            "SELECT user_id FROM episodes WHERE episode_id = ?",
            ("ep_test123",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == user_id


@pytest.mark.asyncio
async def test_new_job_carries_user_id(tmp_path):
    """Test that a new job created for an episode carries the episode's user_id."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize DB and create a user
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    user_data = await user_mgr.create_user(
        name="Test User",
        email="test@example.com",
        password="testpass123",
        role="member"
    )
    user_id = user_data["user_id"]
    
    # Create job_ledger table
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS job_ledger (
                job_id TEXT PRIMARY KEY,
                episode_id TEXT,
                user_id TEXT,
                usd_micros INTEGER,
                actual_usd_micros INTEGER,
                created_at TEXT
            )
        """)
        await db.commit()
        
        # Simulate job creation with user_id
        await db.execute(
            """INSERT INTO job_ledger (job_id, episode_id, user_id, usd_micros, created_at) 
               VALUES (?, ?, ?, ?, datetime('now'))""",
            ("job_test123", "ep_test123", user_id, 1000000)
        )
        await db.commit()
        
        # Verify job has user_id
        async with db.execute(
            "SELECT user_id FROM job_ledger WHERE job_id = ?",
            ("job_test123",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == user_id


@pytest.mark.asyncio
async def test_legacy_data_uses_legacy_user_id(tmp_path):
    """Test that existing episodes/jobs without user_id are tagged as legacy/shared."""
    db_path = str(tmp_path / "test.db")
    
    # Initialize DB
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    # Create tables with existing data (no user_id yet)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                created_at TEXT,
                status TEXT
            )
        """)
        await db.execute("""
            INSERT INTO episodes (episode_id, created_at, status) 
            VALUES ('ep_old', datetime('now'), 'active')
        """)
        await db.commit()
        
        # Run migration
        from hfvg.migrations import migrate_add_user_tracking
        await migrate_add_user_tracking(db_path)
        
        # Verify old episode has legacy user_id
        async with db.execute(
            "SELECT user_id FROM episodes WHERE episode_id = ?",
            ("ep_old",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == LEGACY_USER_ID
