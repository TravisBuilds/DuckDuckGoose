"""Database migrations for the studio DB.

- user identity: adds user_id columns to existing tables and tags legacy data.
- cast library (``CAST_LIBRARY_MIGRATION``): series, episode-series links, characters, versions,
  reference images, sheet jobs and the cast audit log. New tables only; existing tables and rows are
  not touched. Recorded in ``schema_migrations`` and safe to run again.
- props library (``PROPS_LIBRARY_MIGRATION``): props and places with their reference photos, and the
  history of shot plan edits (prompt text and per-shot picks). New tables only, like the cast library.
- universe (``UNIVERSE_MIGRATION``, docs/WORLDS.md): a series' Universe profile (logline, story
  bible, style notes, card, cover, narrator voice), each character's saved voice, the creation flow's
  reference notes and photos, and the mood board versions with their images and approval. New tables
  only; a series without a profile row reads as an empty profile.
- universe board requests (``UNIVERSE_BOARD_MIGRATION``, docs/WORLDS.md): a live mood board the
  worker is making (or failed to make) and its paid picture jobs. The board itself lands in the
  universe tables when every picture is made. New tables only.
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

PROPS_LIBRARY_MIGRATION = "2026-11-props-library-v1"

PROPS_LIBRARY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS prop_items (
        prop_id TEXT PRIMARY KEY,
        library_kind TEXT NOT NULL CHECK (library_kind IN ('series', 'episode')),
        library_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('prop', 'place')),
        slug TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (library_kind, library_id, slug)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prop_images (
        image_id TEXT PRIMARY KEY,
        prop_id TEXT NOT NULL REFERENCES prop_items(prop_id),
        status TEXT NOT NULL DEFAULT 'active',
        sha256 TEXT NOT NULL,
        ext TEXT NOT NULL,
        mime TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        is_primary INTEGER NOT NULL DEFAULT 0,
        original_filename TEXT,
        attestation TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (prop_id, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prop_images_sha ON prop_images(sha256)",
    """
    CREATE TABLE IF NOT EXISTS shot_plan_edits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id TEXT NOT NULL,
        shot_id TEXT NOT NULL,
        field TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        actor TEXT,
        actor_name TEXT,
        run_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_shot_plan_edits_episode ON shot_plan_edits(episode_id, shot_id, id)",
)

UNIVERSE_MIGRATION = "2026-11-world-profile-v1"

