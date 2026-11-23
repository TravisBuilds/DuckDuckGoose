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
  universe tables when the request ends with at least one picture made. New tables only.
- universe story (``UNIVERSE_STORY_MIGRATION``, docs/WORLDS.md): the story a Universe starts from,
  the reference checklist read from it (each character, place and prop with why it is needed and how
  many photos) and which reference photos belong to which item. New tables only; a Universe without
  a story reads as one made before stories (notes and photos only).
- concept frames (``CONCEPT_FRAMES_MIGRATION``, docs/CONCEPT-FRAMES.md): the rounds of first-frame
  options made for an episode's shots before filming, each option (a paid still job in live mode)
  and the option picked as the shot's start frame. New tables only; an episode without them has no
  first frames and runs as before.
- kept starts (``KEPT_STARTS_MIGRATION``, docs/CREATION-FLOW-FIXES.md): the start request the new
  video wizard keeps for an episode that waits on its page (for its first frames or a Universe lock),
  so its Start card works on any device. A new table only; an episode without a row has nothing kept
  (the page still reads a start an older wizard kept in the browser).
- cast sheet pose (``CAST_SHEET_POSE_MIGRATION``, docs/CAST.md): each character's body type and the
  pose its sheet pictures show. A new table only; a character without a row is humanoid with the
  standing pose sheets always had.
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

UNIVERSE_STORY_MIGRATION = "2026-11-universe-story-v1"

