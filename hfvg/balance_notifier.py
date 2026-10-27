"""Balance alert notification system: email, webhook, and deduplication.

Alerts fire on:
- Low balance (below threshold per provider)
- Critical balance
- Stale manual Higgsfield entry
- Balance that didn't recover after expected auto top-up
- 403 Insufficient Balance stop
- Optional daily/monthly spend summary
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any

import aiosqlite
import httpx


# Environment configuration
SMTP_HOST = os.getenv("BALANCE_ALERT_SMTP_HOST")
SMTP_PORT = int(os.getenv("BALANCE_ALERT_SMTP_PORT", "587"))
SMTP_USER = os.getenv("BALANCE_ALERT_SMTP_USER")
SMTP_PASSWORD = os.getenv("BALANCE_ALERT_SMTP_PASSWORD")
SMTP_FROM = os.getenv("BALANCE_ALERT_SMTP_FROM", SMTP_USER)
SMTP_TO = os.getenv("BALANCE_ALERT_SMTP_TO", "").split(",") if os.getenv("BALANCE_ALERT_SMTP_TO") else []

WEBHOOK_URL = os.getenv("BALANCE_ALERT_WEBHOOK_URL")

# Deduplication: same alert type + key within this window is suppressed
ALERT_DEDUPE_WINDOW_SECONDS = int(os.getenv("BALANCE_ALERT_DEDUPE_WINDOW_SECONDS", str(3600)))

# Spend summary configuration
DAILY_SPEND_CEILING_USD = float(os.getenv("DAILY_SPEND_CEILING_USD", "0")) if os.getenv("DAILY_SPEND_CEILING_USD") else None
MONTHLY_SPEND_CEILING_USD = float(os.getenv("MONTHLY_SPEND_CEILING_USD", "0")) if os.getenv("MONTHLY_SPEND_CEILING_USD") else None


@dataclass
class Alert:
    """An alert to send."""
    alert_type: str
    severity: str  # info, warning, error
    subject: str
    message: str
    details: dict[str, Any]
    
    def dedupe_key(self) -> str:
        """Key for deduplication (same key within window is suppressed)."""
        key_data = f"{self.alert_type}:{self.subject}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]


class BalanceNotifier:
    """Alert notification system."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def send_low_balance_alert(self, provider: str, balance_usd: float, threshold_usd: float) -> None:
        """Send low balance alert."""
        alert = Alert(
            alert_type="low_balance",
            severity="warning",
            subject=f"{provider.capitalize()} balance low",
            message=f"{provider.capitalize()} balance is ${balance_usd:.2f}, below threshold ${threshold_usd:.2f}",
            details={"provider": provider, "balance_usd": balance_usd, "threshold_usd": threshold_usd},
        )
        await self._send_alert(alert)
    
    async def send_critical_balance_alert(self, provider: str, balance_usd: float, threshold_usd: float) -> None:
        """Send critical balance alert."""
        alert = Alert(
            alert_type="critical_balance",
            severity="error",
            subject=f"{provider.capitalize()} balance CRITICAL",
            message=f"{provider.capitalize()} balance is ${balance_usd:.2f}, CRITICALLY LOW (threshold ${threshold_usd:.2f})",
            details={"provider": provider, "balance_usd": balance_usd, "threshold_usd": threshold_usd},
        )
        await self._send_alert(alert)
    
    async def send_stale_balance_alert(self, provider: str, age_hours: float, max_age_hours: int) -> None:
        """Send stale manual balance entry alert."""
        alert = Alert(
            alert_type="stale_balance",
            severity="warning",
            subject=f"{provider.capitalize()} balance entry is stale",
            message=(
                f"{provider.capitalize()} manual balance entry is {age_hours:.1f} hours old, "
                f"exceeding maximum age of {max_age_hours} hours. Update it via POST /api/budget/balance"
            ),
            details={"provider": provider, "age_hours": age_hours, "max_age_hours": max_age_hours},
        )
        await self._send_alert(alert)
    
    async def send_403_stop_alert(
        self,
        provider: str,
        episode_id: str,
        job_id: str,
        error_message: str,
    ) -> None:
        """Send 403 Insufficient Balance stop alert."""
        alert = Alert(
            alert_type="403_stop",
            severity="error",
            subject=f"{provider.capitalize()} 403 Insufficient Balance",
            message=(
                f"Episode {episode_id} stopped: {provider} returned 403 Insufficient Balance.\n"
                f"Job: {job_id}\n"
                f"Error: {error_message}\n\n"
                f"Action: Add funds to {provider} account and resume the workflow."
            ),
            details={
                "provider": provider,
                "episode_id": episode_id,
                "job_id": job_id,
                "error_message": error_message,
            },
        )
        await self._send_alert(alert)
    
    async def send_auto_topup_failed_alert(self, provider: str, expected_recovery_usd: float) -> None:
        """Send alert when balance didn't recover after expected auto top-up."""
        alert = Alert(
            alert_type="auto_topup_failed",
            severity="error",
            subject=f"{provider.capitalize()} auto top-up may have failed",
            message=(
                f"{provider.capitalize()} balance did not recover as expected after auto top-up should have triggered.\n"
                f"Expected recovery: ${expected_recovery_usd:.2f}\n\n"
                f"Action: Check {provider} billing dashboard and top up manually if needed."
            ),
            details={"provider": provider, "expected_recovery_usd": expected_recovery_usd},
        )
        await self._send_alert(alert)
    
    async def send_spend_summary_alert(
        self,
        period: str,  # "daily" or "monthly"
        spend_usd: float,
        ceiling_usd: float | None,
    ) -> None:
        """Send spend summary alert."""
        severity = "warning" if ceiling_usd and spend_usd > ceiling_usd else "info"
        ceiling_note = f" (ceiling: ${ceiling_usd:.2f})" if ceiling_usd else ""
        
        alert = Alert(
            alert_type=f"{period}_spend_summary",
            severity=severity,
            subject=f"{period.capitalize()} spend summary",
            message=f"{period.capitalize()} spend: ${spend_usd:.2f}{ceiling_note}",
            details={"period": period, "spend_usd": spend_usd, "ceiling_usd": ceiling_usd},
        )
        await self._send_alert(alert)
    
    async def _send_alert(self, alert: Alert) -> None:
        """Send an alert through all configured channels (with deduplication)."""
        # Check deduplication
        dedupe_key = alert.dedupe_key()
        if await self._is_recent_duplicate(dedupe_key):
            return
        
        # Record alert
        await self._record_alert(alert, dedupe_key)
        
        # Send through channels
        tasks = []
        
        if SMTP_HOST and SMTP_TO:
            tasks.append(self._send_email(alert))
        
        if WEBHOOK_URL:
            tasks.append(self._send_webhook(alert))
        
        # Always log
        self._log_alert(alert)
        
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _is_recent_duplicate(self, dedupe_key: str) -> bool:
        """Check if this alert was recently sent (within deduplication window)."""
        cutoff = datetime.now(timezone.utc).timestamp() - ALERT_DEDUPE_WINDOW_SECONDS
        
        async with aiosqlite.connect(self.db_path) as db:
            row = await (await db.execute(
                "SELECT COUNT(*) FROM balance_alerts WHERE dedupe_key = ? AND sent_at_unix > ?",
                (dedupe_key, cutoff),
            )).fetchone()
        
        return row[0] > 0 if row else False
    
    async def _record_alert(self, alert: Alert, dedupe_key: str) -> None:
        """Record sent alert in database."""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO balance_alerts (alert_type, severity, subject, message, details, dedupe_key, sent_at_unix) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    alert.alert_type,
                    alert.severity,
                    alert.subject,
                    alert.message,
                    json.dumps(alert.details),
                    dedupe_key,
                    datetime.now(timezone.utc).timestamp(),
                ),
            )
            await db.commit()
    
    async def _send_email(self, alert: Alert) -> None:
        """Send alert via email."""
        try:
            msg = MIMEMultipart()
            msg["From"] = SMTP_FROM
            msg["To"] = ", ".join(SMTP_TO)
            msg["Subject"] = f"[{alert.severity.upper()}] {alert.subject}"
            
            body = f"{alert.message}\n\n"
            if alert.details:
                body += "Details:\n"
                for key, value in alert.details.items():
                    body += f"  {key}: {value}\n"
            
            msg.attach(MIMEText(body, "plain"))
            
            # Send in a thread pool to avoid blocking
            await asyncio.to_thread(self._send_email_sync, msg)
        except Exception as e:
            self._log_alert_error(f"Email send failed: {e}")
    
    def _send_email_sync(self, msg: MIMEMultipart) -> None:
        """Send email synchronously."""
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            if SMTP_USER and SMTP_PASSWORD:
                server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    
    async def _send_webhook(self, alert: Alert) -> None:
        """Send alert via webhook (e.g., Slack incoming webhook)."""
        try:
            payload = {
                "text": f"*[{alert.severity.upper()}] {alert.subject}*\n{alert.message}",
                "attachments": [
                    {
                        "color": self._severity_color(alert.severity),
                        "fields": [
                            {"title": key, "value": str(value), "short": True}
                            for key, value in alert.details.items()
                        ],
                    }
                ],
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(WEBHOOK_URL, json=payload)
                resp.raise_for_status()
        except Exception as e:
            self._log_alert_error(f"Webhook send failed: {e}")
    
    def _severity_color(self, severity: str) -> str:
        """Map severity to Slack attachment color."""
        return {"info": "good", "warning": "warning", "error": "danger"}.get(severity, "good")
    
    def _log_alert(self, alert: Alert) -> None:
        """Log alert (always done regardless of other channels)."""
        import logging
        logger = logging.getLogger("hfvg.balance_notifier")
        level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(
            alert.severity, logging.INFO
        )
        logger.log(level, f"[{alert.alert_type}] {alert.subject}: {alert.message}")
    
    def _log_alert_error(self, message: str) -> None:
        """Log alert delivery error."""
        import logging
        logging.getLogger("hfvg.balance_notifier").error(message)


async def init_notifier_tables(db_path: str) -> None:
    """Initialize alert notification tables."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS balance_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                subject TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                sent_at_unix REAL NOT NULL
            )
        """)
        
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_balance_alerts_dedupe
            ON balance_alerts(dedupe_key, sent_at_unix)
        """)
        
        await db.commit()
