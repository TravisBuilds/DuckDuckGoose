"""
Studio database schema initialization.

Tables:
- episodes: Episode metadata, live mode flag, gate approvals
- shots: Shot status, URLs, QC results
- audit_log: Audit trail for live mode changes, approvals, budget commits
"""

import aiosqlite
from pathlib import Path


async def init_studio_db(db_path: str = "./data/studio.db"):
    """
    Initialize studio database schema.
    
    Args:
        db_path: Path to SQLite database
    """
    # Ensure data directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    
    async with aiosqlite.connect(db_path) as db:
        # Episodes table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS episodes (
                episode_id TEXT PRIMARY KEY,
                beatmap_path TEXT,
                live_mode INTEGER DEFAULT 0,
                g108_approved INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        
        # Add smoke_test_result column if it doesn't exist (migration)
        try:
            await db.execute("ALTER TABLE episodes ADD COLUMN smoke_test_result TEXT")
        except:
            pass  # Column already exists
        
        # Shots table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS shots (
                id TEXT PRIMARY KEY,
                episode_id TEXT NOT NULL,
                shot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                still_url TEXT,
                clip_url TEXT,
                qc_results TEXT,
                retries INTEGER DEFAULT 0,
                prompt TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now')),
                FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
            )
        """)
        
        # Create index on episode_id for shots
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_shots_episode 
            ON shots(episode_id)
        """)
        
        # Audit log table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL,
                action TEXT NOT NULL,
                details TEXT,
                user TEXT,
                timestamp TEXT DEFAULT (datetime('now'))
            )
        """)
        
        # Create index on episode_id for audit_log
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_episode 
            ON audit_log(episode_id)
        """)
        
        # Smoke gate overrides table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS smoke_gate_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id TEXT NOT NULL,
                activity TEXT NOT NULL,
                override_by TEXT NOT NULL,
                override_time TEXT NOT NULL,
                FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
            )
        """)
        
        # Anchor stills table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS anchor_stills (
                episode_id TEXT NOT NULL,
                anchor_shot_id TEXT NOT NULL,
                target_shot_id TEXT NOT NULL,
                local_path TEXT NOT NULL,
                version INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                approved_by TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (episode_id, target_shot_id),
                FOREIGN KEY (episode_id) REFERENCES episodes(episode_id)
            )
        """)
        
        await db.commit()


async def create_episode(
    db_path: str,
    episode_id: str,
    beatmap_path: str | None = None,
) -> None:
    """
    Create episode record.
    
    Args:
        db_path: Path to database
        episode_id: Episode ID
        beatmap_path: Path to beatmap file
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT OR IGNORE INTO episodes 
               (episode_id, beatmap_path) 
               VALUES (?, ?)""",
            (episode_id, beatmap_path)
        )
        await db.commit()


async def upsert_episode(
    db_path: str,
    episode_id: str,
    beatmap_path: str | None = None,
    live_mode: bool = False,
) -> None:
    """
    Upsert episode record (insert or update).
    
    Sets live_mode based on dry_run parameter. This is the single source of truth
    for whether an episode is in live mode.
    
    Args:
        db_path: Path to database
        episode_id: Episode ID
        beatmap_path: Path to beatmap file
        live_mode: True if not dry_run (live paid generation)
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO episodes (episode_id, beatmap_path, live_mode) 
               VALUES (?, ?, ?)
               ON CONFLICT(episode_id) DO UPDATE SET
                   beatmap_path = COALESCE(excluded.beatmap_path, beatmap_path),
                   live_mode = excluded.live_mode,
                   updated_at = datetime('now')""",
            (episode_id, beatmap_path, 1 if live_mode else 0)
        )
        await db.commit()


