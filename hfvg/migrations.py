"""Database migrations for the studio DB.

- user identity: adds user_id columns to existing tables and tags legacy data.
- cast library (``CAST_LIBRARY_MIGRATION``): series, episode-series links, characters, versions,
  reference images, sheet jobs and the cast audit log. New tables only; existing tables and rows are
  not touched. Recorded in ``schema_migrations`` and safe to run again.
"""

import sqlite3
from datetime import datetime, timezone

import aiosqlite

from hfvg.users import LEGACY_USER_ID

CAST_LIBRARY_MIGRATION = "2026-11-cast-library-v1"

SCHEMA_MIGRATIONS_SQL = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
"""

CAST_LIBRARY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS series (
        series_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        owner_user_id TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS episode_series (
        episode_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        linked_at TEXT NOT NULL,
        linked_by TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cast_characters (
        character_id TEXT PRIMARY KEY,
        library_kind TEXT NOT NULL CHECK (library_kind IN ('series', 'episode')),
        library_id TEXT NOT NULL,
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (library_kind, library_id, slug)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cast_versions (
        character_id TEXT NOT NULL REFERENCES cast_characters(character_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        label TEXT NOT NULL DEFAULT '',
        outfit TEXT NOT NULL DEFAULT '',
        derived_from_version INTEGER,
        sheet_stage TEXT NOT NULL DEFAULT 'none',
        locked INTEGER NOT NULL DEFAULT 0,
        locked_at TEXT,
        locked_by TEXT,
        lock_hash TEXT,
        locked_description TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (character_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cast_images (
        image_id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL REFERENCES cast_characters(character_id),
        version INTEGER NOT NULL,
        role TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        sha256 TEXT NOT NULL,
        ext TEXT NOT NULL,
        mime TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 0,
        placeholder INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        job_id TEXT,
        original_filename TEXT,
        attestation TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (character_id, version, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cast_images_sha ON cast_images(sha256)",
    """
    CREATE TABLE IF NOT EXISTS cast_sheet_jobs (
        job_id TEXT PRIMARY KEY,
        character_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        stage TEXT NOT NULL,
        view TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        episode_id TEXT NOT NULL,
        workflow_id TEXT,
        status TEXT NOT NULL,
        estimated_usd_micros INTEGER,
        image_id TEXT,
        error TEXT,
        requested_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cast_sheet_jobs_version ON cast_sheet_jobs(character_id, version)",
    """
    CREATE TABLE IF NOT EXISTS cast_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        actor TEXT,
        action TEXT NOT NULL,
        series_id TEXT,
        episode_id TEXT,
        character_id TEXT,
        image_id TEXT,
        details TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cast_audit_character ON cast_audit_log(character_id, id)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _table_exists(db, table_name: str) -> bool:
    """Check if a table exists in the database."""
    async with db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def migrate_add_user_tracking(db_path: str):
    """
    Add user_id columns to episodes, jobs, and transactions.
    Tag existing rows with 'legacy/shared' user_id.
    """
    async with aiosqlite.connect(db_path, uri=True) as db:
        # Check and add user_id columns
        
        # Episodes table (only if it exists)
        if await _table_exists(db, "episodes"):
            async with db.execute("PRAGMA table_info(episodes)") as cur:
                cols = {r[1] async for r in cur}
            if "user_id" not in cols:
                await db.execute("""
                    ALTER TABLE episodes ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
                """)
                await db.execute("""
                    UPDATE episodes SET user_id = 'legacy/shared' WHERE user_id IS NULL
                """)
        
        # Job ledger table (only if it exists)
        if await _table_exists(db, "job_ledger"):
            async with db.execute("PRAGMA table_info(job_ledger)") as cur:
                cols = {r[1] async for r in cur}
            if "user_id" not in cols:
                await db.execute("""
                    ALTER TABLE job_ledger ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
                """)
                await db.execute("""
                    UPDATE job_ledger SET user_id = 'legacy/shared' WHERE user_id IS NULL
                """)
        
        # Budget transactions table (only if it exists)
        if await _table_exists(db, "budget_transactions"):
            async with db.execute("PRAGMA table_info(budget_transactions)") as cur:
                cols = {r[1] async for r in cur}
            if "user_id" not in cols:
                await db.execute("""
                    ALTER TABLE budget_transactions ADD COLUMN user_id TEXT DEFAULT 'legacy/shared'
                """)
                await db.execute("""
                    UPDATE budget_transactions SET user_id = 'legacy/shared' WHERE user_id IS NULL
                """)
        
        # Create indexes for user_id queries (only for existing tables)
        if await _table_exists(db, "episodes"):
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_episodes_user 
                ON episodes(user_id, created_at)
            """)
        if await _table_exists(db, "job_ledger"):
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_job_ledger_user 
                ON job_ledger(user_id, created_at)
            """)
        if await _table_exists(db, "budget_transactions"):
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_budget_transactions_user 
                ON budget_transactions(user_id, timestamp)
            """)
        
        await db.commit()


def apply_cast_library_sync(conn: sqlite3.Connection) -> bool:
    """Apply the cast library migration on an open sqlite3 connection; True when it ran now.
    Used by processes that open the cast tables without the API's startup (the worker, tools)."""
    conn.execute(SCHEMA_MIGRATIONS_SQL)
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (CAST_LIBRARY_MIGRATION,)).fetchone():
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in CAST_LIBRARY_SQL:
            conn.execute(statement)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                     (CAST_LIBRARY_MIGRATION, _now_iso()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


async def migrate_cast_library(db_path: str) -> bool:
    """The cast library tables (CAST_LIBRARY_MIGRATION); True when it ran now, False when applied."""
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute(SCHEMA_MIGRATIONS_SQL)
        async with db.execute("SELECT 1 FROM schema_migrations WHERE version = ?",
                              (CAST_LIBRARY_MIGRATION,)) as cur:
            if await cur.fetchone():
                return False
        for statement in CAST_LIBRARY_SQL:
            await db.execute(statement)
        await db.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                         (CAST_LIBRARY_MIGRATION, _now_iso()))
        await db.commit()
    return True


async def run_all_migrations(db_path: str):
    """Run all pending migrations."""
    await migrate_add_user_tracking(db_path)
    await migrate_cast_library(db_path)
