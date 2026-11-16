"""Low-balance alert delivery (docs/BALANCE-ALERTS.md).

Destinations come from the environment when the alert is sent; every alert is recorded with the
channels tried and each one's outcome; a failed delivery is retried at the next check instead of
being deduplicated away; a missing destination is visible (dashboard warning, "not sent" record),
never a silent success. SMTP is a fake class and the webhook a respx route: nothing is sent."""

import json
import os
import smtplib
from datetime import datetime, timezone

import aiosqlite
import httpx
import pytest
import respx

from hfvg import balance_notifier as bn

WEBHOOK = "https://hooks.example.test/services/T000/B000/not-a-real-token"
ALERT_ENV = ("BALANCE_ALERT_SMTP_HOST", "BALANCE_ALERT_SMTP_PORT", "BALANCE_ALERT_SMTP_USER",
             "BALANCE_ALERT_SMTP_PASSWORD", "BALANCE_ALERT_SMTP_FROM", "BALANCE_ALERT_SMTP_TO",
             "BALANCE_ALERT_SMTP_SSL", "BALANCE_ALERT_SMTP_STARTTLS", "BALANCE_ALERT_WEBHOOK_URL",
             "BALANCE_ALERT_DEDUPE_WINDOW_SECONDS")


@pytest.fixture(autouse=True)
def _no_alert_env(monkeypatch):
    for key in ALERT_ENV:
        monkeypatch.delenv(key, raising=False)


def _db() -> str:
    return os.environ["DATABASE_PATH"]