UNIVERSE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS series_profiles (
        series_id TEXT PRIMARY KEY REFERENCES series(series_id),
        logline TEXT NOT NULL DEFAULT '',
        bible TEXT NOT NULL DEFAULT '',
        style_notes TEXT NOT NULL DEFAULT '',
        preset_id TEXT,
        cover_image_id TEXT,
        narrator_voice_id TEXT,
        narrator_voice_name TEXT,
        narrator_voice_description TEXT NOT NULL DEFAULT '',
        narrator_preview_url TEXT,
        bible_board_id TEXT,
        bible_generated_at TEXT,
        updated_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS character_voices (
        character_id TEXT PRIMARY KEY REFERENCES cast_characters(character_id),
        description TEXT NOT NULL DEFAULT '',
        voice_id TEXT NOT NULL,
        voice_name TEXT NOT NULL DEFAULT '',
        preview_url TEXT,
        chosen_by TEXT,
        chosen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_notes (
        note_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        topic TEXT NOT NULL CHECK (topic IN ('premise', 'characters', 'places', 'tone')),
        body TEXT NOT NULL,
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_notes_series ON universe_notes(series_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS universe_photos (
        image_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        status TEXT NOT NULL DEFAULT 'active',
        sha256 TEXT NOT NULL,
        ext TEXT NOT NULL,
        mime TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        caption TEXT NOT NULL DEFAULT '',
        original_filename TEXT,
        attestation TEXT,
        created_by TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (series_id, sha256)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_photos_sha ON universe_photos(sha256)",
    """
    CREATE TABLE IF NOT EXISTS universe_boards (
        board_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        version INTEGER NOT NULL CHECK (version >= 1),
        status TEXT NOT NULL CHECK (status IN ('ready', 'approved', 'redo_requested')),
        prompt TEXT NOT NULL,
        answers_feedback TEXT NOT NULL DEFAULT '',
        redo_feedback TEXT,
        sources TEXT NOT NULL DEFAULT '{}',
        image_count INTEGER NOT NULL,
        estimate_usd_micros INTEGER NOT NULL,
        approved_total_usd_micros INTEGER NOT NULL,
        dry_run INTEGER NOT NULL,
        requested_by TEXT,
        decided_by TEXT,
        decided_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (series_id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_board_images (
        image_id TEXT PRIMARY KEY,
        board_id TEXT NOT NULL REFERENCES universe_boards(board_id),
        position INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        ext TEXT NOT NULL,
        mime TEXT NOT NULL,
        width INTEGER NOT NULL,
        height INTEGER NOT NULL,
        bytes INTEGER NOT NULL,
        placeholder INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (board_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_board_images_sha ON universe_board_images(sha256)",
)

UNIVERSE_BOARD_MIGRATION = "2026-11-universe-board-requests-v1"

UNIVERSE_BOARD_SQL = (
    """
    CREATE TABLE IF NOT EXISTS universe_board_requests (
        request_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        status TEXT NOT NULL CHECK (status IN ('making', 'done', 'failed')),
        prompt TEXT NOT NULL,
        answers_feedback TEXT NOT NULL DEFAULT '',
        replaces_board_id TEXT REFERENCES universe_boards(board_id),
        sources TEXT NOT NULL DEFAULT '{}',
        image_count INTEGER NOT NULL CHECK (image_count >= 1),
        model_path TEXT NOT NULL,
        estimate_usd_micros INTEGER NOT NULL,
        approved_total_usd_micros INTEGER NOT NULL,
        workflow_id TEXT NOT NULL,
        board_id TEXT REFERENCES universe_boards(board_id),
        error TEXT,
        requested_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_board_requests_series ON universe_board_requests(series_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS universe_board_jobs (
        job_id TEXT PRIMARY KEY,
        request_id TEXT NOT NULL REFERENCES universe_board_requests(request_id),
        position INTEGER NOT NULL CHECK (position >= 0),
        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'refused', 'skipped',
                                               'pending_reconcile')),
        estimate_usd_micros INTEGER NOT NULL,
        approved_usd_micros INTEGER NOT NULL,
        reference_count INTEGER NOT NULL DEFAULT 0,
        sha256 TEXT,
        ext TEXT,
        mime TEXT,
        width INTEGER,
        height INTEGER,
        bytes INTEGER,
        placeholder INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (request_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_board_jobs_sha ON universe_board_jobs(sha256)",
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


def _apply_sync(conn: sqlite3.Connection, version: str, statements: tuple[str, ...]) -> bool:
    conn.execute(SCHEMA_MIGRATIONS_SQL)
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)).fetchone():
        return False
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in statements:
            conn.execute(statement)
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                     (version, _now_iso()))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


async def _apply(db_path: str, version: str, statements: tuple[str, ...]) -> bool:
    async with aiosqlite.connect(db_path, uri=True) as db:
        await db.execute(SCHEMA_MIGRATIONS_SQL)
        async with db.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (version,)) as cur:
            if await cur.fetchone():
                return False
        for statement in statements:
            await db.execute(statement)
        await db.execute("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                         (version, _now_iso()))
        await db.commit()
    return True


def apply_cast_library_sync(conn: sqlite3.Connection) -> bool:
    """Apply the cast library migration on an open sqlite3 connection; True when it ran now.
    Used by processes that open the cast tables without the API's startup (the worker, tools)."""
    return _apply_sync(conn, CAST_LIBRARY_MIGRATION, CAST_LIBRARY_SQL)


def apply_props_library_sync(conn: sqlite3.Connection) -> bool:
    """The props library migration on an open sqlite3 connection, as apply_cast_library_sync."""
    return _apply_sync(conn, PROPS_LIBRARY_MIGRATION, PROPS_LIBRARY_SQL)


def apply_universe_sync(conn: sqlite3.Connection) -> bool:
    """The universe migration on an open sqlite3 connection (after the cast library's, whose tables
    it references), as apply_cast_library_sync."""
    return _apply_sync(conn, UNIVERSE_MIGRATION, UNIVERSE_SQL)


def apply_universe_board_sync(conn: sqlite3.Connection) -> bool:
    """The universe board requests migration on an open sqlite3 connection (after the universe
    migration, whose tables it references), as apply_cast_library_sync."""
    return _apply_sync(conn, UNIVERSE_BOARD_MIGRATION, UNIVERSE_BOARD_SQL)


async def migrate_cast_library(db_path: str) -> bool:
    """The cast library tables (CAST_LIBRARY_MIGRATION); True when it ran now, False when applied."""
    return await _apply(db_path, CAST_LIBRARY_MIGRATION, CAST_LIBRARY_SQL)


async def migrate_props_library(db_path: str) -> bool:
    """The props library tables (PROPS_LIBRARY_MIGRATION); True when it ran now, False when applied."""
    return await _apply(db_path, PROPS_LIBRARY_MIGRATION, PROPS_LIBRARY_SQL)


async def migrate_universe(db_path: str) -> bool:
    """The universe tables (UNIVERSE_MIGRATION); True when it ran now, False when applied."""
    return await _apply(db_path, UNIVERSE_MIGRATION, UNIVERSE_SQL)


async def migrate_universe_board(db_path: str) -> bool:
    """The universe board request tables (UNIVERSE_BOARD_MIGRATION); True when it ran now."""
    return await _apply(db_path, UNIVERSE_BOARD_MIGRATION, UNIVERSE_BOARD_SQL)


async def run_all_migrations(db_path: str):
    """Run all pending migrations."""
    await migrate_add_user_tracking(db_path)
    await migrate_cast_library(db_path)
    await migrate_props_library(db_path)
    await migrate_universe(db_path)
    await migrate_universe_board(db_path)