async def set_live_mode(
    db_path: str,
    episode_id: str,
    live: bool,
    user: str = "admin",
) -> None:
    """
    Set episode live mode flag.
    
    Args:
        db_path: Path to database
        episode_id: Episode ID
        live: Live mode flag
        user: User making the change
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE episodes 
               SET live_mode = ?, updated_at = datetime('now')
               WHERE episode_id = ?""",
            (int(live), episode_id)
        )
        
        # Audit log
        await db.execute(
            """INSERT INTO audit_log 
               (episode_id, action, details, user) 
               VALUES (?, ?, ?, ?)""",
            (episode_id, "set_live_mode", f"live={live}", user)
        )
        
        await db.commit()


async def approve_g108(
    db_path: str,
    episode_id: str,
    user: str = "admin",
) -> None:
    """
    Approve G1.08 credit plan.
    
    Args:
        db_path: Path to database
        episode_id: Episode ID
        user: User approving
    """
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """UPDATE episodes 
               SET g108_approved = 1, updated_at = datetime('now')
               WHERE episode_id = ?""",
            (episode_id,)
        )
        
        # Audit log
        await db.execute(
            """INSERT INTO audit_log 
               (episode_id, action, details, user) 
               VALUES (?, ?, ?, ?)""",
            (episode_id, "approve_g108", "Credit plan approved", user)
        )
        
        await db.commit()


async def insert_shots_from_beatmap(
    db_path: str,
    episode_id: str,
    shots: list[dict],
) -> None:
    """
    Insert shots from parsed beatmap.
    
    Args:
        db_path: Path to database
        episode_id: Episode ID
        shots: List of shot dicts from parser
    """
    async with aiosqlite.connect(db_path) as db:
        for shot in shots:
            shot_id = shot["shot_id"]
            record_id = f"{episode_id}-{shot_id}"
            
            await db.execute(
                """INSERT OR IGNORE INTO shots 
                   (id, episode_id, shot_id, status, prompt) 
                   VALUES (?, ?, ?, ?, ?)""",
                (record_id, episode_id, shot_id, "pending", shot.get("action", ""))
            )
        
        await db.commit()


async def store_smoke_test_result(db_path: str, episode_id: str, result: dict):
    """Store smoke test result in episode metadata."""
    import json
    async with aiosqlite.connect(db_path) as db:
        # Store in episodes table smoke_test_result column (JSON)
        cursor = await db.execute(
            "UPDATE episodes SET smoke_test_result = ? WHERE episode_id = ?",
            (json.dumps(result), episode_id)
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Episode {episode_id} not found - cannot store smoke test result")
        await db.commit()


async def get_smoke_test_result(db_path: str, episode_id: str) -> dict | None:
    """Retrieve latest smoke test result for episode."""
    import json
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT smoke_test_result FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return json.loads(row[0])
            return None


async def check_smoke_test_passed(db_path: str, episode_id: str, max_age_hours: int = 24) -> tuple[bool, str]:
    """
    Check if episode has a recent passing smoke test.
    
    Returns:
        (passed, reason) tuple
    """
    from datetime import datetime, timedelta
    result = await get_smoke_test_result(db_path, episode_id)
    
    if not result:
        return (False, "No smoke test result found")
    
    if not result.get("passed"):
        return (False, f"Smoke test failed: {result.get('upload_probe', {}).get('error') or result.get('generation_probe', {}).get('error')}")
    
    # Check timestamp age
    timestamp_str = result.get("timestamp")
    if not timestamp_str:
        return (False, "Smoke test missing timestamp")
    
    timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    age = datetime.now(timestamp.tzinfo) - timestamp
    
    if age > timedelta(hours=max_age_hours):
        return (False, f"Smoke test too old: {age.total_seconds() / 3600:.1f} hours (max {max_age_hours}h)")
    
    return (True, "Smoke test passed")


async def enforce_smoke_test_gate(db_path: str, episode_id: str, activity_name: str) -> None:
    """
    Enforce smoke test gate for live operations.
    
    Checks SMOKE_TEST_OVERRIDE env var. If set, persists override to DB and allows.
    Otherwise checks for recent passing smoke test.
    
    Args:
        db_path: Path to studio DB
        episode_id: Episode being operated on
        activity_name: Name of activity for audit trail
        
    Raises:
        RuntimeError: If smoke test gate fails
    """
    import os
    from temporalio import activity
    
    # Check if we're in dry run (no gate in dry run)
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    if dry_run:
        return  # No gate in dry run
    
    # Check for override
    smoke_override = os.getenv("SMOKE_TEST_OVERRIDE", "false").lower() == "true"
    
    if smoke_override:
        # Persist override to DB
        import aiosqlite
        from datetime import datetime
        
        override_by = os.getenv("USER", "system")
        override_time = datetime.utcnow().isoformat() + "Z"
        
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO smoke_gate_overrides (episode_id, activity, override_by, override_time)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, activity_name, override_by, override_time)
            )
            await db.commit()
        
        activity.logger.warning(
            f"SMOKE_TEST_OVERRIDE enabled for {episode_id} in {activity_name} by {override_by}"
        )
        return
    
    # Check smoke test
    max_age_hours = int(os.getenv("SMOKE_TEST_MAX_AGE_HOURS", "24"))
    passed, reason = await check_smoke_test_passed(db_path, episode_id, max_age_hours)
    
    if not passed:
        raise RuntimeError(
            f"Smoke test gate failed: {reason}. "
            f"Run smoke test or set SMOKE_TEST_OVERRIDE=true"
        )