UNIVERSE_STORY_SQL = (
    """
    CREATE TABLE IF NOT EXISTS universe_stories (
        series_id TEXT PRIMARY KEY REFERENCES series(series_id),
        body TEXT NOT NULL DEFAULT '',
        list_story_sha TEXT,
        list_made_at TEXT,
        updated_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_checklist_items (
        item_id TEXT PRIMARY KEY,
        series_id TEXT NOT NULL REFERENCES series(series_id),
        kind TEXT NOT NULL CHECK (kind IN ('character', 'place', 'prop')),
        name TEXT NOT NULL,
        why TEXT NOT NULL DEFAULT '',
        show TEXT NOT NULL DEFAULT '',
        ideal_photos INTEGER NOT NULL CHECK (ideal_photos BETWEEN 1 AND 6),
        min_photos INTEGER NOT NULL CHECK (min_photos BETWEEN 0 AND 6),
        optional INTEGER NOT NULL DEFAULT 0,
        position INTEGER NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('story', 'owner')),
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'removed')),
        created_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_checklist_items_series ON universe_checklist_items(series_id, position)",
    """
    CREATE TABLE IF NOT EXISTS universe_checklist_photos (
        item_id TEXT NOT NULL REFERENCES universe_checklist_items(item_id),
        image_id TEXT NOT NULL REFERENCES universe_photos(image_id),
        added_by TEXT,
        added_at TEXT NOT NULL,
        PRIMARY KEY (item_id, image_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_universe_checklist_photos_image ON universe_checklist_photos(image_id)",
)

CONCEPT_FRAMES_MIGRATION = "2026-11-concept-frames-v1"

CONCEPT_FRAMES_SQL = (
    """
    CREATE TABLE IF NOT EXISTS concept_frame_sets (
        set_id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL,
        shot_id TEXT NOT NULL,
        round INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('making', 'ready', 'partial', 'failed')),
        changes TEXT NOT NULL DEFAULT '[]',
        prompt TEXT NOT NULL,
        brief_sha TEXT NOT NULL,
        adapter TEXT NOT NULL,
        model_path TEXT NOT NULL,
        aspect_ratio TEXT NOT NULL,
        option_count INTEGER NOT NULL CHECK (option_count BETWEEN 1 AND 4),
        dry_run INTEGER NOT NULL,
        estimate_usd_micros INTEGER NOT NULL,
        approved_total_usd_micros INTEGER NOT NULL,
        workflow_id TEXT,
        error TEXT,
        requested_by TEXT,
        created_at TEXT NOT NULL,
        finished_at TEXT,
        UNIQUE (episode_id, shot_id, round)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_concept_frame_sets_shot ON concept_frame_sets(episode_id, shot_id, round)",
    """
    CREATE TABLE IF NOT EXISTS concept_frame_options (
        option_id TEXT PRIMARY KEY,
        set_id TEXT NOT NULL REFERENCES concept_frame_sets(set_id),
        position INTEGER NOT NULL,
        prompt TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'refused',
                                               'pending_reconcile', 'skipped')),
        estimated_usd_micros INTEGER,
        approved_usd_micros INTEGER,
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
        UNIQUE (set_id, position)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_concept_frame_options_sha ON concept_frame_options(sha256)",
    """
    CREATE TABLE IF NOT EXISTS concept_frame_picks (
        pick_id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL,
        shot_id TEXT NOT NULL,
        option_id TEXT NOT NULL REFERENCES concept_frame_options(option_id),
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        brief_sha TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('current', 'replaced', 'removed')),
        picked_by TEXT,
        picked_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_concept_frame_picks_shot ON concept_frame_picks(episode_id, shot_id)",
)

KEPT_STARTS_MIGRATION = "2026-11-kept-starts-v1"

KEPT_STARTS_SQL = (
    """
    CREATE TABLE IF NOT EXISTS episode_kept_starts (
        episode_id TEXT PRIMARY KEY,
        request TEXT NOT NULL,
        kept_by TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)

CAST_SHEET_POSE_MIGRATION = "2026-11-cast-sheet-pose-v1"

# body_type is checked by hfvg.cast_store (no CHECK here, so a later body type needs no table rebuild)
CAST_SHEET_POSE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS cast_character_bodies (
        character_id TEXT PRIMARY KEY REFERENCES cast_characters(character_id),
        body_type TEXT NOT NULL DEFAULT 'humanoid',
        sheet_pose TEXT NOT NULL DEFAULT '',
        updated_by TEXT,
        updated_at TEXT NOT NULL
    )
    """,
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


def apply_universe_story_sync(conn: sqlite3.Connection) -> bool:
    """The universe story migration on an open sqlite3 connection (after the universe migration,
    whose tables it references), as apply_cast_library_sync."""
    return _apply_sync(conn, UNIVERSE_STORY_MIGRATION, UNIVERSE_STORY_SQL)


def apply_concept_frames_sync(conn: sqlite3.Connection) -> bool:
    """The concept frames migration on an open sqlite3 connection, as apply_cast_library_sync."""
    return _apply_sync(conn, CONCEPT_FRAMES_MIGRATION, CONCEPT_FRAMES_SQL)


def apply_kept_starts_sync(conn: sqlite3.Connection) -> bool:
    """The kept starts migration on an open sqlite3 connection, as apply_cast_library_sync."""
    return _apply_sync(conn, KEPT_STARTS_MIGRATION, KEPT_STARTS_SQL)


def apply_cast_sheet_pose_sync(conn: sqlite3.Connection) -> bool:
    """The cast sheet pose migration on an open sqlite3 connection (after the cast library's, whose
    characters it references), as apply_cast_library_sync."""
    return _apply_sync(conn, CAST_SHEET_POSE_MIGRATION, CAST_SHEET_POSE_SQL)


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


async def migrate_universe_story(db_path: str) -> bool:
    """The universe story and reference checklist tables (UNIVERSE_STORY_MIGRATION); True when it ran now."""
    return await _apply(db_path, UNIVERSE_STORY_MIGRATION, UNIVERSE_STORY_SQL)


async def migrate_concept_frames(db_path: str) -> bool:
    """The concept frame tables (CONCEPT_FRAMES_MIGRATION); True when it ran now."""
    return await _apply(db_path, CONCEPT_FRAMES_MIGRATION, CONCEPT_FRAMES_SQL)


async def migrate_kept_starts(db_path: str) -> bool:
    """The kept start table (KEPT_STARTS_MIGRATION); True when it ran now."""
    return await _apply(db_path, KEPT_STARTS_MIGRATION, KEPT_STARTS_SQL)


async def migrate_cast_sheet_pose(db_path: str) -> bool:
    """The character body table (CAST_SHEET_POSE_MIGRATION); True when it ran now."""
    return await _apply(db_path, CAST_SHEET_POSE_MIGRATION, CAST_SHEET_POSE_SQL)


async def run_all_migrations(db_path: str):
    """Run all pending migrations."""
    await migrate_add_user_tracking(db_path)
    await migrate_cast_library(db_path)
    await migrate_props_library(db_path)
    await migrate_universe(db_path)
    await migrate_universe_board(db_path)
    await migrate_universe_story(db_path)
    await migrate_concept_frames(db_path)
    await migrate_kept_starts(db_path)
    await migrate_cast_sheet_pose(db_path)
