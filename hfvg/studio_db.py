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