async def set_anchor_still(
    db_path: str,
    episode_id: str,
    anchor_shot_id: str,
    target_shot_ids: list[str],
    local_path: str,
    version: int,
    sha256: str,
    approved_by: str | None = None,
) -> None:
    """
    Set an anchor still for target shots.
    
    Args:
        db_path: Path to studio database
        episode_id: Episode ID
        anchor_shot_id: Shot ID of the anchor still
        target_shot_ids: List of shot IDs that will use this anchor as a ref
        local_path: Local file path to the anchor still
        version: Version number of the anchor still
        sha256: SHA256 hash of the anchor still file
        approved_by: Optional note about who/what approved this anchor
    """
    import aiosqlite
    
    async with aiosqlite.connect(db_path) as db:
        for target_shot_id in target_shot_ids:
            await db.execute(
                """INSERT OR REPLACE INTO anchor_stills 
                   (episode_id, anchor_shot_id, target_shot_id, local_path, version, sha256, approved_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (episode_id, anchor_shot_id, target_shot_id, local_path, version, sha256, approved_by)
            )
        await db.commit()


async def get_anchor_still(
    db_path: str,
    episode_id: str,
    target_shot_id: str,
) -> dict | None:
    """
    Get anchor still for a target shot.
    
    Args:
        db_path: Path to studio database
        episode_id: Episode ID
        target_shot_id: Target shot ID
        
    Returns:
        dict with anchor_shot_id, local_path, version, sha256, approved_by if found, None otherwise
    """
    import aiosqlite
    
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT anchor_shot_id, local_path, version, sha256, approved_by
               FROM anchor_stills
               WHERE episode_id = ? AND target_shot_id = ?""",
            (episode_id, target_shot_id)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "anchor_shot_id": row[0],
                    "local_path": row[1],
                    "version": row[2],
                    "sha256": row[3],
                    "approved_by": row[4],
                }
            return None


async def list_anchor_stills(
    db_path: str,
    episode_id: str,
) -> list[dict]:
    """
    List all anchor stills for an episode.
    
    Args:
        db_path: Path to studio database
        episode_id: Episode ID
        
    Returns:
        List of dicts with anchor_shot_id, target_shot_id, local_path, version, sha256, approved_by
    """
    import aiosqlite
    
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            """SELECT anchor_shot_id, target_shot_id, local_path, version, sha256, approved_by
               FROM anchor_stills
               WHERE episode_id = ?
               ORDER BY anchor_shot_id, target_shot_id""",
            (episode_id,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "anchor_shot_id": row[0],
                    "target_shot_id": row[1],
                    "local_path": row[2],
                    "version": row[3],
                    "sha256": row[4],
                    "approved_by": row[5],
                }
                for row in rows
            ]
