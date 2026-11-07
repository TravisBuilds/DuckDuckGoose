"""Cost tracking: actual provider charges vs estimates.

Tracks real provider costs per job (credits consumed from provider responses or balance deltas)
alongside estimates. Provides visibility into cost accuracy and efficiency.

- Record actual charges (Higgsfield credits, ElevenLabs characters/credits)
- Convert to USD using configured rates
- Track estimate vs actual variance
- Cost per episode, cost per video minute
- Monthly run-rate projections (informational only)
- Passive spend milestone notices (non-blocking)
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional

import aiosqlite

from hfvg.config import config
from hfvg.pricing import micros_to_usd_str, MICROS_PER_USD


def extract_actual_cost_from_provider_job(job_data: dict, job_type: str) -> tuple[Optional[int], Optional[float], str]:
    """
    Extract actual cost from provider job response.
    
    Args:
        job_data: Provider job response (ProviderJob.model_dump())
        job_type: "still", "clip", or "audio"
    
    Returns:
        (actual_usd_micros, provider_credits, cost_source) tuple
        Returns (None, None, "unavailable") if actual cost cannot be determined
    """
    # Check if provider returned cost in USD
    if job_data.get("cost") is not None:
        cost_usd = float(job_data["cost"])
        return (int(cost_usd * MICROS_PER_USD), None, "provider_usd")
    
    # Extract from raw provider data
    raw = job_data.get("raw", {})
    if not raw:
        return (None, None, "unavailable")
    
    # Higgsfield: check for credits in raw data
    if "credits" in raw:
        try:
            credits = float(raw["credits"])
            # Convert Higgsfield credits to USD (0.0475 USD per credit from HARNESS-GATES)
            from hfvg.budget import LEGACY_APP_CREDIT_USD_MICROS
            usd_micros = int(credits * LEGACY_APP_CREDIT_USD_MICROS)
            return (usd_micros, credits, "provider_credits")
        except (ValueError, TypeError):
            pass
    
    # ElevenLabs: check character-cost header or credits
    if job_type == "audio":
        # Try character-cost from response headers
        headers = job_data.get("response_headers", {})
        for key, value in headers.items():
            if key.lower() == "character-cost":
                try:
                    credits = float(value)
                    # Convert ElevenLabs credits to USD (0.0002 USD per credit)
                    from hfvg.budget import ELEVENLABS_CREDIT_USD_MICROS
                    usd_micros = int(credits * ELEVENLABS_CREDIT_USD_MICROS)
                    return (usd_micros, credits, "provider_header")
                except (ValueError, TypeError):
                    pass
        
        # Try credits from raw
        if "credits" in raw:
            try:
                credits = float(raw["credits"])
                from hfvg.budget import ELEVENLABS_CREDIT_USD_MICROS
                usd_micros = int(credits * ELEVENLABS_CREDIT_USD_MICROS)
                return (usd_micros, credits, "provider_credits")
            except (ValueError, TypeError):
                pass
    
    return (None, None, "unavailable")


@dataclass
class CostMetrics:
    """Cost metrics for tracking and visibility."""
    estimated_usd_micros: int
    actual_usd_micros: Optional[int]
    variance_usd_micros: Optional[int]
    variance_percent: Optional[float]
    provider_credits: Optional[float]
    provider_units: Optional[str]


class CostTracker:
    """Track actual costs and compare to estimates."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH

    async def init_db(self):
        """Initialize cost tracking tables."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Enhance job_ledger with actual cost tracking
            # Check if columns exist and add them if needed
            async with db.execute("PRAGMA table_info(job_ledger)") as cur:
                cols = {r[1] async for r in cur}
            
            if "actual_usd_micros" not in cols:
                await db.execute("""
                    ALTER TABLE job_ledger ADD COLUMN actual_usd_micros INTEGER
                """)
            if "provider_credits_consumed" not in cols:
                await db.execute("""
                    ALTER TABLE job_ledger ADD COLUMN provider_credits_consumed REAL
                """)
            if "cost_source" not in cols:
                # Already added by budget.py G-AUD-COST migration, but check
                try:
                    await db.execute("""
                        ALTER TABLE job_ledger ADD COLUMN cost_source TEXT
                    """)
                except aiosqlite.OperationalError:
                    pass  # Column already exists
            
            # Spend milestones (passive notifications)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS spend_milestones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    milestone_type TEXT NOT NULL,
                    threshold_usd_micros INTEGER NOT NULL,
                    user_id TEXT,
                    episode_id TEXT,
                    reached_at TEXT NOT NULL,
                    message TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_spend_milestones_reached 
                ON spend_milestones(reached_at DESC)
            """)
            
            await db.commit()

    async def record_actual_cost(
        self,
        job_id: str,
        actual_usd_micros: int,
        provider_credits: Optional[float] = None,
        cost_source: str = "provider_reported"
    ):
        """
        Record actual cost for a job after completion.
        
        Args:
            job_id: Job identifier
            actual_usd_micros: Actual cost in USD micros
            provider_credits: Provider-specific credits consumed
            cost_source: "provider_reported", "balance_delta", "estimate" (fallback)
        """
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                UPDATE job_ledger 
                SET actual_usd_micros = ?,
                    provider_credits_consumed = ?,
                    cost_source = ?,
                    updated_at = ?
                WHERE job_id = ?
            """, (actual_usd_micros, provider_credits, cost_source,
                  datetime.now(timezone.utc).isoformat(), job_id))
            await db.commit()

    async def get_cost_metrics(self, job_id: str) -> Optional[CostMetrics]:
        """Get cost metrics for a specific job."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT usd_micros, actual_usd_micros, provider_credits_consumed
                FROM job_ledger WHERE job_id = ?
            """, (job_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                estimated, actual, credits = row
                
                if actual is not None:
                    variance = actual - estimated
                    variance_pct = (variance / estimated * 100) if estimated > 0 else 0
                else:
                    variance = None
                    variance_pct = None
                
                return CostMetrics(
                    estimated_usd_micros=estimated,
                    actual_usd_micros=actual,
                    variance_usd_micros=variance,
                    variance_percent=round(variance_pct, 1) if variance_pct is not None else None,
                    provider_credits=credits,
                    provider_units="credits"
                )

    async def get_episode_cost_summary(self, episode_id: str) -> dict:
        """Get cost summary for an episode: estimated, actual, variance."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT 
                    COUNT(*) as job_count,
                    SUM(usd_micros) as total_estimated,
                    SUM(actual_usd_micros) as total_actual,
                    SUM(CASE WHEN actual_usd_micros IS NOT NULL THEN 1 ELSE 0 END) as jobs_with_actual
                FROM job_ledger 
                WHERE episode_id = ?
            """, (episode_id,)) as cursor:
                row = await cursor.fetchone()
                job_count, estimated, actual, with_actual = row
                
                variance = (actual - estimated) if (estimated and actual) else None
                variance_pct = (variance / estimated * 100) if (estimated and variance) else None
                
                return {
                    "episode_id": episode_id,
                    "job_count": job_count or 0,
                    "estimated_usd_micros": estimated or 0,
                    "actual_usd_micros": actual or 0,
                    "variance_usd_micros": variance,
                    "variance_percent": round(variance_pct, 1) if variance_pct else None,
                    "jobs_with_actual_cost": with_actual or 0,
                    "cost_tracking_coverage_percent": round(
                        (with_actual / job_count * 100) if job_count else 0, 1
                    )
                }

    async def get_cost_per_video_minute(self, episode_id: str) -> Optional[dict]:
        """
        Calculate cost per finished video minute for an episode.
        Requires episode metadata with total video duration.
        """
        # Get total cost
        summary = await self.get_episode_cost_summary(episode_id)
        total_cost = summary["actual_usd_micros"]
        
        # Get total video duration (would come from episode metadata/delivery)
        # For now, return structure - implementation depends on where duration is stored
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT details FROM audit_log 
                WHERE episode_id = ? AND action = 'delivery_complete'
                ORDER BY timestamp DESC LIMIT 1
            """, (episode_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                
                # Parse duration from delivery details (if available)
                # Placeholder - adjust based on actual schema
                try:
                    import json
                    details = json.loads(row[0])
                    duration_seconds = details.get("total_duration_seconds")
                    if duration_seconds:
                        minutes = duration_seconds / 60
                        cost_per_minute = total_cost / minutes if minutes > 0 else None
                        return {
                            "episode_id": episode_id,
                            "total_cost_usd_micros": total_cost,
                            "total_minutes": round(minutes, 2),
                            "cost_per_minute_usd_micros": int(cost_per_minute) if cost_per_minute else None,
                            "cost_per_minute_usd": micros_to_usd_str(int(cost_per_minute)) if cost_per_minute else None
                        }
                except (json.JSONDecodeError, KeyError):
                    pass
        
        return None

    async def get_monthly_runrate(
        self,
        user_id: Optional[str] = None
    ) -> dict:
        """
        Calculate monthly spend run-rate projection (informational only).
        Based on actual daily spend rate in current month.
        """
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        days_elapsed = (now - month_start).days + 1
        
        # Days in current month
        if now.month == 12:
            month_end = now.replace(year=now.year + 1, month=1, day=1)
        else:
            month_end = now.replace(month=now.month + 1, day=1)
        days_in_month = (month_end - month_start).days
        
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Get actual spend this month (only where actual_usd_micros is recorded)
            query = """
                SELECT COALESCE(SUM(actual_usd_micros), 0)
                FROM job_ledger
                WHERE created_at >= ? AND created_at < ?
                  AND actual_usd_micros IS NOT NULL
            """
            params = [month_start.isoformat(), month_end.isoformat()]
            
            if user_id:
                query += " AND user_id = ?"
                params.append(user_id)
            
            async with db.execute(query, tuple(params)) as cursor:
                current_spend = (await cursor.fetchone())[0]
        
        if days_elapsed == 0:
            daily_rate = 0
            projected = current_spend
        else:
            daily_rate = current_spend / days_elapsed
            projected = int(daily_rate * days_in_month)
        
        return {
            "month": month_start.strftime("%Y-%m"),
            "days_elapsed": days_elapsed,
            "days_in_month": days_in_month,
            "current_spend_usd_micros": current_spend,
            "current_spend_usd": micros_to_usd_str(current_spend),
            "daily_rate_usd_micros": int(daily_rate),
            "daily_rate_usd": micros_to_usd_str(int(daily_rate)),
            "projected_month_end_usd_micros": projected,
            "projected_month_end_usd": micros_to_usd_str(projected),
        }

    async def check_spend_milestones(
        self,
        current_spend_usd_micros: int,
        user_id: Optional[str] = None,
        episode_id: Optional[str] = None
    ):
        """
        Check and record passive spend milestone notices (non-blocking).
        
        Milestones: $100, $500, $1000, $5000, $10000 USD
        """
        milestones = [
            100_000_000,   # $100
            500_000_000,   # $500
            1_000_000_000, # $1000
            5_000_000_000, # $5000
            10_000_000_000 # $10000
        ]
        
        for threshold in milestones:
            if current_spend_usd_micros >= threshold:
                # Check if milestone already recorded
                async with aiosqlite.connect(self.db_path, uri=True) as db:
                    async with db.execute("""
                        SELECT id FROM spend_milestones
                        WHERE milestone_type = ?
                          AND threshold_usd_micros = ?
                          AND COALESCE(user_id, '') = COALESCE(?, '')
                          AND COALESCE(episode_id, '') = COALESCE(?, '')
                    """, ("spend", threshold, user_id or '', episode_id or '')) as cursor:
                        if await cursor.fetchone():
                            continue  # Already recorded
                    
                    # Record milestone
                    scope = []
                    if user_id:
                        scope.append(f"user {user_id}")
                    if episode_id:
                        scope.append(f"episode {episode_id}")
                    scope_str = " for " + " ".join(scope) if scope else ""
                    
                    message = (
                        f"Spend milestone reached{scope_str}: "
                        f"{micros_to_usd_str(threshold)} USD"
                    )
                    
                    await db.execute("""
                        INSERT INTO spend_milestones 
                        (milestone_type, threshold_usd_micros, user_id, episode_id, 
                         reached_at, message)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, ("spend", threshold, user_id, episode_id,
                          datetime.now(timezone.utc).isoformat(), message))
                    await db.commit()

    async def get_recent_milestones(
        self,
        user_id: Optional[str] = None,
        limit: int = 20
    ) -> list[dict]:
        """Get recent spend milestone notices."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            query = """
                SELECT id, milestone_type, threshold_usd_micros, user_id, 
                       episode_id, reached_at, message
                FROM spend_milestones
            """
            params = []
            
            if user_id:
                query += " WHERE user_id = ? OR user_id IS NULL"
                params.append(user_id)
            
            query += " ORDER BY reached_at DESC LIMIT ?"
            params.append(limit)
            
            async with db.execute(query, tuple(params)) as cursor:
                return [dict(row) async for row in cursor]
