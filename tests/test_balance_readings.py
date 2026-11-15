"""Balance readings (docs/BALANCE-ALERTS.md).

Every check stores its reading with a timestamp and what it was for (the preflight before a paid
step, the scheduled check, an admin's "check now"); the Higgsfield figure comes down with the
provider's usage report instead of sitting on the manual entry; an entry that did not move although
the studio spent is flagged; the scheduled check runs on its own Temporal schedule. Provider
responses are respx fakes; nothing is called for real."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiosqlite
import httpx
import pytest
import respx
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import balance_monitor as bm
from hfvg.budget import BudgetLedger

EL_SUBSCRIPTION = "https://api.elevenlabs.io/v1/user/subscription"
HF_USAGE = "https://api.higgsfield.ai/models/usage"
WEBHOOK = "https://hooks.example.test/balance/not-a-real-token"
SUBSCRIPTION = {"character_limit": 100_000, "character_count": 40_000, "status": "active"}
TASK_QUEUE = "balance-check-test"


def _db() -> str:
    return os.environ["DATABASE_PATH"]


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key-not-real")
    for key in ("HIGGSFIELD_API_KEY", "HIGGSFIELD_BASE_URL", "ELEVENLABS_API_BASE", "BALANCE_ALERT_WEBHOOK_URL",
                "BALANCE_ALERT_SMTP_HOST", "BALANCE_ALERT_SMTP_TO", bm.USAGE_CACHE_SECONDS_ENV,
                bm.CHECK_INTERVAL_ENV, bm.CHECK_SCHEDULE_ENABLED_ENV):
        monkeypatch.delenv(key, raising=False)


async def _ledger() -> BudgetLedger:
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    await bm.init_balance_tables(_db())
    return ledger


async def _spend(ledger: BudgetLedger, job_id: str, usd_micros: int, line: str = "L4_video"):
    await ledger.set_line("ep01", line, 500_000_000, 400_000_000,
                          "elevenlabs" if line.startswith("el_") else "higgsfield")
    assert await ledger.reserve("ep01", line, usd_micros, "test", job_id=job_id)
    await ledger.commit("ep01", line, usd_micros, usd_micros, job_id, "test")


async def _readings(provider: str) -> list[dict]:
    return (await bm.latest_balance_readings(_db(), per_provider=20))[provider]


def _usage_page(cost: str) -> dict:
    return {"time_series": [{"bucket": "2026-11-02T10:00:00Z", "results": [
        {"model": "kling-video/v2.1/pro/image-to-video", "unit": "generation", "quantity": 4, "unit_price": "0.65",
         "cost": cost, "currency": "USD"}]}], "total": 1, "page": 1, "limit": 100}


# ------------------------------------------------------------------ stored readings

async def test_each_check_stores_its_reading_with_a_timestamp(live):
    ledger = await _ledger()
    await ledger.set_manual_balance(50_000_000, "usr_test")
    with respx.mock() as mock:
        mock.get(EL_SUBSCRIPTION).mock(return_value=httpx.Response(200, json=SUBSCRIPTION))
        result = await bm.BalanceMonitor(_db()).check_all_balances(read_for="schedule")
    assert result.errors == []
    (el,) = await _readings("elevenlabs")
    (hf,) = await _readings("higgsfield")
    assert (el["read_for"], el["source"], el["balance_usd"]) == ("schedule", "live", "12.00")
    assert el["details"]["remaining_characters"] == 60_000
    assert (hf["read_for"], hf["source"], hf["balance_usd"]) == ("schedule", "derived", "50.00")
    assert hf["details"]["manual_entry_set_at"] and "usage_api_spend" not in hf["details"]
    read_at = datetime.fromisoformat(hf["read_at"])
    assert read_at.tzinfo is not None and abs((datetime.now(timezone.utc) - read_at).total_seconds()) < 60


async def test_failed_checks_are_stored_as_errors_not_skipped(live, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    await _ledger()
    await bm.BalanceMonitor(_db()).check_all_balances(read_for="on_demand")
    (el,) = await _readings("elevenlabs")
    (hf,) = await _readings("higgsfield")
    assert (el["balance_usd"], el["error"]) == (None, "ELEVENLABS_API_KEY not set")
    assert hf["source"] == "error" and hf["balance_usd"] is None
    assert hf["error"].startswith("Higgsfield balance check failed: No manual Higgsfield balance entry")


async def test_dry_run_checks_store_nothing(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    await _ledger()
    await bm.BalanceMonitor(_db()).check_all_balances(read_for="schedule")
    assert await _readings("higgsfield") == [] and await _readings("elevenlabs") == []


@pytest.mark.parametrize("manual_usd_micros,expected", [
    (50_000_000, (True, "higgsfield balance OK: $50.00 >= $6.00")),
    (5_000_000, (False, "higgsfield balance insufficient: $5.00 < required $6.00 (estimate $5.00 + 20% reserve)")),
])
async def test_the_preflight_reading_is_stored_and_the_gate_decides_as_before(live, manual_usd_micros, expected):
    ledger = await _ledger()
    await ledger.set_manual_balance(manual_usd_micros, "usr_test")
    decision = await bm.BalanceMonitor(_db()).check_preflight_balance("higgsfield", 5_000_000)
    assert decision == expected
    (hf,) = await _readings("higgsfield")
    assert (hf["read_for"], hf["balance_usd_micros"]) == ("preflight", manual_usd_micros)


async def test_a_preflight_without_a_balance_is_stored_and_does_not_block(live):
    await _ledger()
    passed, reason = await bm.BalanceMonitor(_db()).check_preflight_balance("higgsfield", 5_000_000)
    assert passed is True and "gate is no-op" in reason
    (hf,) = await _readings("higgsfield")
    assert (hf["read_for"], hf["source"], hf["error"]) == ("preflight", "error", "balance check failed or not set up")


async def test_storing_a_reading_never_raises(tmp_path):
    balance = bm.ProviderBalance(provider="higgsfield", balance_usd=Decimal("1"), source="derived",
                                 age_seconds=0.0, details={})
    await bm.record_balance_reading(str(tmp_path), "higgsfield", balance, "preflight")


# ------------------------------------------------------------------ the stale Higgsfield figure

async def test_the_usage_report_brings_the_figure_down_after_an_episode(live, monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "key-studio:secret-not-real")
    ledger = await _ledger()
    entry = await ledger.set_manual_balance(100_000_000, "usr_test")
    with respx.mock() as mock:
        route = mock.get(HF_USAGE).mock(return_value=httpx.Response(200, json=_usage_page("2.60")))
        balance = await bm.BalanceMonitor(_db())._check_higgsfield_balance()
    assert balance.balance_usd == Decimal("97.40"), "the provider-reported spend since the entry is subtracted"
    assert balance.details["usage_api_spend"] == "2.60" and balance.details["ledger_spend"] == "0"
    params = route.calls[0].request.url.params
    assert params["timeframe"] == "hour" and params["bound_to_timeframe"] == "false"
    assert params["start"] == datetime.fromisoformat(entry["set_at"]).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert params.get_list("expand") == ["time_series"]


async def test_the_usage_figure_is_reused_for_a_few_minutes_per_entry(live, monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "key-studio:secret-not-real")
    ledger = await _ledger()
    await ledger.set_manual_balance(100_000_000, "usr_test")
    monitor = bm.BalanceMonitor(_db())
    with respx.mock() as mock:
        mock.get(EL_SUBSCRIPTION).mock(return_value=httpx.Response(200, json=SUBSCRIPTION))
        route = mock.get(HF_USAGE).mock(return_value=httpx.Response(200, json=_usage_page("2.60")))
        await monitor.check_all_balances(read_for="preflight")
        await monitor.check_all_balances(read_for="preflight")
        assert route.call_count == 1, "a reading for the same entry within the cache window is reused"
        await ledger.set_manual_balance(90_000_000, "usr_test")
        await monitor.check_all_balances(read_for="preflight")
        assert route.call_count == 2, "a new manual entry needs a new usage figure"
        monkeypatch.setenv(bm.USAGE_CACHE_SECONDS_ENV, "0")
        await monitor.check_all_balances(read_for="preflight")
        assert route.call_count == 3
    readings = await _readings("higgsfield")
    assert [r["balance_usd"] for r in readings] == ["87.40", "87.40", "97.40", "97.40"]


async def test_a_failing_usage_report_falls_back_to_the_ledger_and_says_so(live, monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "key-studio:secret-not-real")
    ledger = await _ledger()
    await ledger.set_manual_balance(100_000_000, "usr_test")
    await _spend(ledger, "clip-1", 3_000_000)
    with respx.mock() as mock:
        mock.get(HF_USAGE).mock(return_value=httpx.Response(500, text="Key key-studio:secret-not-real upstream"))
        balance = await bm.BalanceMonitor(_db())._check_higgsfield_balance()
    assert balance.balance_usd == Decimal("97")
    assert "HTTP 500" in balance.details["usage_api_error"]
    assert "secret-not-real" not in balance.details["usage_api_error"]


async def _entries(ledger, first, spend, second, spend_line="L4_video"):
    await ledger.set_manual_balance(first, "usr_test")
    if spend:
        await _spend(ledger, "clip-between", spend, spend_line)
    await ledger.set_manual_balance(second, "usr_test")


@pytest.mark.parametrize("first,spend,second,line,flagged", [
    (3_963_000_000, 16_650_000, 3_963_000_000, "L4_video", True),
    (3_963_000_000, 16_650_000, 3_946_350_000, "L4_video", False),
    (3_963_000_000, 0, 3_963_000_000, "L4_video", False),
    (3_963_000_000, 999_999, 3_963_000_000, "L4_video", False),
    (3_963_000_000, 16_650_000, 3_963_000_000, "el_sfx", False),
])
async def test_an_entry_that_did_not_move_despite_spend_is_flagged(live, first, spend, second, line, flagged):
    ledger = await _ledger()
    await _entries(ledger, first, spend, second, line)
    out = await bm.manual_balance_consistency(_db())
    assert out["unchanged_despite_spend"] is flagged
    assert out["spent_between_usd_micros"] == (spend if line == "L4_video" else 0)


async def test_consistency_needs_two_entries(live):
    ledger = await _ledger()
    assert await bm.manual_balance_consistency(_db()) == {"entries": 0, "unchanged_despite_spend": False}
    await ledger.set_manual_balance(1_000_000, "usr_test")
    assert (await bm.manual_balance_consistency(_db()))["entries"] == 1


# ------------------------------------------------------------------ schedule status

async def test_overview_says_when_the_scheduled_check_is_overdue(live, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY")
    await _ledger()
    now = datetime.now(timezone.utc)
    schedule = (await bm.balance_overview(_db(), now=now))["schedule"]
    assert schedule == {"enabled": True, "interval_minutes": 360, "problem": None, "last_run_at": None,
                        "overdue": True, "dry_run": False}
    await bm.BalanceMonitor(_db()).check_all_balances(read_for="schedule")
    assert (await bm.balance_overview(_db(), now=now))["schedule"]["overdue"] is False
    later = now + timedelta(minutes=2 * 360 + 5)
    assert (await bm.balance_overview(_db(), now=later))["schedule"]["overdue"] is True
    monkeypatch.setenv(bm.CHECK_SCHEDULE_ENABLED_ENV, "false")
    assert (await bm.balance_overview(_db(), now=later))["schedule"]["overdue"] is False
    monkeypatch.setenv(bm.CHECK_SCHEDULE_ENABLED_ENV, "true")
    monkeypatch.setenv(bm.CHECK_INTERVAL_ENV, "often")
    schedule = (await bm.balance_overview(_db(), now=later))["schedule"]
    assert schedule["interval_minutes"] is None and "whole number of minutes" in schedule["problem"]


@pytest.mark.parametrize("raw", ["0", "-5", "six"])
def test_bad_check_intervals_are_refused(monkeypatch, raw):
    monkeypatch.setenv(bm.CHECK_INTERVAL_ENV, raw)
    with pytest.raises(ValueError):
        bm.balance_check_interval_minutes()


# ------------------------------------------------------------------ the scheduled activity

async def test_scheduled_check_reads_syncs_and_alerts_on_an_unchanged_entry(live, monkeypatch):
    from temporalio.testing import ActivityEnvironment
    from hfvg.activities.balance_check import scheduled_balance_check

    monkeypatch.setenv("HIGGSFIELD_API_KEY", "key-studio:secret-not-real")
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    ledger = await _ledger()
    await _entries(ledger, 3_963_000_000, 16_650_000, 3_963_000_000)
    with respx.mock() as mock:
        mock.get(EL_SUBSCRIPTION).mock(return_value=httpx.Response(200, json=SUBSCRIPTION))
        mock.get(HF_USAGE).mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
        hook = mock.post(WEBHOOK).mock(return_value=httpx.Response(200))
        out = await ActivityEnvironment().run(scheduled_balance_check)
    assert out["balances"]["elevenlabs"] == {"balance_usd": "12.00", "source": "live"}
    assert out["balances"]["higgsfield"]["source"] == "derived"
    assert out["usage_sync"]["ok"] is True and out["errors"] == []
    assert out["unchanged_balance"] is True and out["unchanged_alert"]["delivered"] is True
    assert out["alert_channels"] == ["webhook"]
    alert = json.loads(hook.calls[0].request.content)["alert"]
    assert alert["type"] == "balance_unchanged" and alert["details"]["spent_between_usd"] == "16.65"
    assert [r["read_for"] for r in await _readings("higgsfield")] == ["schedule"]
    async with aiosqlite.connect(_db()) as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM cost_sync_runs")).fetchone())[0] == 1


async def test_scheduled_check_without_alert_destination_warns(live):
    from temporalio.testing import ActivityEnvironment
    from hfvg.activities.balance_check import scheduled_balance_check

    await _ledger()
    with respx.mock() as mock:
        mock.get(EL_SUBSCRIPTION).mock(return_value=httpx.Response(200, json=SUBSCRIPTION))
        out = await ActivityEnvironment().run(scheduled_balance_check)
    assert out["alert_channels"] == []
    assert out["alert_warnings"][0].startswith("No alert destination is configured")


def test_worker_registers_the_balance_check():
    from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS
    from hfvg.workflows.balance_check import BalanceCheckWorkflow

    names = {getattr(a, "__temporal_activity_definition").name for a in PRODUCTION_ACTIVITIES}
    assert BalanceCheckWorkflow in PRODUCTION_WORKFLOWS and "scheduled_balance_check" in names


async def _describe(client: Client, schedule_id: str):
    from temporalio.service import RPCError
    try:
        return await client.get_schedule_handle(schedule_id).describe()
    except RPCError:
        return None


@pytest.mark.timeout(120)
async def test_worker_creates_the_balance_check_schedule_idempotently(live, monkeypatch):
    from hfvg.worker import setup_balance_check_schedule
    from hfvg.workflows.balance_check import BALANCE_CHECK_SCHEDULE_ID, BALANCE_CHECK_WORKFLOW_ID

    async with await WorkflowEnvironment.start_local() as env:
        monkeypatch.setenv(bm.CHECK_SCHEDULE_ENABLED_ENV, "false")
        assert await setup_balance_check_schedule(env.client, TASK_QUEUE) is False
        assert await _describe(env.client, BALANCE_CHECK_SCHEDULE_ID) is None
        monkeypatch.setenv(bm.CHECK_SCHEDULE_ENABLED_ENV, "true")
        assert await setup_balance_check_schedule(env.client, TASK_QUEUE) is True
        desc = await _describe(env.client, BALANCE_CHECK_SCHEDULE_ID)
        action = desc.schedule.action
        assert action.workflow == "BalanceCheckWorkflow" and action.id == BALANCE_CHECK_WORKFLOW_ID
        assert action.task_queue == TASK_QUEUE
        assert desc.schedule.spec.intervals[0].every == timedelta(minutes=360)
        monkeypatch.setenv(bm.CHECK_INTERVAL_ENV, "30")
        assert await setup_balance_check_schedule(env.client, TASK_QUEUE) is True
        desc = await _describe(env.client, BALANCE_CHECK_SCHEDULE_ID)
        assert desc.schedule.spec.intervals[0].every == timedelta(minutes=30)
        monkeypatch.setenv(bm.CHECK_INTERVAL_ENV, "never")
        assert await setup_balance_check_schedule(env.client, TASK_QUEUE) is False


@pytest.mark.timeout(120)
async def test_the_workflow_runs_the_check(monkeypatch):
    from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS
    from hfvg.workflows.balance_check import BalanceCheckWorkflow

    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("BALANCE_ALERT_WEBHOOK_URL", raising=False)
    await _ledger()
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=PRODUCTION_WORKFLOWS,
                          activities=PRODUCTION_ACTIVITIES):
            out = await env.client.execute_workflow(BalanceCheckWorkflow.run, id="balance-check-test",
                                                    task_queue=TASK_QUEUE)
    assert out["balances"]["higgsfield"] == {"balance_usd": "100.00", "source": "manual"}
    assert out["usage_sync"]["skipped"].startswith("DRY_RUN")
