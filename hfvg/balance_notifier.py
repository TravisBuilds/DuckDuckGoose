"""Balance alert notification system: email, webhook, and deduplication.

Alerts fire on:
- Low balance (below threshold per provider)
- Critical balance
- Stale manual Higgsfield entry
- A Higgsfield balance entry that did not move although the studio spent since the previous one
- Balance that didn't recover after expected auto top-up
- 403 Insufficient Balance stop
- An admin's test alert (dashboard button / POST /api/alerts/test)

Destinations come from the environment at send time (docs/BALANCE-ALERTS.md, .env.example):
SMTP email (BALANCE_ALERT_SMTP_*) and/or a generic JSON webhook (BALANCE_ALERT_WEBHOOK_URL). Every
alert is recorded in balance_alerts with the channels tried and each channel's outcome, delivered or
not, so a missing or broken destination is visible on the dashboard instead of a silent no-op.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any, Mapping
from urllib.parse import urlsplit

import aiosqlite
import httpx

logger = logging.getLogger("hfvg.balance_notifier")

DEFAULT_DEDUPE_WINDOW_SECONDS = 3600
TEST_ALERT_DEDUPE_WINDOW_SECONDS = 30

# Spend summary configuration
DAILY_SPEND_CEILING_USD = float(os.getenv("DAILY_SPEND_CEILING_USD", "0")) if os.getenv("DAILY_SPEND_CEILING_USD") else None
MONTHLY_SPEND_CEILING_USD = float(os.getenv("MONTHLY_SPEND_CEILING_USD", "0")) if os.getenv("MONTHLY_SPEND_CEILING_USD") else None


def _flag(raw: str | None, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class AlertConfig:
    """Alert destinations read from the environment, with what is wrong with them."""
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_to: tuple[str, ...] = ()
    smtp_ssl: bool = False
    smtp_starttls: bool = True
    webhook_url: str | None = None
    dedupe_window_seconds: int = DEFAULT_DEDUPE_WINDOW_SECONDS
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def email_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_to and self.smtp_from) and not any(
            p.startswith("email:") for p in self.problems)

    @property
    def webhook_ready(self) -> bool:
        return bool(self.webhook_url) and not any(p.startswith("webhook:") for p in self.problems)

    @property
    def channels(self) -> list[str]:
        return [name for name, ready in (("email", self.email_ready), ("webhook", self.webhook_ready)) if ready]

    def fingerprint(self) -> str:
        """Changes when the destinations change, so a new destination is not suppressed by alerts
        recorded while it was missing."""
        parts = []
        if self.email_ready:
            parts.append(f"email:{self.smtp_host}:{','.join(sorted(self.smtp_to))}")
        if self.webhook_ready:
            parts.append(f"webhook:{hashlib.sha256((self.webhook_url or '').encode()).hexdigest()[:12]}")
        return "|".join(parts) or "none"


def load_alert_config(env: Mapping[str, str] | None = None) -> AlertConfig:
    """The alert destinations configured now (read at call time, so a deploy or test that changes
    the environment is honoured without a restart of the importing process)."""
    env = os.environ if env is None else env
    problems: list[str] = []
    get = lambda key: (env.get(key) or "").strip() or None  # noqa: E731

    host = get("BALANCE_ALERT_SMTP_HOST")
    user = get("BALANCE_ALERT_SMTP_USER")
    password = get("BALANCE_ALERT_SMTP_PASSWORD")
    sender = get("BALANCE_ALERT_SMTP_FROM") or user
    to = tuple(a.strip() for a in (get("BALANCE_ALERT_SMTP_TO") or "").split(",") if a.strip())
    ssl_on = _flag(env.get("BALANCE_ALERT_SMTP_SSL"), False)
    port_raw = get("BALANCE_ALERT_SMTP_PORT")
    port = 465 if ssl_on else 587
    if port_raw:
        try:
            port = int(port_raw)
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            problems.append(f"email: BALANCE_ALERT_SMTP_PORT must be a port number, got {port_raw!r}")
    if host or to:
        if not host:
            problems.append("email: BALANCE_ALERT_SMTP_TO is set but BALANCE_ALERT_SMTP_HOST is not")
        if not to:
            problems.append("email: BALANCE_ALERT_SMTP_HOST is set but BALANCE_ALERT_SMTP_TO is empty")
        elif any("@" not in a for a in to):
            problems.append("email: BALANCE_ALERT_SMTP_TO must be comma-separated email addresses")
        if not sender:
            problems.append("email: set BALANCE_ALERT_SMTP_FROM (or BALANCE_ALERT_SMTP_USER)")
        if bool(user) != bool(password):
            problems.append("email: BALANCE_ALERT_SMTP_USER and BALANCE_ALERT_SMTP_PASSWORD go together")

    webhook = get("BALANCE_ALERT_WEBHOOK_URL")
    if webhook:
        parts = urlsplit(webhook)
        if parts.scheme not in ("https", "http") or not parts.netloc:
            problems.append("webhook: BALANCE_ALERT_WEBHOOK_URL must be an http(s) URL")

    window = DEFAULT_DEDUPE_WINDOW_SECONDS
    window_raw = get("BALANCE_ALERT_DEDUPE_WINDOW_SECONDS")
    if window_raw:
        try:
            window = int(window_raw)
            if window < 0:
                raise ValueError
        except ValueError:
            problems.append(f"BALANCE_ALERT_DEDUPE_WINDOW_SECONDS must be a whole number of seconds, got "
                            f"{window_raw!r}; using {DEFAULT_DEDUPE_WINDOW_SECONDS}")
            window = DEFAULT_DEDUPE_WINDOW_SECONDS

    return AlertConfig(smtp_host=host, smtp_port=port, smtp_user=user, smtp_password=password,
                       smtp_from=sender, smtp_to=to, smtp_ssl=ssl_on,
                       smtp_starttls=_flag(env.get("BALANCE_ALERT_SMTP_STARTTLS"), True),
                       webhook_url=webhook, dedupe_window_seconds=window, problems=tuple(problems))


def _mask_email(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def alert_config_status(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What the dashboard shows about alert destinations (no secrets: the webhook shows its host
    only, recipients are masked)."""
    cfg = load_alert_config(env)
    warnings = list(cfg.problems)
    if not cfg.channels:
        warnings.insert(0, "No alert destination is configured: low-balance alerts are only recorded on this "
                           "dashboard. Set BALANCE_ALERT_SMTP_* and/or BALANCE_ALERT_WEBHOOK_URL (see .env.example).")
    return {
        "configured": bool(cfg.channels),
        "channels": cfg.channels,
        "email": {"configured": cfg.email_ready, "host": cfg.smtp_host, "port": cfg.smtp_port,
                  "to": [_mask_email(a) for a in cfg.smtp_to], "tls": "ssl" if cfg.smtp_ssl else
                  ("starttls" if cfg.smtp_starttls else "none")},
        "webhook": {"configured": cfg.webhook_ready,
                    "host": urlsplit(cfg.webhook_url).netloc if cfg.webhook_url else None},
        "dedupe_window_seconds": cfg.dedupe_window_seconds,
        "warnings": warnings,
    }


