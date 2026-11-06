"""Database migrations for user identity system.

Adds user_id columns to existing tables and tags legacy data.
"""

import aiosqlite

from hfvg.users import LEGACY_USER_ID


async def migrate_add_user_tracking(db_path: str):
    """
    Add user_id columns to episodes, jobs, and transactions.
    Tag existing rows with 'legacy/shared' user_id.
    """
    async with aiosqlite.connect(db_path, uri=True) as db:
        # Check and add user_id columns
        
        # Episodes table
        async with db.execute("PRAGMA table_info(episodes)") as cur:
            cols = {r[1] async for r in cur}
        if "user_id" not in cols:
            await db.execute("""
                ALTER TABLE episodes ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
            """)
            await db.execute("""
                UPDATE episodes SET user_id = 'legacy/shared' WHERE user_id IS NULL
            """)
        
        # Job ledger table
        async with db.execute("PRAGMA table_info(job_ledger)") as cur:
            cols = {r[1] async for r in cur}
        if "user_id" not in cols:
            await db.execute("""
                ALTER TABLE job_ledger ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
            """)
            await db.execute("""
                UPDATE job_ledger SET user_id = 'legacy/shared' WHERE user_id IS NULL
            """)
        
        # Budget transactions table
        async with db.execute("PRAGMA table_info(budget_transactions)") as cur:
            cols = {r[1] async for r in cur}
        if "user_id" not in cols:
            await db.execute("""
                ALTER TABLE budget_transactions ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
            """)
            await db.execute("""
                UPDATE budget_transactions SET user_id = 'legacy/shared' WHERE user_id IS NULL
            """)
        
        # Create indexes for user_id queries
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_episodes_user 
            ON episodes(user_id, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_job_ledger_user 
            ON job_ledger(user_id, created_at)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_budget_transactions_user 
            ON budget_transactions(user_id, timestamp)
        """)
        
        await db.commit()


async def run_all_migrations(db_path: str):
    """Run all pending migrations."""
    await migrate_add_user_tracking(db_path)
