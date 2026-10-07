"""Per-line budget ledger with 80% stop and Ep04 caps (HARNESS-GATES v1.1)."""

import aiosqlite
from pathlib import Path
from typing import Any

from hfvg.config import config
from hfvg.gates import load_policy


class BudgetLedger:
    """
    Per-line budget tracking with 80% stop.
    
    Tracks spend per budget line (L1-L6 Higgsfield, ElevenLabs lines) with:
    - Reserve before round
    - Commit actuals after generation
    - Release on block or refund
    - 80% stop threshold per line
    - Native provider credits + USD costs
    """
    
    def __init__(self, db_path: str | None = None):
        """Initialize budget ledger."""
        self.db_path = db_path or config.DB_PATH
        self.policy = load_policy("mid-mountain-rest")
    
    async def init_db(self):
        """Initialize budget tables."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Per-line budget tracking
            await db.execute("""
                CREATE TABLE IF NOT EXISTS budget_lines (
                    line_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    line_name TEXT NOT NULL,
                    budget_cap REAL NOT NULL,
                    stop_threshold REAL NOT NULL,
                    unit TEXT NOT NULL,
                    spent REAL DEFAULT 0,
                    reserved REAL DEFAULT 0,
                    created_at TEXT
                )
            """)
            
            # Budget transactions (reserve/commit/release)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS budget_transactions (
                    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    job_id TEXT,
                    txn_type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    usd_micros INTEGER,
                    timestamp TEXT NOT NULL,
                    reason TEXT,
                    FOREIGN KEY (line_id) REFERENCES budget_lines(line_id)
                )
            """)
            
            await db.commit()
    
    async def init_episode_budget(self, episode_id: str):
        """
        Initialize per-line budgets for an episode.
        
        Args:
            episode_id: Episode identifier (e.g., 'ep04')
        """
        await self.init_db()
        
        # Get Higgsfield budget
        hf_budget = self.policy.get_higgsfield_budget(episode_id)
        lines = hf_budget.get("lines", {})
        stop_fraction = hf_budget.get("stop_fraction", 0.8)
        unit = hf_budget.get("unit", "Higgsfield app credits")
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Initialize Higgsfield lines
            for line_name, cap in lines.items():
                line_id = f"{episode_id}:{line_name}"
                stop_threshold = cap * stop_fraction
                
                await db.execute("""
                    INSERT OR IGNORE INTO budget_lines
                    (line_id, episode_id, provider, line_name, budget_cap, 
                     stop_threshold, unit, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (line_id, episode_id, "higgsfield", line_name, cap, 
                      stop_threshold, unit))
            
            # Initialize ElevenLabs lines
            el_budget = self.policy.get_elevenlabs_budget(episode_id)
            el_lines = el_budget.get("lines", {})
            el_stop_fraction = el_budget.get("stop_fraction", 0.8)
            el_unit = el_budget.get("unit", "ElevenLabs credits")
            
            for line_name, cap in el_lines.items():
                line_id = f"{episode_id}:el_{line_name}"
                stop_threshold = cap * el_stop_fraction
                
                await db.execute("""
                    INSERT OR IGNORE INTO budget_lines
                    (line_id, episode_id, provider, line_name, budget_cap,
                     stop_threshold, unit, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (line_id, episode_id, "elevenlabs", line_name, cap,
                      stop_threshold, el_unit))
            
            await db.commit()

    async def init_episode_budget_from_plan(self, episode_id: str, credit_plan: dict):
        """
        Initialize budget from parsed credit plan.
        
        Args:
            episode_id: Episode ID
            credit_plan: Parsed credit plan dict with lines, caps, stops
        
        Raises:
            ValueError: If any stop_threshold > budget_cap
        """
        await self.init_db()
        
        unit = "Higgsfield app credits"
        
        # Validate all lines before inserting
        for line_name, line_data in credit_plan["lines"].items():
            cap = line_data["cap"]
            stop = line_data["stop"]
            
            if stop > cap:
                raise ValueError(
                    f"Invalid credit plan for {line_name}: stop_threshold ({stop}) "
                    f"must be <= budget_cap ({cap})"
                )
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            for line_name, line_data in credit_plan["lines"].items():
                line_id = f"{episode_id}:{line_name}"
                cap = line_data["cap"]
                stop = line_data["stop"]
                
                await db.execute("""
                    INSERT OR IGNORE INTO budget_lines
                    (line_id, episode_id, provider, line_name, budget_cap, 
                     stop_threshold, unit, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (line_id, episode_id, "higgsfield", line_name, cap, 
                      stop, unit))
            
            await db.commit()
    
    async def reserve(self, episode_id: str, line_name: str, amount: float, 
                     reason: str = "") -> bool:
        """
        Reserve budget from a line before generation (atomic).
        
        Args:
            episode_id: Episode identifier
            line_name: Budget line (e.g., 'L1_refs', 'L4_video')
            amount: Amount to reserve (in line's native units)
            reason: Reason for reservation
        
        Returns:
            True if reserved, False if would exceed stop threshold
        
        Raises:
            ValueError: If line doesn't exist, episode cap exceeded
        """
        EPISODE_CAP = 1250.0  # GC.04 1,250 credit episode cap
        line_id = f"{episode_id}:{line_name}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # BEGIN IMMEDIATE for atomic reserve (prevents concurrent double-reserve)
            await db.execute("BEGIN IMMEDIATE")
            
            try:
                # Check line exists and get caps (hard cap pre-check only)
                async with db.execute("""
                    SELECT spent, reserved, stop_threshold, budget_cap
                    FROM budget_lines WHERE line_id = ?
                """, (line_id,)) as cursor:
                    row = await cursor.fetchone()
                    if not row:
                        await db.rollback()
                        raise ValueError(f"Budget line {line_id} not found")
                    
                    spent, reserved, stop_threshold, cap = row
                    total = spent + reserved + amount
                    
                    # Hard cap check (line-level) - fast fail before atomic UPDATE
                    if total > cap:
                        await db.rollback()
                        raise ValueError(
                            f"Budget cap exceeded: {total} > {cap} for {line_id}"
                        )
                
                # Check episode-level 1,250 cap (GC.04)
                async with db.execute("""
                    SELECT SUM(spent + reserved) as total
                    FROM budget_lines WHERE episode_id = ?
                """, (episode_id,)) as cursor:
                    row = await cursor.fetchone()
                    episode_total = (row[0] or 0.0) + amount
                    
                    if episode_total > EPISODE_CAP:
                        await db.rollback()
                        raise ValueError(
                            f"Episode {episode_id} would exceed 1,250 credit cap. "
                            f"Current: {row[0] or 0.0:.1f}, requested: {amount:.1f}, "
                            f"cap: {EPISODE_CAP}"
                        )
                
                # Atomic conditional UPDATE - single source of truth for stop threshold
                # This prevents concurrent reserves from exceeding the stop threshold
                cursor = await db.execute("""
                    UPDATE budget_lines 
                    SET reserved = reserved + ?
                    WHERE line_id = ? 
                      AND (spent + reserved + ?) <= stop_threshold
                """, (amount, line_id, amount))
                
                if cursor.rowcount == 0:
                    # Race condition: another reserve beat us to the threshold
                    await db.rollback()
                    return False
                
                # Log transaction
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, 'reserve', ?, datetime('now'), ?)
                """, (line_id, episode_id, amount, reason))
                
                await db.commit()
                return True
            
            except Exception:
                await db.rollback()
                raise
    
    async def commit(self, episode_id: str, line_name: str, reserved_amount: float,
                    actual_cost: float | None = None,
                    usd_micros: int | None = None, job_id: str | None = None,
                    reason: str = ""):
        """
        Commit actual spend (release reserve, add to spent).
        
        Handles actual cost != reserved: if actual > reserved, commits reserved and logs
        overage warning. If actual < reserved, commits actual and releases remainder.
        
        Args:
            episode_id: Episode identifier
            line_name: Budget line
            reserved_amount: Amount that was reserved
            actual_cost: Actual cost from provider (defaults to reserved_amount)
            usd_micros: Cost in USD micros (optional)
            job_id: Associated job ID (optional)
            reason: Reason for spend
        """
        if actual_cost is None:
            actual_cost = reserved_amount
        
        line_id = f"{episode_id}:{line_name}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            if actual_cost <= reserved_amount:
                # Normal: commit actual, release remainder
                remainder = reserved_amount - actual_cost
                
                await db.execute("""
                    UPDATE budget_lines
                    SET reserved = reserved - ?, spent = spent + ?
                    WHERE line_id = ?
                """, (reserved_amount, actual_cost, line_id))
                
                # Log commit
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, usd_micros,
                     timestamp, reason)
                    VALUES (?, ?, ?, 'commit', ?, ?, datetime('now'), ?)
                """, (line_id, episode_id, job_id, actual_cost, usd_micros, reason))
                
                # Log remainder release if significant
                if remainder > 0.01:
                    await db.execute("""
                        INSERT INTO budget_transactions
                        (line_id, episode_id, txn_type, amount, timestamp, reason)
                        VALUES (?, ?, 'release', ?, datetime('now'), ?)
                    """, (line_id, episode_id, remainder, 
                          f"Unused from reserve: {reason}"))
            else:
                # Overage: commit actual cost, log warning
                overage = actual_cost - reserved_amount
                
                await db.execute("""
                    UPDATE budget_lines
                    SET reserved = reserved - ?, spent = spent + ?
                    WHERE line_id = ?
                """, (reserved_amount, actual_cost, line_id))
                
                # Log commit at actual cost
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, usd_micros,
                     timestamp, reason)
                    VALUES (?, ?, ?, 'commit', ?, ?, datetime('now'), ?)
                """, (line_id, episode_id, job_id, actual_cost, usd_micros, reason))
                
                # Log overage warning
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, ?, 'overage_warning', ?, datetime('now'), ?)
                """, (line_id, episode_id, job_id, overage,
                      f"Cost exceeded reserve by {overage:.2f}: {reason}"))
            
            await db.commit()
    
    async def release(self, episode_id: str, line_name: str, amount: float,
                     reason: str = ""):
        """
        Release reserved budget without committing (e.g., on block or refund).
        Atomic and idempotent: uses BEGIN IMMEDIATE and conditional UPDATE.
        Never allows reserved to go below 0.
        
        Args:
            episode_id: Episode identifier
            line_name: Budget line
            amount: Amount to release (must be > 0)
            reason: Reason for release
        """
        if amount <= 0:
            return  # Nothing to release
        
        line_id = f"{episode_id}:{line_name}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # BEGIN IMMEDIATE for atomic read-modify-write
            await db.execute("BEGIN IMMEDIATE")
            
            try:
                # Conditional UPDATE that checks reserved >= amount atomically
                # This prevents reserved from going negative under concurrency
                cursor = await db.execute("""
                    UPDATE budget_lines
                    SET reserved = reserved - ?
                    WHERE line_id = ? AND reserved >= ?
                """, (amount, line_id, amount))
                
                rows_affected = cursor.rowcount
                
                if rows_affected == 0:
                    # Either line not found or insufficient reserved
                    # Check which case
                    async with db.execute("""
                        SELECT reserved FROM budget_lines WHERE line_id = ?
                    """, (line_id,)) as check_cursor:
                        row = await check_cursor.fetchone()
                        if not row:
                            await db.rollback()
                            raise ValueError(f"Budget line {line_id} not found")
                        
                        current_reserved = row[0]
                        
                        if current_reserved < amount:
                            # Partial release: release what's available
                            await db.execute("""
                                UPDATE budget_lines
                                SET reserved = 0
                                WHERE line_id = ?
                            """, (line_id,))
                            
                            actual_released = current_reserved
                            reason_with_note = f"Partial: requested {amount}, released {actual_released} (all available). {reason}"
                        else:
                            # Race condition: another transaction changed reserved
                            # Log idempotent case
                            actual_released = 0
                            reason_with_note = f"Idempotent: concurrent release. {reason}"
                else:
                    # Full release succeeded
                    actual_released = amount
                    reason_with_note = reason
                
                # Log transaction
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, 'release', ?, datetime('now'), ?)
                """, (line_id, episode_id, actual_released, reason_with_note))
                
                await db.commit()
                
            except Exception as e:
                await db.rollback()
                raise
    
    async def get_line_status(self, episode_id: str, line_name: str) -> dict[str, Any]:
        """
        Get current status of a budget line.
        
        Returns:
            dict with: spent, reserved, budget_cap, stop_threshold, unit,
                      available, at_stop (bool)
        """
        line_id = f"{episode_id}:{line_name}"
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT spent, reserved, budget_cap, stop_threshold, unit, provider
                FROM budget_lines WHERE line_id = ?
            """, (line_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Budget line {line_id} not found")
                
                spent, reserved, cap, stop, unit, provider = row
                total_committed = spent + reserved
                available = stop - total_committed
                at_stop = total_committed >= stop
                
                return {
                    "line_id": line_id,
                    "provider": provider,
                    "spent": spent,
                    "reserved": reserved,
                    "total_committed": total_committed,
                    "budget_cap": cap,
                    "stop_threshold": stop,
                    "available": available,
                    "at_stop": at_stop,
                    "unit": unit,
                }
    
    async def get_episode_summary(self, episode_id: str) -> dict[str, Any]:
        """
        Get budget summary for an episode.
        
        Returns:
            dict with per-provider totals and line details
        """
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Get all lines for episode
            async with db.execute("""
                SELECT line_id, provider, line_name, spent, reserved, 
                       budget_cap, stop_threshold, unit
                FROM budget_lines WHERE episode_id = ?
            """, (episode_id,)) as cursor:
                lines = []
                hf_total = 0
                el_total = 0
                
                async for row in cursor:
                    line_id, provider, name, spent, reserved, cap, stop, unit = row
                    total = spent + reserved
                    
                    lines.append({
                        "line_name": name,
                        "provider": provider,
                        "spent": spent,
                        "reserved": reserved,
                        "total": total,
                        "cap": cap,
                        "stop": stop,
                        "at_stop": total >= stop,
                        "unit": unit,
                    })
                    
                    if provider == "higgsfield":
                        hf_total += total
                    elif provider == "elevenlabs":
                        el_total += total
                
                return {
                    "episode_id": episode_id,
                    "lines": lines,
                    "higgsfield_total": hf_total,
                    "elevenlabs_total": el_total,
                }
