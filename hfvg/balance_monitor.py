"""Provider balance monitoring: live balance checks, preflight gates, and alerts.

This module tracks provider account balances to prevent mid-episode stalls from exhausted credits.
Each provider has different APIs and monitoring strategies:

- **ElevenLabs**: GET /v1/user/subscription returns character_count, character_limit, and status.
  Remaining = character_limit - character_count. No direct dollar balance endpoint for PAYG.
  
- **Higgsfield**: No live balance endpoint. Estimated remaining = manual entry - max(harness
  ledger committed spend, /models/usage reported spend). Both numbers are shown; disagreement
  is flagged.

Balance checks run:
1. Before any paid stage (preflight gate): block when known balance < estimate + reserve
2. Periodically via Temporal schedule (default hourly)
3. On demand via CLI (`hfvg balance`) or API (GET /api/balance)

All balance data is labeled 'live', 'derived', or 'manual' with age in seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import aiosqlite
import httpx

from hfvg.config import config
from hfvg.pricing import micros_to_usd_str, usd_to_micros, MICROS_PER_USD


# Environment configuration
HIGGSFIELD_BALANCE_MAX_AGE_HOURS = int(os.getenv("HFVG_BALANCE_MAX_AGE_HOURS", "24"))
BALANCE_RESERVE_PERCENT = int(os.getenv("BALANCE_RESERVE_PERCENT", "20"))
HIGGSFIELD_AUTO_TOPUP_ENABLED = os.getenv("HIGGSFIELD_AUTO_TOPUP_ENABLED", "false").lower() == "true"
ELEVENLABS_AUTO_TOPUP_ENABLED = os.getenv("ELEVENLABS_AUTO_TOPUP_ENABLED", "false").lower() == "true"

# Alert thresholds
BALANCE_LOW_THRESHOLD_USD = float(os.getenv("BALANCE_LOW_THRESHOLD_USD", "10.0"))
BALANCE_CRITICAL_THRESHOLD_USD = float(os.getenv("BALANCE_CRITICAL_THRESHOLD_USD", "5.0"))


BalanceSource = Literal["live", "derived", "manual"]


@dataclass
class ProviderBalance:
    """Balance snapshot for one provider."""
    provider: str
    balance_usd: Decimal | None  # None for unknown/error states (e.g. 401 auth failures)
    source: BalanceSource
    age_seconds: float
    details: dict[str, Any]
    
    def to_dict(self) -> dict[str, Any]:
        result = {
            **asdict(self),
            "balance_usd": str(self.balance_usd) if self.balance_usd is not None else None,
        }
        if self.balance_usd is not None:
            result["balance_usd_micros"] = int(self.balance_usd * MICROS_PER_USD)
        return result


@dataclass
class BalanceCheckResult:
    """Result of checking all provider balances."""
    timestamp: datetime
    providers: dict[str, ProviderBalance]
    warnings: list[str]
    errors: list[str]
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "warnings": self.warnings,
            "errors": self.errors,
        }


class BalanceMonitor:
    """Provider balance monitor."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def check_all_balances(self) -> BalanceCheckResult:
        """Check balances for all configured providers."""
        timestamp = datetime.now(timezone.utc)
        providers = {}
        warnings = []
        errors = []
        
        # Check ElevenLabs
        try:
            el_balance = await self._check_elevenlabs_balance()
            providers["elevenlabs"] = el_balance
            
            # Check thresholds - skip if balance is None (unknown/error state like 401)
            if el_balance.balance_usd is not None:
                balance_usd = float(el_balance.balance_usd)
                if balance_usd < BALANCE_CRITICAL_THRESHOLD_USD:
                    errors.append(f"ElevenLabs balance critically low: ${balance_usd:.2f} < ${BALANCE_CRITICAL_THRESHOLD_USD:.2f}")
                    # Defect 10: Send critical balance alert
                    from hfvg.balance_notifier import BalanceNotifier
                    notifier = BalanceNotifier(self.db_path)
                    await notifier.send_critical_balance_alert("elevenlabs", balance_usd, BALANCE_CRITICAL_THRESHOLD_USD)
                elif balance_usd < BALANCE_LOW_THRESHOLD_USD:
                    warnings.append(f"ElevenLabs balance low: ${balance_usd:.2f} < ${BALANCE_LOW_THRESHOLD_USD:.2f}")
                    # Defect 10: Send low balance alert
                    from hfvg.balance_notifier import BalanceNotifier
                    notifier = BalanceNotifier(self.db_path)
                    await notifier.send_low_balance_alert("elevenlabs", balance_usd, BALANCE_LOW_THRESHOLD_USD)
            elif "error" in el_balance.details:
                # Unknown balance (e.g. 401) - warn but don't treat as critical
                warnings.append(f"ElevenLabs balance check: {el_balance.details['error']}")
        except Exception as e:
            errors.append(f"ElevenLabs balance check failed: {e}")
        
        # Check Higgsfield
        try:
            hf_balance = await self._check_higgsfield_balance()
            providers["higgsfield"] = hf_balance
            
            # Check thresholds and staleness
            balance_usd = float(hf_balance.balance_usd)
            age_hours = hf_balance.age_seconds / 3600
            
            if age_hours > HIGGSFIELD_BALANCE_MAX_AGE_HOURS:
                warnings.append(
                    f"Higgsfield balance is stale ({age_hours:.1f}h > {HIGGSFIELD_BALANCE_MAX_AGE_HOURS}h): "
                    f"${balance_usd:.2f} from manual entry"
                )
                # Defect 10: Send stale balance alert
                from hfvg.balance_notifier import BalanceNotifier
                notifier = BalanceNotifier(self.db_path)
                await notifier.send_stale_balance_alert("higgsfield", age_hours, HIGGSFIELD_BALANCE_MAX_AGE_HOURS)
            
            if balance_usd < BALANCE_CRITICAL_THRESHOLD_USD:
                errors.append(f"Higgsfield balance critically low: ${balance_usd:.2f} < ${BALANCE_CRITICAL_THRESHOLD_USD:.2f}")
                # Defect 10: Send critical balance alert
                from hfvg.balance_notifier import BalanceNotifier
                notifier = BalanceNotifier(self.db_path)
                await notifier.send_critical_balance_alert("higgsfield", balance_usd, BALANCE_CRITICAL_THRESHOLD_USD)
            elif balance_usd < BALANCE_LOW_THRESHOLD_USD:
                warnings.append(f"Higgsfield balance low: ${balance_usd:.2f} < ${BALANCE_LOW_THRESHOLD_USD:.2f}")
                # Defect 10: Send low balance alert
                from hfvg.balance_notifier import BalanceNotifier
                notifier = BalanceNotifier(self.db_path)
                await notifier.send_low_balance_alert("higgsfield", balance_usd, BALANCE_LOW_THRESHOLD_USD)
            
            # Check for disagreement between ledger and usage API
            details = hf_balance.details
            if "ledger_spend" in details and "usage_api_spend" in details:
                ledger = float(details["ledger_spend"])
                usage = float(details["usage_api_spend"])
                diff = abs(ledger - usage)
                if diff > 1.0:  # More than $1 disagreement
                    warnings.append(
                        f"Higgsfield spend disagreement: ledger ${ledger:.2f} vs usage API ${usage:.2f} (diff ${diff:.2f})"
                    )
        except Exception as e:
            errors.append(f"Higgsfield balance check failed: {e}")
        
        return BalanceCheckResult(
            timestamp=timestamp,
            providers=providers,
            warnings=warnings,
            errors=errors,
        )
    
    async def _check_elevenlabs_balance(self) -> ProviderBalance:
        """Check ElevenLabs subscription balance (character limit - count)."""
        api_key = os.getenv("ELEVENLABS_API_KEY")
        
        # Missing key means unknown balance
        if not api_key:
            return ProviderBalance(
                provider="elevenlabs",
                balance_usd=None,
                source="live",
                age_seconds=0.0,
                details={
                    "error": "ELEVENLABS_API_KEY not set",
                },
            )
        
        api_base = os.getenv("ELEVENLABS_API_BASE", "https://api.elevenlabs.io")
        url = f"{api_base}/v1/user/subscription"
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers={"xi-api-key": api_key})
            
            if resp.status_code == 401:
                # Defect 12: 401 is status 'unknown' with NO numeric balance - doesn't block paid steps
                # or trigger critical alerts. A warning about the bad key is fine.
                # Unknown balance (not $0)
                return ProviderBalance(
                    provider="elevenlabs",
                    balance_usd=None,
                    source="live",
                    age_seconds=0.0,
                    details={
                        "error": "401 Unauthorized - API key may lack subscription read permission",
                        "status_code": 401,
                    },
                )
            
            resp.raise_for_status()
            data = resp.json()
        
        # Extract character usage
        character_limit = int(data.get("character_limit", 0))
        character_count = int(data.get("character_count", 0))
        remaining_chars = max(0, character_limit - character_count)
        
        # Convert to USD estimate (using default rate)
        from hfvg.providers.elevenlabs import DEFAULT_USD_PER_CREDIT, MAX_CREDITS_PER_CHAR
        usd_per_credit = Decimal(os.getenv("ELEVENLABS_USD_PER_CREDIT", str(DEFAULT_USD_PER_CREDIT)))
        # Character limit is in terms of credits, roughly
        remaining_usd = Decimal(remaining_chars) * Decimal(str(MAX_CREDITS_PER_CHAR)) * usd_per_credit
        
        return ProviderBalance(
            provider="elevenlabs",
            balance_usd=remaining_usd,
            source="live",
            age_seconds=0.0,
            details={
                "character_limit": character_limit,
                "character_count": character_count,
                "remaining_characters": remaining_chars,
                "current_overage": data.get("current_overage"),
                "status": data.get("status"),
            },
        )
    
    async def _check_higgsfield_balance(self) -> ProviderBalance:
        """Check Higgsfield estimated balance (manual entry - committed spend)."""
        # Get manual balance entry (using existing manual_balance table from budget.py)
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT balance_usd_micros, set_at FROM manual_balance "
                "ORDER BY id DESC LIMIT 1"
            )).fetchone()
        
        if not row:
            raise ValueError("No manual Higgsfield balance entry found (set via POST /api/budget/balance)")
        
        manual_usd_micros, set_at_iso = row
        # Parse ISO timestamp to unix timestamp
        from datetime import datetime as dt
        set_at_dt = dt.fromisoformat(set_at_iso.replace("Z", "+00:00"))
        recorded_at = set_at_dt.timestamp()
        age_seconds = datetime.now(timezone.utc).timestamp() - recorded_at
        manual_usd = Decimal(manual_usd_micros) / MICROS_PER_USD
        
        # Get committed spend since manual entry  
        # Defect 14: only Higgsfield spend (L1-L4 video lines, L7_revision), NOT ElevenLabs (el_*)
        # The after_txn_id in manual_balance tells us which transactions were already accounted for
        async with aiosqlite.connect(self.db_path) as db:
            # Get transactions after the manual entry's after_txn_id
            after_txn_id = await (await db.execute(
                "SELECT after_txn_id FROM manual_balance ORDER BY id DESC LIMIT 1"
            )).fetchone()
            after_txn_id = after_txn_id[0] if after_txn_id else 0
            
            ledger_row = await (await db.execute(
                "SELECT COALESCE(SUM(bt.amount), 0) FROM budget_transactions bt "
                "JOIN budget_lines bl ON bt.line_id = bl.line_id "
                "WHERE bt.txn_type = 'commit' AND bt.txn_id > ? "
                "AND bl.line_name NOT LIKE 'el_%'",
                (after_txn_id,)
            )).fetchone()
            ledger_spend_micros = ledger_row[0] if ledger_row else 0
        
        ledger_spend = Decimal(ledger_spend_micros) / MICROS_PER_USD
        
        # Try to get usage from Higgsfield API
        usage_api_spend = None
        try:
            usage_api_spend = await self._check_higgsfield_usage_api(recorded_at)
        except Exception as e:
            # Task 8: Log errors (redacted) instead of swallowing them
            # Redact sensitive data from error message
            error_msg = str(e)
            # Redact API keys and tokens
            import re
            error_msg = re.sub(r'Key [^:]+:[^\s,]+', 'Key [REDACTED]', error_msg)
            error_msg = re.sub(r'Bearer [^\s,]+', 'Bearer [REDACTED]', error_msg)
            import logging
            logging.getLogger(__name__).warning(f"Higgsfield usage API check failed: {error_msg}")
        
        # Estimated remaining = manual - max(ledger, usage)
        max_spend = max(ledger_spend, usage_api_spend or Decimal("0.0"))
        estimated_remaining = manual_usd - max_spend
        
        details = {
            "manual_entry_usd": str(manual_usd),
            "manual_entry_age_hours": age_seconds / 3600,
            "ledger_spend": str(ledger_spend),
        }
        
        if usage_api_spend is not None:
            details["usage_api_spend"] = str(usage_api_spend)
            details["max_spend"] = str(max_spend)
            if abs(ledger_spend - usage_api_spend) > Decimal("1.0"):
                details["spend_disagreement"] = True
        
        return ProviderBalance(
            provider="higgsfield",
            balance_usd=estimated_remaining,
            source="derived",
            age_seconds=age_seconds,
            details=details,
        )
    
    async def _check_higgsfield_usage_api(self, since_unix: float) -> Decimal | None:
        """Query Higgsfield /models/usage for spend since a timestamp."""
        # Parse HIGGSFIELD_API_KEY (format: "id:secret")
        api_key = os.getenv("HIGGSFIELD_API_KEY", "").strip()
        if not api_key:
            return None
        
        if ":" not in api_key:
            return None
        
        api_id, api_secret = api_key.split(":", 1)
        
        if not api_id or not api_secret:
            return None
        
        api_base = os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
        url = f"{api_base}/models/usage"
        
        # Convert unix timestamp to date range (API requires start/end dates, max 31 days)
        from datetime import datetime as dt
        start_date = dt.fromtimestamp(since_unix, tz=timezone.utc).strftime("%Y-%m-%d")
        end_date = dt.now(timezone.utc).strftime("%Y-%m-%d")
        
        # Calculate day span (max 31)
        days = (dt.now(timezone.utc) - dt.fromtimestamp(since_unix, tz=timezone.utc)).days
        if days > 31:
            # Truncate to last 31 days
            start_date = (dt.now(timezone.utc) - __import__("datetime").timedelta(days=31)).strftime("%Y-%m-%d")
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                params={"start": start_date, "end": end_date, "timeframe": "day"},
                headers={"Authorization": f"Key {api_id}:{api_secret}"},
            )
            resp.raise_for_status()
            data = resp.json()
        
        # Sum USD across all models and days
        total_usd = Decimal("0.0")
        for model_usage in data.get("usage", []):
            for day_entry in model_usage.get("data", []):
                usd = day_entry.get("usd")
                if usd:
                    total_usd += Decimal(str(usd))
        
        return total_usd
    
    async def _check_single_provider_balance(self, provider: str) -> ProviderBalance | None:
        """Check balance for a single provider (optimized for preflight gates).
        
        Returns:
            ProviderBalance or None if provider unknown
        """
        if provider == "elevenlabs":
            try:
                return await self._check_elevenlabs_balance()
            except Exception:
                return None
        elif provider == "higgsfield":
            try:
                return await self._check_higgsfield_balance()
            except Exception:
                return None
        else:
            return None
    
    async def check_preflight_balance(
        self,
        provider: str,
        estimate_usd_micros: int,
        auto_topup_enabled: bool = False,
    ) -> tuple[bool, str]:
        """Preflight balance gate: block if KNOWN balance < estimate + reserve.
        
        Defect 8: None/unknown balance NEVER blocks (permission errors, etc).
        Only block when we have a numeric balance that's too low.
        
        Task 11: Check only the specified provider, not all providers.
        
        Returns:
            (passed, reason): True if check passed, False + reason if blocked.
        """
        # Check only the provider this step spends from (not all providers)
        balance = await self._check_single_provider_balance(provider)
        
        # No-op when provider not found or has no recorded balance
        if balance is None:
            return (True, f"Balance unavailable for {provider}, gate is no-op")
        
        # Defect 8: None (unknown) balance never blocks (gate is no-op)
        if balance.balance_usd is None:
            return (True, f"{provider} balance unknown (gate is no-op): {balance.details.get('error', 'no error')}")
        
        estimate_usd = Decimal(estimate_usd_micros) / MICROS_PER_USD
        reserve = estimate_usd * Decimal(BALANCE_RESERVE_PERCENT) / Decimal("100")
        required = estimate_usd + reserve
        
        # Check staleness for Higgsfield: warn but don't block
        if provider == "higgsfield":
            age_hours = balance.age_seconds / 3600
            if age_hours > HIGGSFIELD_BALANCE_MAX_AGE_HOURS:
                # Stale Higgsfield entry warns, it doesn't block
                return (True, (
                    f"WARNING: Higgsfield balance is stale ({age_hours:.1f}h > {HIGGSFIELD_BALANCE_MAX_AGE_HOURS}h): "
                    f"${balance.balance_usd:.2f}. Update it via POST /api/budget/balance"
                ))
        
        # Check balance sufficiency
        if balance.balance_usd < required:
            if auto_topup_enabled:
                # With auto top-up, only warn but allow
                return (True, (
                    f"WARNING: {provider} balance ${balance.balance_usd:.2f} < required ${required:.2f} "
                    f"(estimate ${estimate_usd:.2f} + {BALANCE_RESERVE_PERCENT}% reserve), "
                    f"but auto top-up is enabled"
                ))
            return (False, (
                f"{provider} balance insufficient: ${balance.balance_usd:.2f} < required ${required:.2f} "
                f"(estimate ${estimate_usd:.2f} + {BALANCE_RESERVE_PERCENT}% reserve)"
            ))
        
        return (True, f"{provider} balance OK: ${balance.balance_usd:.2f} >= ${required:.2f}")
    
    async def record_403_stop(
        self,
        provider: str,
        episode_id: str,
        job_id: str,
        error_message: str,
    ) -> None:
        """Record a 403 Insufficient Balance stop for alerting."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO balance_stops (provider, episode_id, job_id, error_message, recorded_at_unix) "
                "VALUES (?, ?, ?, ?, ?)",
                (provider, episode_id, job_id, error_message, datetime.now(timezone.utc).timestamp()),
            )
            await db.commit()


async def init_balance_tables(db_path: str) -> None:
    """Initialize balance monitoring tables."""
    async with aiosqlite.connect(db_path) as db:
        # 403 stop events
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balance_stops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                episode_id TEXT NOT NULL,
                job_id TEXT NOT NULL,
                error_message TEXT NOT NULL,
                recorded_at_unix REAL NOT NULL
            )
        """)
        
        await db.commit()