@dataclass
class Alert:
    """An alert to send."""
    alert_type: str
    severity: str  # info, warning, error
    subject: str
    message: str
    details: dict[str, Any]
    
    def dedupe_key(self) -> str:
        """Key for deduplication (same key within window is suppressed).
        
        Task 13: Include episode and shot in dedupe key so different episodes' 403s aren't suppressed.
        """
        # Include episode_id and job_id from details if present (for 403s)
        episode = self.details.get("episode_id", "")
        job = self.details.get("job_id", "")
        key_data = f"{self.alert_type}:{self.subject}:{episode}:{job}"
        return hashlib.sha256(key_data.encode()).hexdigest()[:16]


class BalanceNotifier:
    """Alert notification system."""
    
    def __init__(self, db_path: str, env: Mapping[str, str] | None = None):
        self.db_path = db_path
        self.env = env
        self._dedupe_windows: dict[str, int] = {}
    
    async def send_low_balance_alert(self, provider: str, balance_usd: float, threshold_usd: float) -> dict:
        """Send low balance alert."""
        alert = Alert(
            alert_type="low_balance",
            severity="warning",
            subject=f"{provider.capitalize()} balance low",
            message=f"{provider.capitalize()} balance is ${balance_usd:.2f}, below threshold ${threshold_usd:.2f}",
            details={"provider": provider, "balance_usd": balance_usd, "threshold_usd": threshold_usd},
        )
        return await self._send_alert(alert)
    
    async def send_critical_balance_alert(self, provider: str, balance_usd: float, threshold_usd: float) -> dict:
        """Send critical balance alert."""
        alert = Alert(
            alert_type="critical_balance",
            severity="error",
            subject=f"{provider.capitalize()} balance CRITICAL",
            message=f"{provider.capitalize()} balance is ${balance_usd:.2f}, CRITICALLY LOW (threshold ${threshold_usd:.2f})",
            details={"provider": provider, "balance_usd": balance_usd, "threshold_usd": threshold_usd},
        )
        return await self._send_alert(alert)
    
    async def send_stale_balance_alert(self, provider: str, age_hours: float, max_age_hours: int) -> dict:
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
        return await self._send_alert(alert)

    async def send_unchanged_balance_alert(self, provider: str, balance_usd: str, spent_usd: str,
                                           previous_set_at: str, set_at: str) -> dict:
        """The balance entered did not move although the studio spent money since the previous entry:
        the number is probably read from another wallet (e.g. web app credits instead of the API
        balance) or a cached page."""
        alert = Alert(
            alert_type="balance_unchanged",
            severity="warning",
            subject=f"{provider.capitalize()} balance entry did not change after spend",
            message=(
                f"The {provider} balance entered at {set_at} (${balance_usd}) equals the entry of "
                f"{previous_set_at}, but the studio committed ${spent_usd} of {provider} spend in between. "
                "Check that the number is the API account's USD balance, not a web app credit wallet or a "
                "cached page."
            ),
            details={"provider": provider, "balance_usd": balance_usd, "spent_between_usd": spent_usd,
                     "previous_set_at": previous_set_at, "set_at": set_at},
        )
        return await self._send_alert(alert)
    
    async def send_403_stop_alert(
        self,
        provider: str,
        episode_id: str,
        job_id: str,
        error_message: str,
    ) -> dict:
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
        return await self._send_alert(alert)

    async def send_test_alert(self, requested_by: str | None) -> dict:
        """An admin's test of the configured destinations. Recorded like any alert (is_test=1);
        repeats within TEST_ALERT_DEDUPE_WINDOW_SECONDS are suppressed (double clicks)."""
        alert = Alert(
            alert_type="test",
            severity="info",
            subject="DuckDuckGoose test alert",
            message=("This is a test of the studio's low-balance alert destinations. "
                     f"Requested by {requested_by or 'an admin'} from the master dashboard."),
            details={"requested_by": requested_by or "admin"},
        )
        return await self._send_alert(alert, dedupe_window_seconds=TEST_ALERT_DEDUPE_WINDOW_SECONDS, is_test=True)
    
    async def _send_alert(self, alert: Alert, *, dedupe_window_seconds: int | None = None,
                          is_test: bool = False) -> dict:
        """Send an alert through all configured channels (with deduplication) and record the outcome.

        Returns {"suppressed": bool, "delivered": bool, "channels": [...], "results": {channel: ok/error}}.
        """
        await init_notifier_tables(self.db_path)
        config = load_alert_config(self.env)
        window = config.dedupe_window_seconds if dedupe_window_seconds is None else dedupe_window_seconds
        # Check deduplication
        dedupe_key = hashlib.sha256(f"{alert.dedupe_key()}|{config.fingerprint()}".encode()).hexdigest()[:16]
        self._dedupe_windows[dedupe_key] = window
        if await self._is_recent_duplicate(dedupe_key):
            return {"suppressed": True, "delivered": False, "channels": config.channels, "results": {}}
        
        # Always log
        self._log_alert(alert)

        senders = {"email": self._send_email, "webhook": self._send_webhook}
        channels = config.channels
        outcomes = await asyncio.gather(*(senders[c](alert, config) for c in channels), return_exceptions=True)
        results: dict[str, str] = {}
        for channel, outcome in zip(channels, outcomes):
            if isinstance(outcome, BaseException):
                error = self._redact_webhook_error(outcome) if channel == "webhook" else self._redact_email_error(outcome, config)
                self._log_alert_error(f"{channel.capitalize()} send failed: {error}")
                results[channel] = f"failed: {error}"
            else:
                results[channel] = "sent"
        if not channels:
            results["none"] = "not sent: no alert destination configured"
        delivered = any(v == "sent" for v in results.values())
        await self._record_alert(alert, dedupe_key, channels=channels, results=results,
                                 delivered=delivered, is_test=is_test)
        return {"suppressed": False, "delivered": delivered, "channels": channels, "results": results}
    
    async def _is_recent_duplicate(self, dedupe_key: str) -> bool:
        """Check if this alert was recently recorded (within the deduplication window). A delivery
        that failed on a configured channel does not count, so the next check retries it."""
        window = self._dedupe_windows.get(dedupe_key)
        if window is None:
            window = load_alert_config(self.env).dedupe_window_seconds
        cutoff = datetime.now(timezone.utc).timestamp() - window
        
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_alert_columns(db)
            row = await (await db.execute(
                "SELECT COUNT(*) FROM balance_alerts WHERE dedupe_key = ? AND sent_at_unix > ? "
                "AND (delivered IS NULL OR delivered = 1 OR COALESCE(channels, '') = '')",
                (dedupe_key, cutoff),
            )).fetchone()
        
        return row[0] > 0 if row else False
    
    async def _record_alert(self, alert: Alert, dedupe_key: str, *, channels: list[str] | None = None,
                            results: dict[str, str] | None = None, delivered: bool | None = None,
                            is_test: bool = False) -> None:
        """Record the alert and how each channel did."""
        async with aiosqlite.connect(self.db_path) as db:
            await ensure_alert_columns(db)
            await db.execute(
                "INSERT INTO balance_alerts (alert_type, severity, subject, message, details, dedupe_key, "
                "sent_at_unix, channels, delivery, delivered, is_test) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    alert.alert_type,
                    alert.severity,
                    alert.subject,
                    alert.message,
                    json.dumps(alert.details),
                    dedupe_key,
                    datetime.now(timezone.utc).timestamp(),
                    ",".join(channels or []),
                    json.dumps(results or {}),
                    None if delivered is None else int(delivered),
                    int(is_test),
                ),
            )
            await db.commit()
    
    async def _send_email(self, alert: Alert, config: AlertConfig) -> None:
        """Send alert via email (raises on failure)."""
        msg = MIMEMultipart()
        msg["From"] = config.smtp_from
        msg["To"] = ", ".join(config.smtp_to)
        msg["Subject"] = f"[{alert.severity.upper()}] {alert.subject}"
        
        body = f"{alert.message}\n\n"
        if alert.details:
            body += "Details:\n"
            for key, value in alert.details.items():
                body += f"  {key}: {value}\n"
        
        msg.attach(MIMEText(body, "plain"))
        
        # Send in a thread pool to avoid blocking
        await asyncio.to_thread(self._send_email_sync, msg, config)
    
    def _send_email_sync(self, msg: MIMEMultipart, config: AlertConfig) -> None:
        """Send email synchronously."""
        if config.smtp_ssl:
            server = smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=20,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20)
        with server:
            if not config.smtp_ssl and config.smtp_starttls:
                server.starttls(context=ssl.create_default_context())
            if config.smtp_user and config.smtp_password:
                server.login(config.smtp_user, config.smtp_password)
            server.send_message(msg)

    def _redact_email_error(self, error: BaseException, config: AlertConfig) -> str:
        """SMTP errors without the password (servers sometimes echo credentials)."""
        text = f"{type(error).__name__}: {error}"
        if config.smtp_password:
            text = text.replace(config.smtp_password, "[REDACTED]")
        return text[:300]
    
    def _redact_webhook_error(self, error: Exception) -> str:
        """Task 13: Redact webhook URL and exception details from error logs."""
        error_str = str(error)
        error_type = type(error).__name__
        
        # Redact any URL-like patterns (http://, https://)
        redacted = re.sub(r'https?://[^\s]+', '[URL_REDACTED]', error_str)
        
        # If no URL was found and redacted, redact the entire message for safety
        if redacted == error_str and error_str:
            # Non-URL error: redact the message to avoid leaking sensitive details
            return f"{error_type}: [details redacted]"
        
        return f"{error_type}: {redacted}"
    
    async def _send_webhook(self, alert: Alert, config: AlertConfig) -> None:
        """Send alert via webhook (raises on failure). The JSON carries a Slack-compatible ``text``
        plus the structured alert, so a generic receiver and a Slack incoming webhook both work."""
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
            "alert": {"type": alert.alert_type, "severity": alert.severity, "subject": alert.subject,
                      "message": alert.message, "details": alert.details,
                      "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(config.webhook_url, json=payload)
            resp.raise_for_status()
    
    def _severity_color(self, severity: str) -> str:
        """Map severity to Slack attachment color."""
        return {"info": "good", "warning": "warning", "error": "danger"}.get(severity, "good")
    
    def _log_alert(self, alert: Alert) -> None:
        """Log alert (always done regardless of other channels)."""
        level = {"info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}.get(
            alert.severity, logging.INFO
        )
        logger.log(level, f"[{alert.alert_type}] {alert.subject}: {alert.message}")
    
    def _log_alert_error(self, message: str) -> None:
        """Log alert delivery error."""
        logger.error(message)


ALERT_DELIVERY_COLUMNS = (
    ("channels", "TEXT"),
    ("delivery", "TEXT"),
    ("delivered", "INTEGER"),
    ("is_test", "INTEGER NOT NULL DEFAULT 0"),
)


async def ensure_alert_columns(db: aiosqlite.Connection) -> None:
    """Delivery record columns on balance_alerts (additive; no-op before the table exists)."""
    async with db.execute("PRAGMA table_info(balance_alerts)") as cur:
        cols = {r[1] async for r in cur}
    if not cols:
        return
    for col, decl in ALERT_DELIVERY_COLUMNS:
        if col not in cols:
            await db.execute(f"ALTER TABLE balance_alerts ADD COLUMN {col} {decl}")


async def recent_alerts(db_path: str, limit: int = 20) -> list[dict[str, Any]]:
    """The latest recorded alerts with their delivery outcome (newest first)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='balance_alerts'") as cur:
            if not await cur.fetchone():
                return []
        await ensure_alert_columns(db)
        await db.commit()
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, alert_type, severity, subject, message, sent_at_unix, channels, delivery, delivered, "
            "is_test FROM balance_alerts ORDER BY sent_at_unix DESC, id DESC LIMIT ?", (limit,)
        ) as cur:
            rows = [dict(r) async for r in cur]
    for row in rows:
        row["sent_at"] = datetime.fromtimestamp(row.pop("sent_at_unix"), tz=timezone.utc).isoformat(timespec="seconds")
        try:
            row["delivery"] = json.loads(row["delivery"]) if row["delivery"] else {}
        except ValueError:
            row["delivery"] = {}
        row["channels"] = [c for c in (row["channels"] or "").split(",") if c]
        row["delivered"] = None if row["delivered"] is None else bool(row["delivered"])
        row["is_test"] = bool(row["is_test"])
    return rows


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
        await ensure_alert_columns(db)
        
        await db.commit()