async def _alerts():
    async with aiosqlite.connect(_db()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM balance_alerts ORDER BY id") as cur:
            return [dict(r) for r in await cur.fetchall()]


def _email_env(monkeypatch, **extra):
    monkeypatch.setenv("BALANCE_ALERT_SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("BALANCE_ALERT_SMTP_USER", "alerts@example.test")
    monkeypatch.setenv("BALANCE_ALERT_SMTP_PASSWORD", "pw-not-real-123")
    monkeypatch.setenv("BALANCE_ALERT_SMTP_TO", "ops@example.test, finance@example.test")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL: records the session, sends nothing."""
    sessions: list["FakeSMTP"] = []
    fail_login = False

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.ssl_context = host, port, context
        self.tls = False
        self.login_args = None
        self.sent = []
        FakeSMTP.sessions.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.tls = True

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, f"5.7.8 rejected {user} {password}".encode())
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSMTP.sessions = []
    FakeSMTP.fail_login = False
    monkeypatch.setattr(bn.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(bn.smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


# ------------------------------------------------------------------ configuration

def test_destinations_are_read_when_asked_not_at_import(monkeypatch):
    assert bn.load_alert_config().channels == []
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    _email_env(monkeypatch)
    cfg = bn.load_alert_config()
    assert cfg.channels == ["email", "webhook"] and cfg.problems == ()
    assert (cfg.smtp_from, cfg.smtp_port, cfg.smtp_to) == ("alerts@example.test", 587,
                                                           ("ops@example.test", "finance@example.test"))


@pytest.mark.parametrize("env,problem", [
    ({"BALANCE_ALERT_SMTP_HOST": "smtp.example.test"}, "BALANCE_ALERT_SMTP_TO is empty"),
    ({"BALANCE_ALERT_SMTP_TO": "ops@example.test"}, "BALANCE_ALERT_SMTP_HOST is not"),
    ({"BALANCE_ALERT_SMTP_HOST": "h", "BALANCE_ALERT_SMTP_TO": "not-an-address", "BALANCE_ALERT_SMTP_FROM": "a@b.test"},
     "comma-separated email addresses"),
    ({"BALANCE_ALERT_SMTP_HOST": "h", "BALANCE_ALERT_SMTP_TO": "o@b.test", "BALANCE_ALERT_SMTP_FROM": "a@b.test",
      "BALANCE_ALERT_SMTP_PORT": "smtp"}, "BALANCE_ALERT_SMTP_PORT must be a port number"),
    ({"BALANCE_ALERT_SMTP_HOST": "h", "BALANCE_ALERT_SMTP_TO": "o@b.test", "BALANCE_ALERT_SMTP_USER": "u@b.test"},
     "go together"),
    ({"BALANCE_ALERT_WEBHOOK_URL": "hooks.example.test/x"}, "must be an http(s) URL"),
])
def test_incomplete_destinations_are_reported_and_not_used(env, problem):
    cfg = bn.load_alert_config(env)
    assert any(problem in p for p in cfg.problems), cfg.problems
    status = bn.alert_config_status(env)
    assert status["configured"] is False and status["channels"] == []
    assert status["warnings"][0].startswith("No alert destination is configured")
    assert any(problem in w for w in status["warnings"])


def test_status_shows_no_secrets(monkeypatch):
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    _email_env(monkeypatch)
    status = bn.alert_config_status()
    text = json.dumps(status)
    assert "not-a-real-token" not in text and "pw-not-real-123" not in text and "ops@example.test" not in text
    assert status["webhook"]["host"] == "hooks.example.test"
    assert status["email"]["to"] == ["o***@example.test", "f***@example.test"] and status["email"]["tls"] == "starttls"
    assert status["configured"] is True and status["warnings"] == []


# ------------------------------------------------------------------ delivery records

async def test_no_destination_is_recorded_as_not_sent_and_shown():
    notifier = bn.BalanceNotifier(_db())
    result = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert result == {"suppressed": False, "delivered": False, "channels": [],
                      "results": {"none": "not sent: no alert destination configured"}}
    (row,) = await _alerts()
    assert (row["channels"], row["delivered"], json.loads(row["delivery"])) == ("", 0, result["results"])
    again = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert again["suppressed"] is True and len(await _alerts()) == 1, "recorded once per window, not on every check"


async def test_webhook_delivery_is_recorded_with_the_structured_alert(monkeypatch):
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
        result = await bn.BalanceNotifier(_db()).send_critical_balance_alert("elevenlabs", 3.5, 5.0)
    assert result["delivered"] is True and result["results"] == {"webhook": "sent"}
    payload = json.loads(route.calls[0].request.content)
    assert payload["alert"]["type"] == "critical_balance" and payload["alert"]["details"]["balance_usd"] == 3.5
    assert "CRITICAL" in payload["text"]
    (row,) = await _alerts()
    assert (row["channels"], row["delivered"]) == ("webhook", 1)


async def test_a_failed_delivery_is_retried_at_the_next_check(monkeypatch):
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    notifier = bn.BalanceNotifier(_db())
    with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
        first = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        second = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        third = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert first["delivered"] is False and first["results"]["webhook"].startswith("failed: HTTPStatusError")
    assert "not-a-real-token" not in first["results"]["webhook"], "the webhook URL is a secret"
    assert second["delivered"] is True and third["suppressed"] is True
    assert route.call_count == 2
    assert [r["delivered"] for r in await _alerts()] == [0, 1]


async def test_a_new_destination_is_not_suppressed_by_unsent_alerts(monkeypatch):
    notifier = bn.BalanceNotifier(_db())
    await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    with respx.mock() as mock:
        mock.post(WEBHOOK).mock(return_value=httpx.Response(200))
        result = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert result["suppressed"] is False and result["delivered"] is True


async def test_email_uses_starttls_and_login(monkeypatch, fake_smtp):
    _email_env(monkeypatch)
    result = await bn.BalanceNotifier(_db()).send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert result["results"] == {"email": "sent"}
    (session,) = fake_smtp.sessions
    assert (session.host, session.port, session.tls) == ("smtp.example.test", 587, True)
    assert session.login_args == ("alerts@example.test", "pw-not-real-123")
    (msg,) = session.sent
    assert msg["To"] == "ops@example.test, finance@example.test" and msg["From"] == "alerts@example.test"
    assert msg["Subject"] == "[WARNING] Higgsfield balance low"


async def test_email_over_ssl(monkeypatch, fake_smtp):
    _email_env(monkeypatch, BALANCE_ALERT_SMTP_SSL="true")
    await bn.BalanceNotifier(_db()).send_low_balance_alert("higgsfield", 8.0, 10.0)
    (session,) = fake_smtp.sessions
    assert session.port == 465 and session.ssl_context is not None and session.tls is False


async def test_email_failure_is_recorded_without_the_password(monkeypatch, fake_smtp):
    _email_env(monkeypatch)
    fake_smtp.fail_login = True
    result = await bn.BalanceNotifier(_db()).send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert result["delivered"] is False and result["results"]["email"].startswith("failed: SMTPAuthenticationError")
    assert "pw-not-real-123" not in json.dumps(await _alerts(), default=str)


# ------------------------------------------------------------------ per-channel delivery

@pytest.mark.parametrize("failing", ["email", "webhook"])
async def test_only_the_channel_that_failed_is_retried(monkeypatch, fake_smtp, failing):
    _email_env(monkeypatch)
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    delivering = "webhook" if failing == "email" else "email"
    notifier = bn.BalanceNotifier(_db())
    replies = [httpx.Response(500), httpx.Response(500), httpx.Response(200)] if failing == "webhook" else \
        [httpx.Response(200)]
    with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(side_effect=replies)
        fake_smtp.fail_login = failing == "email"
        first = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        second = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        fake_smtp.fail_login = False
        third = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        fourth = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)

    assert [r["suppressed"] for r in (first, second, third)] == [False] * 3, "a failed channel is retried"
    assert first["delivered"] is True and first["results"][delivering] == "sent"
    assert first["results"][failing].startswith("failed: ")
    assert second["delivered"] is False and second["results"][failing].startswith("failed: ")
    assert second["results"][delivering].startswith("already sent at ")
    assert third["delivered"] is True and third["results"][failing] == "sent"
    assert fourth["suppressed"] is True, "every destination has it now"
    emails_sent = sum(len(session.sent) for session in fake_smtp.sessions)
    assert (emails_sent, route.call_count) == ((1, 1) if failing == "email" else (1, 3)), \
        "the channel that delivered is not sent the alert again"

    rows = await _alerts()
    assert [(r["channels"], r["delivered"]) for r in rows] == [("email,webhook", 1), ("email,webhook", 0),
                                                              ("email,webhook", 1)]
    first_sent_at = datetime.fromtimestamp(rows[0]["sent_at_unix"], tz=timezone.utc).isoformat(timespec="seconds")
    assert json.loads(rows[2]["delivery"]) == {failing: "sent", delivering: f"already sent at {first_sent_at}"}
    assert "pw-not-real-123" not in json.dumps(rows, default=str) and "not-a-real-token" not in json.dumps(rows)


async def test_a_channel_is_suppressed_for_one_window_after_it_delivered(monkeypatch, fake_smtp):
    """An "already sent" note on a retry does not stretch the window of the channel it names."""
    _email_env(monkeypatch)
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    window = bn.DEFAULT_DEDUPE_WINDOW_SECONDS
    notifier = bn.BalanceNotifier(_db())

    async def age(seconds):
        async with aiosqlite.connect(_db()) as db:
            await db.execute("UPDATE balance_alerts SET sent_at_unix = sent_at_unix - ?", (seconds,))
            await db.commit()

    fake_smtp.fail_login = True
    with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(return_value=httpx.Response(200))
        await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        await age(window - 60)
        retry = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
        await age(120)
        reminder = await notifier.send_low_balance_alert("higgsfield", 8.0, 10.0)
    assert (retry["suppressed"], reminder["suppressed"]) == (False, False), "the failed email is retried"
    assert retry["results"]["webhook"].startswith("already sent at ")
    assert reminder["results"]["webhook"] == "sent"
    assert route.call_count == 2, "sent at the first check, then again once that delivery left the window"


# ------------------------------------------------------------------ test alert, history

async def test_test_alert_is_marked_and_double_clicks_are_suppressed(monkeypatch):
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    notifier = bn.BalanceNotifier(_db())
    with respx.mock() as mock:
        route = mock.post(WEBHOOK).mock(return_value=httpx.Response(200))
        first = await notifier.send_test_alert("usr_test")
        second = await notifier.send_test_alert("usr_test")
        async with aiosqlite.connect(_db()) as db:
            past = datetime.now(timezone.utc).timestamp() - bn.TEST_ALERT_DEDUPE_WINDOW_SECONDS - 1
            await db.execute("UPDATE balance_alerts SET sent_at_unix = ?", (past,))
            await db.commit()
        third = await notifier.send_test_alert("usr_test")
    assert (first["delivered"], second["suppressed"], third["delivered"]) == (True, True, True)
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["alert"]["type"] == "test"
    recent = await bn.recent_alerts(_db())
    assert [(r["alert_type"], r["is_test"], r["delivered"], r["channels"]) for r in recent] == \
        [("test", True, True, ["webhook"])] * 2
    assert recent[0]["delivery"] == {"webhook": "sent"} and recent[0]["sent_at"].endswith("+00:00")


async def test_recent_alerts_on_a_fresh_database():
    assert await bn.recent_alerts(_db()) == []
