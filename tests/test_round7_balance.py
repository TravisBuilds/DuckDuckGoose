"""Round 7 B8: live work refuses without a recent manually recorded Higgsfield balance
(HFVG_BALANCE_MAX_AGE_HOURS, default 24); the balance can be recorded from the CLI or the
authenticated API and is shown in the credits panel. Synthetic data, no provider calls."""

import json
import sys
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import temporalio.activity
from click.testing import CliRunner

from hfvg import budget
from hfvg.budget import BudgetLedger, balance_freshness
from hfvg.credit_ledger import CreditLedger
from hfvg.studio_db import create_episode, init_studio_db

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=timezone.utc)


def _status(hours_ago: float | None):
    if hours_ago is None:
        return {"manual_balance_usd_micros": None, "set_at": None}
    return {"manual_balance_usd_micros": 50_000_000, "set_at": (NOW - timedelta(hours=hours_ago)).isoformat()}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv(budget.BALANCE_MAX_AGE_ENV, raising=False)


def test_freshness_rules(monkeypatch):
    assert balance_freshness(_status(None), NOW)["ok"] is False
    assert "no manual Higgsfield balance" in balance_freshness(_status(None), NOW)["reason"]
    assert balance_freshness(_status(23.5), NOW)["ok"] is True
    stale = balance_freshness(_status(25), NOW)
    assert stale["ok"] is False and "older than 24 h" in stale["reason"] and stale["age_hours"] == 25.0
    monkeypatch.setenv(budget.BALANCE_MAX_AGE_ENV, "48")
    assert balance_freshness(_status(25), NOW)["ok"] is True
    assert balance_freshness(_status(49), NOW)["ok"] is False
    naive = {"manual_balance_usd_micros": 1, "set_at": (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()}
    assert balance_freshness(naive, NOW)["ok"] is True, "naive timestamps are UTC (the ledger writes UTC)"


@pytest.mark.parametrize("raw", ["abc", "0", "-3", "nan", "inf"])
def test_invalid_max_age_fails_loudly(monkeypatch, raw):
    monkeypatch.setenv(budget.BALANCE_MAX_AGE_ENV, raw)
    with pytest.raises(ValueError, match=budget.BALANCE_MAX_AGE_ENV):
        budget.balance_max_age_hours()


async def _backdate(db_path: str, hours: float):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE manual_balance SET set_at = ?", (ts,))
        await db.commit()


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "b8.db")
    await init_studio_db(path)
    await BudgetLedger(path).init_db()
    await CreditLedger(path).init_db()
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("DRY_RUN", "true")
    return path


async def _refusal(coro) -> str | None:
    """The ValueError message of a refused reserve (None = reserved): a surviving mutation fails
    with a plain AssertionError (redgreen), not 'DID NOT RAISE'."""
    try:
        await coro
    except ValueError as e:
        return str(e)
    return None


@pytest.mark.asyncio
async def test_live_reserve_refuses_a_stale_balance_dry_does_not(db_path):
    ledger = BudgetLedger(db_path)
    await ledger.set_line("ep99", "L2_drafts", 10_000_000, 8_000_000)
    refused = await _refusal(ledger.reserve("ep99", "L2_drafts", 60_000, "live still", require_balance=True))
    assert refused and "no manual Higgsfield balance" in refused, refused
    await ledger.set_manual_balance(50_000_000, set_by="test")
    assert await ledger.reserve("ep99", "L2_drafts", 60_000, "live still", require_balance=True)
    await _backdate(db_path, 30)
    refused = await _refusal(ledger.reserve("ep99", "L2_drafts", 60_000, "live still", require_balance=True))
    assert refused and "older than 24 h" in refused, refused
    assert await ledger.reserve("ep99", "L2_drafts", 60_000, "dry still", require_balance=False)


