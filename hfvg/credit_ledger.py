"""Higgsfield credit ledger: per-episode caps, pause on limit, idempotent reserves.

Unlike the USD ledger (budget.py), this tracks Higgsfield CREDITS directly from their API.
Episode cap triggers a workflow pause (update handler), not a failure.
Single-run lock prevents concurrent episode runs on the same Higgsfield account.

Schema:
- credit_lines: per-episode line with cap_credits, spent_credits, reserved_credits
- credit_transactions: per-job reserve/commit/release audit trail
- run_locks: one row per active episode (DELETE on completion)

All amounts are integer Higgsfield credits (1 credit = 1 Higgsfield app credit).
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiosqlite

# Default episode cap: 500 credits (Ep05 teaser cap)
DEFAULT_EPISODE_CAP_CREDITS = 500

# Pause threshold: 90% of cap (gives warning room before hard stop)
DEFAULT_PAUSE_PERCENT = 90


@dataclass(frozen=True)
class CreditSettings:
    episode_cap_credits: int
    pause_percent: int
    
    @property
    def pause_threshold_credits(self) -> int:
        return self.episode_cap_credits * self.pause_percent // 100


def get_credit_settings() -> CreditSettings:
    """Read credit settings from env."""
    return CreditSettings(
        episode_cap_credits=int(os.getenv("EPISODE_CAP_CREDITS", str(DEFAULT_EPISODE_CAP_CREDITS))),
        pause_percent=int(os.getenv("PAUSE_PERCENT", str(DEFAULT_PAUSE_PERCENT))),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CreditLedger:
    """Higgsfield credit ledger with episode cap and pause threshold."""
    
    def __init__(self, db_path: str | None = None):
        from hfvg.config import config
        self.db_path = db_path or config.DB_PATH
    
    async def init_db(self):
        """Create credit ledger tables."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS credit_lines (
                    line_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    cap_credits INTEGER NOT NULL,
                    pause_threshold_credits INTEGER NOT NULL,
                    spent_credits INTEGER NOT NULL DEFAULT 0,
                    reserved_credits INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS credit_transactions (
                    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    job_id TEXT,
                    txn_type TEXT NOT NULL,
                    amount_credits INTEGER NOT NULL,
                    timestamp TEXT,
                    notes TEXT
                )
            """)
            
            await db.execute("""
                CREATE TABLE IF NOT EXISTS run_locks (
                    episode_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    locked_at TEXT NOT NULL,
                    account_identifier TEXT NOT NULL DEFAULT 'default'
                )
            """)
            
            await db.commit()
    
    async def acquire_run_lock(
        self, episode_id: str, workflow_id: str, account_identifier: str = "default"
    ) -> dict:
        """
        Acquire account-level run lock (one episode per account at a time).
        
        Args:
            episode_id: Episode identifier
            workflow_id: Temporal workflow ID
            account_identifier: Account/org identifier
        
        Returns:
            dict with acquired (bool), existing_episode (str|None)
        """
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            # Check for existing lock on this account
            async with db.execute(
                "SELECT episode_id FROM run_locks WHERE account_identifier = ?",
                (account_identifier,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {"acquired": False, "existing_episode": row[0]}
            
            # Acquire lock
            await db.execute(
                "INSERT OR REPLACE INTO run_locks (episode_id, workflow_id, locked_at, account_identifier) VALUES (?, ?, ?, ?)",
                (episode_id, workflow_id, _now_iso(), account_identifier)
            )
            await db.commit()
        
        return {"acquired": True, "existing_episode": None}
    
    async def release_run_lock(self, episode_id: str):
        """Release account-level run lock."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("DELETE FROM run_locks WHERE episode_id = ?", (episode_id,))
            await db.commit()
    
    async def create_episode_line(self, episode_id: str, cap_credits: int | None = None):
        """Create credit line for episode."""
        settings = get_credit_settings()
        cap = cap_credits if cap_credits is not None else settings.episode_cap_credits
        pause = cap * settings.pause_percent // 100
        
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            await db.execute(
                """INSERT OR IGNORE INTO credit_lines 
                   (line_id, episode_id, cap_credits, pause_threshold_credits, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (f"episode_{episode_id}", episode_id, cap, pause, _now_iso())
            )
            await db.commit()
    
    async def acquire_run_lock(
        self, 
        episode_id: str, 
        workflow_id: str, 
        account_identifier: str = "default",
        stale_timeout_seconds: int = 3600  # 1 hour default
    ) -> dict:
        """
        Acquire run lock for episode.
        
        Single-run lock: only one episode workflow can run at a time per Higgsfield account.
        If an existing lock is stale (older than stale_timeout_seconds), it is reclaimed.
        
        Args:
            episode_id: Episode identifier
            workflow_id: Temporal workflow ID
            account_identifier: Account key (configured, not literal "ACCOUNT")
            stale_timeout_seconds: Time after which a lock is considered stale and can be reclaimed
        
        Returns:
            dict with acquired (bool), existing_episode (str|None), reclaimed (bool)
        """
        from datetime import datetime, timezone, timedelta
        
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            # Check if lock exists
            async with db.execute(
                "SELECT episode_id, locked_at FROM run_locks WHERE account_identifier = ?",
                (account_identifier,)
            ) as cur:
                row = await cur.fetchone()
                if row:
                    existing_episode, locked_at_str = row
                    
                    # Check if lock is stale
                    locked_at = datetime.fromisoformat(locked_at_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    age_seconds = (now - locked_at).total_seconds()
                    
                    if age_seconds > stale_timeout_seconds:
                        # Lock is stale, reclaim it
                        await db.execute(
                            "DELETE FROM run_locks WHERE account_identifier = ?",
                            (account_identifier,)
                        )
                        # Acquire lock for new episode
                        await db.execute(
                            """INSERT INTO run_locks (episode_id, workflow_id, locked_at, account_identifier)
                               VALUES (?, ?, ?, ?)""",
                            (episode_id, workflow_id, _now_iso(), account_identifier)
                        )
                        await db.commit()
                        return {
                            "acquired": True, 
                            "existing_episode": existing_episode,
                            "reclaimed": True
                        }
                    else:
                        # Lock is fresh, cannot acquire
                        return {
                            "acquired": False, 
                            "existing_episode": existing_episode,
                            "reclaimed": False
                        }
            
            # No existing lock, acquire it
            await db.execute(
                """INSERT INTO run_locks (episode_id, workflow_id, locked_at, account_identifier)
                   VALUES (?, ?, ?, ?)""",
                (episode_id, workflow_id, _now_iso(), account_identifier)
            )
            await db.commit()
            return {"acquired": True, "existing_episode": None, "reclaimed": False}
    
    async def release_run_lock(self, episode_id: str):
        """Release run lock for episode."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("DELETE FROM run_locks WHERE episode_id = ?", (episode_id,))
            await db.commit()
    
    async def update_credit_cap(self, episode_id: str, new_cap_credits: int) -> dict:
        """
        Update credit cap for an episode line.
        
        Used to raise the cap and resume a paused run.
        
        Args:
            episode_id: Episode identifier
            new_cap_credits: New credit cap (must be >= current cap)
        
        Returns:
            dict with updated (bool), previous_cap (int), new_cap (int)
        """
        line_id = f"episode_{episode_id}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Get current cap
            async with db.execute(
                "SELECT cap_credits FROM credit_lines WHERE line_id = ?",
                (line_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    raise ValueError(f"Credit line not found for episode {episode_id}")
                
                previous_cap = row[0]
            
            # Update cap
            await db.execute(
                "UPDATE credit_lines SET cap_credits = ? WHERE line_id = ?",
                (new_cap_credits, line_id)
            )
            await db.commit()
            
            return {
                "updated": True,
                "previous_cap": previous_cap,
                "new_cap": new_cap_credits
            }
    
    async def reserve_credits(self, episode_id: str, job_id: str, amount_credits: float, notes: str = "") -> dict[str, Any]:
        """
        Reserve credits for a job (idempotent).
        
        If a reservation with the same job_id already exists, returns the existing reservation
        without modifying the ledger (idempotent).
        
        Returns:
            {"reserved": bool, "available": int, "pause_needed": bool, "over_cap": bool, "existing_amount": float|None}
        """
        if amount_credits <= 0:
            raise ValueError(f"amount_credits must be > 0, got {amount_credits}")
        
        line_id = f"episode_{episode_id}"
        
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            # Check if already reserved (idempotent)
            async with db.execute(
                """SELECT amount_credits FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
                (line_id, job_id)
            ) as cur:
                existing = await cur.fetchone()
                if existing:
                    existing_amount = existing[0]
                    return {
                        "reserved": True,
                        "available": 0,
                        "pause_needed": False,
                        "over_cap": False,
                        "idempotent": True,
                        "existing_amount": existing_amount
                    }
            
            # Get current line state
            async with db.execute(
                """SELECT cap_credits, pause_threshold_credits, spent_credits, reserved_credits 
                   FROM credit_lines WHERE line_id = ?""",
                (line_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    raise ValueError(f"Credit line not found for episode {episode_id}")
                
                cap, pause_threshold, spent, reserved = row
            
            total_committed = spent + reserved + amount_credits
            available = cap - total_committed
            
            # Check if over cap (hard stop)
            if total_committed > cap:
                return {
                    "reserved": False,
                    "available": max(0, cap - spent - reserved),
                    "pause_needed": False,
                    "over_cap": True
                }
            
            # Reserve the credits
            await db.execute(
                """UPDATE credit_lines SET reserved_credits = reserved_credits + ? 
                   WHERE line_id = ?""",
                (amount_credits, line_id)
            )
            
            await db.execute(
                """INSERT INTO credit_transactions 
                   (line_id, episode_id, job_id, txn_type, amount_credits, timestamp, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (line_id, episode_id, job_id, "reserve", amount_credits, _now_iso(), notes)
            )
            
            await db.commit()
            
            return {
                "reserved": True,
                "available": available,
                "pause_needed": total_committed >= pause_threshold,
                "over_cap": False
            }
    
    async def commit_credits(self, episode_id: str, job_id: str, actual_credits: int):
        """
        Commit reserved credits with actual cost (idempotent).
        
        Moves from reserved to spent, adjusts if actual differs from reserve.
        """
        if actual_credits < 0:
            raise ValueError(f"actual_credits must be >= 0, got {actual_credits}")
        
        line_id = f"episode_{episode_id}"
        
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            # Check if already committed
            async with db.execute(
                """SELECT amount_credits FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type = 'commit'""",
                (line_id, job_id)
            ) as cur:
                if await cur.fetchone():
                    return  # Already committed (idempotent)
            
            # Get reserved amount
            async with db.execute(
                """SELECT amount_credits FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
                (line_id, job_id)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    raise ValueError(f"No reservation found for job {job_id}")
                reserved_amount = row[0]
            
            # Update line: subtract from reserved, add to spent
            await db.execute(
                """UPDATE credit_lines 
                   SET reserved_credits = reserved_credits - ?,
                       spent_credits = spent_credits + ?
                   WHERE line_id = ?""",
                (reserved_amount, actual_credits, line_id)
            )
            
            # Record commit
            await db.execute(
                """INSERT INTO credit_transactions 
                   (line_id, episode_id, job_id, txn_type, amount_credits, timestamp, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (line_id, episode_id, job_id, "commit", actual_credits, _now_iso(),
                 f"reserved={reserved_amount}")
            )
            
            await db.commit()
    
    async def release_credits(self, episode_id: str, job_id: str, notes: str = ""):
        """
        Release reserved credits without committing (idempotent).
        
        Used when a job is canceled or fails before completion.
        """
        line_id = f"episode_{episode_id}"
        
        async with aiosqlite.connect(self.db_path, uri=True, isolation_level="IMMEDIATE") as db:
            # Check if already released or committed
            async with db.execute(
                """SELECT txn_type FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type IN ('release', 'commit')""",
                (line_id, job_id)
            ) as cur:
                if await cur.fetchone():
                    return  # Already released or committed
            
            # Get reserved amount
            async with db.execute(
                """SELECT amount_credits FROM credit_transactions 
                   WHERE line_id = ? AND job_id = ? AND txn_type = 'reserve'""",
                (line_id, job_id)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return  # No reservation to release
                reserved_amount = row[0]
            
            # Release from reserved
            await db.execute(
                """UPDATE credit_lines SET reserved_credits = reserved_credits - ? 
                   WHERE line_id = ?""",
                (reserved_amount, line_id)
            )
            
            # Record release
            await db.execute(
                """INSERT INTO credit_transactions 
                   (line_id, episode_id, job_id, txn_type, amount_credits, timestamp, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (line_id, episode_id, job_id, "release", reserved_amount, _now_iso(), "")
            )
            
            await db.commit()
    
    async def get_episode_status(self, episode_id: str) -> dict[str, Any]:
        """Get current credit status for episode."""
        line_id = f"episode_{episode_id}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute(
                """SELECT cap_credits, pause_threshold_credits, spent_credits, reserved_credits 
                   FROM credit_lines WHERE line_id = ?""",
                (line_id,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return {
                        "cap_credits": 0,
                        "spent_credits": 0,
                        "reserved_credits": 0,
                        "available_credits": 0,
                        "pause_threshold": 0,
                        "pause_needed": False,
                        "over_cap": False
                    }
                
                cap, pause_threshold, spent, reserved = row
                total_committed = spent + reserved
                available = cap - total_committed
                
                return {
                    "cap_credits": cap,
                    "spent_credits": spent,
                    "reserved_credits": reserved,
                    "available_credits": max(0, available),
                    "pause_threshold": pause_threshold,
                    "pause_needed": total_committed >= pause_threshold,
                    "over_cap": total_committed > cap
                }