def _episode(tmp_path):
    base = tmp_path / "data" / "episodes" / "ep99"
    base.mkdir(parents=True, exist_ok=True)
    (base / "BEATMAP.md").write_text("A01 (2.5s)")
    (base / "shots.json").write_text(json.dumps({"A01": {"prompt": "Test shot",
                                                         "params": {"model": "kling", "duration": 3}}}))
    (base / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    (base / "prompt_kit.json").write_text("{}")


async def _preflight_problems(is_live: bool):
    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    try:
        await preflight_validate_episode_activity("ep99", None, is_live)
    except PreflightError as e:
        return e.problems
    return []


@pytest.mark.asyncio
async def test_live_preflight_refuses_missing_or_stale_balance(db_path, tmp_path, monkeypatch):
    _episode(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "synthetic-not-a-key")
    monkeypatch.setattr(temporalio.activity, "info", lambda: type("o", (), {"workflow_id": "wf"})())
    gc = lambda probs: [p for p in probs if p.startswith("GC.01")]  # noqa: E731

    assert any("no manual Higgsfield balance" in p for p in gc(await _preflight_problems(True)))
    assert gc(await _preflight_problems(False)) == [], "dry preflight does not need a balance"
    await BudgetLedger(db_path).set_manual_balance(50_000_000, set_by="test")
    assert gc(await _preflight_problems(True)) == []
    await _backdate(db_path, 25)
    assert any("older than 24 h" in p for p in gc(await _preflight_problems(True)))
    monkeypatch.setenv(budget.BALANCE_MAX_AGE_ENV, "36")
    assert gc(await _preflight_problems(True)) == []
    monkeypatch.setenv(budget.BALANCE_MAX_AGE_ENV, "soon")
    assert any(budget.BALANCE_MAX_AGE_ENV in p for p in await _preflight_problems(True))


def test_cli_set_and_show_balance(db_path):
    from hfvg.cli import cli
    runner = CliRunner()
    res = runner.invoke(cli, ["budget", "show-balance"])
    assert res.exit_code == 1 and json.loads(res.output)["fresh"] is False
    res = runner.invoke(cli, ["budget", "set-balance", "42.50", "--note", "account page"])
    assert res.exit_code == 0, res.output
    report = json.loads(res.output)
    assert report["balance_usd"] == "42.50" and report["fresh"] is True and report["set_by"] == "cli"
    res = runner.invoke(cli, ["budget", "show-balance"])
    assert res.exit_code == 0 and json.loads(res.output)["balance_usd"] == "42.50"
    res = runner.invoke(cli, ["budget", "set-balance", "lots"])
    assert res.exit_code != 0


@pytest.fixture
def api(monkeypatch):
    sys.modules.pop("api.main", None)
    monkeypatch.setenv("ADMIN_SECRET", "s" * 40)
    from fastapi.testclient import TestClient
    from api.main import app
    yield TestClient(app)
    sys.modules.pop("api.main", None)


@pytest.mark.asyncio
async def test_api_records_balance_behind_auth_and_credits_panel_shows_it(db_path, api):
    await create_episode(db_path, "ep99")
    assert api.post("/api/budget/balance", json={"balance_usd": "12.00"}).status_code in (401, 403)
    assert api.get("/api/episodes/ep99/credits").status_code in (401, 403)
    cookies = api.post("/api/login", json={"admin_secret": "s" * 40}).cookies

    panel = api.get("/api/episodes/ep99/credits", cookies=cookies).json()["higgsfield_balance"]
    assert panel["balance_usd_micros"] is None and panel["fresh"] is False

    r = api.post("/api/budget/balance", json={"balance_usd": "12.00", "note": "account page"}, cookies=cookies)
    assert r.status_code == 200, r.text
    panel = api.get("/api/episodes/ep99/credits", cookies=cookies).json()["higgsfield_balance"]
    assert panel["balance_usd_micros"] == 12_000_000 and panel["fresh"] is True
    assert panel["set_by"] == "admin" and panel["max_age_hours"] == 24.0 and panel["set_at"]

    await _backdate(db_path, 30)
    panel = api.get("/api/episodes/ep99/credits", cookies=cookies).json()["higgsfield_balance"]
    assert panel["fresh"] is False and "older than 24 h" in panel["reason"]
