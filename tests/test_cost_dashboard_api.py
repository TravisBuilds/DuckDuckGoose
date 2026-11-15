"""Admin routes behind the cost dashboard (docs/ACTUAL-COSTS.md, docs/BALANCE-ALERTS.md): the
dashboard's cost truth, balances and alerts; assigning an episode's spend to a person; "check
balances now", "sync usage now" and the test alert; and recording who started each run; and the
page's display helpers. Provider calls are skipped (DRY_RUN) or respx fakes; people are made up."""

import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import httpx
import pytest
import respx

from hfvg import actual_costs as ac
from hfvg.budget import BudgetLedger
from hfvg.studio_db import create_episode, init_studio_db
from hfvg.users import UserManager

REPO = Path(__file__).resolve().parents[1]
WEBHOOK = "https://hooks.example.test/services/T000/B000/not-a-real-token"


def _db() -> str:
    return os.environ["DATABASE_PATH"]


@pytest.fixture
async def people():
    from hfvg.cost_tracking import CostTracker
    await init_studio_db(_db())
    await BudgetLedger(_db()).init_db()
    await CostTracker(_db()).init_db()
    users = UserManager(_db())
    await users.init_db()
    one = await users.create_user(name="Producer One", email="producer.one@example.test", role="member")
    two = await users.create_user(name="Editor Two", email="editor.two@example.test", role="member")
    return one["user_id"], two["user_id"]


@pytest.fixture
def api(people, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    for key in ("BALANCE_ALERT_WEBHOOK_URL", "BALANCE_ALERT_SMTP_HOST", "BALANCE_ALERT_SMTP_TO"):
        monkeypatch.delenv(key, raising=False)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    return api_main


def _admin(api_main):
    from fastapi.testclient import TestClient
    client = TestClient(api_main.app)
    client.headers["Authorization"] = f"Bearer {os.environ['ADMIN_SECRET']}"
    return client


async def _job(job_id, episode_id, line, usd, *, actual=None, source=None):
    await BudgetLedger(_db()).record_job(episode_id, job_id, "A01", "model-x", "draft", line, usd, user_id="admin")
    async with aiosqlite.connect(_db()) as db:
        await db.execute("UPDATE job_ledger SET status = 'committed', actual_usd_micros = ?, actual_source = ? "
                         "WHERE job_id = ?", (actual, source, job_id))
        await db.commit()


async def test_dashboard_carries_cost_truth_balances_and_alerts(api, people):
    await _job("clip-1", "ep01", "L4_video", 400_000, actual=420_000, source=ac.SOURCE_HF_USAGE)
    res = _admin(api).get("/api/dashboard")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cost_truth"]["totals"]["actual_usd_micros"] == 420_000
    assert body["cost_truth"]["rates"]["elevenlabs"]["usd_per_credit"] == "0.0002"
    assert set(body["balances"]) == {"readings", "consistency", "schedule"}
    assert body["alerts"]["status"]["configured"] is False and body["alerts"]["recent"] == []
    assert "alerts_unconfigured" in {w["code"] for w in body["cost_warnings"]}


async def test_the_headline_and_the_projection_agree_with_the_cost_truth_after_a_backfill(api, people):
    await _job("clip-1", "ep01", "L4_video", 400_000, actual=420_000, source=ac.SOURCE_HF_USAGE)
    await _job("still-1", "ep01", "L2_drafts", 100_000)
    await BudgetLedger(_db()).record_job("ep02", "clip-2", "A01", "model-x", "draft", "L4_video", 300_000,
                                         user_id="admin")
    await BudgetLedger(_db()).record_job("ep02", "clip-3", "A02", "model-x", "draft", "L4_video", 80_000,
                                         user_id="admin")
    async with aiosqlite.connect(_db()) as db:  # in flight, but its provider already reported the charge
        await db.execute("UPDATE job_ledger SET actual_usd_micros = 50000, actual_source = ? WHERE job_id = 'clip-3'",
                         (ac.SOURCE_HF_USAGE,))
        await db.commit()

    def agreed():
        body = _admin(api).get("/api/dashboard").json()
        truth, summary = body["cost_truth"], body["spend_summary"]
        assert (summary["headline_spend_usd_micros"], summary["headline_actual_usd_micros"],
                summary["headline_coverage_pct"]) == (truth["totals"]["spend_usd_micros"],
                                                      truth["totals"]["actual_usd_micros"], truth["totals"]["coverage_pct"])
        assert {k: body["monthly_runrate"][k] for k in truth["monthly"]} == truth["monthly"]
        return summary, truth

    summary, truth = agreed()
    assert (summary["headline_spend_usd_micros"], summary["headline_label"]) == (520_000, "actual and estimated")
    assert truth["in_flight"] == {"jobs": 2, "held_usd_micros": 380_000}
    assert summary["total_estimated_usd_micros"] == 880_000
    # the projection also counts the charge already reported for the job in flight; the headline does not
    assert truth["monthly"]["spend_so_far_usd_micros"] == 570_000

    async with aiosqlite.connect(_db()) as db:
        await db.execute("UPDATE job_ledger SET actual_usd_micros = 90000, actual_source = ?, actual_backfilled = 1 "
                         "WHERE job_id = 'still-1'", (ac.SOURCE_HF_USAGE,))
        await db.commit()
    summary, truth = agreed()
    assert (summary["headline_spend_usd_micros"], summary["headline_label"]) == (510_000, "actual")
    assert truth["monthly"]["spend_so_far_usd_micros"] == 560_000


async def test_admins_assign_an_episodes_spend_to_a_person(api, people):
    one, _ = people
    client = _admin(api)
    await _job("clip-1", "ep01", "L4_video", 400_000)
    res = client.put("/api/dashboard/episodes/ep01/attribution", json={"user_id": one, "note": "shared laptop"})
    assert res.status_code == 200, res.text
    assert (res.json()["user_id"], res.json()["name"], res.json()["set_by"]) == (one, "Producer One", "admin secret")
    truth = client.get("/api/dashboard").json()["cost_truth"]
    assert truth["by_person"][0]["name"] == "Producer One" and truth["by_episode"][0]["attribution"] == "assigned"
    assert client.delete("/api/dashboard/episodes/ep01/attribution").json() == {"episode_id": "ep01", "cleared": True}
    assert client.delete("/api/dashboard/episodes/ep01/attribution").status_code == 404
    assert client.put("/api/dashboard/episodes/ep01/attribution", json={"user_id": "usr_nobody"}).status_code == 404
    assert client.put("/api/dashboard/episodes/ep01/attribution", json={"user_id": ""}).status_code == 422


async def test_spend_is_only_assigned_to_an_episode_that_exists(api, people):
    one, _ = people
    client = _admin(api)
    res = client.put("/api/dashboard/episodes/ep77/attribution", json={"user_id": one})
    assert res.status_code == 404 and "ep77" in res.json()["detail"], res.text
    assert client.put("/api/dashboard/episodes/ep7/attribution", json={"user_id": one}).status_code == 400
    from hfvg.spend_attribution import load_attribution
    async with aiosqlite.connect(_db()) as db:
        assert (await load_attribution(db)).assigned == {}
    await create_episode(_db(), "ep02", user_id=one)  # created in the studio, nothing spent yet
    assert client.put("/api/dashboard/episodes/ep02/attribution", json={"user_id": one}).status_code == 200


async def test_balance_check_and_usage_sync_routes(api, people):
    client = _admin(api)
    check = client.post("/api/balance/check")
    assert check.status_code == 200, check.text
    assert set(check.json()["overview"]) == {"readings", "consistency", "schedule"}
    sync = client.post("/api/costs/sync")
    assert sync.status_code == 200 and sync.json()["skipped"].startswith("DRY_RUN")


async def test_the_test_alert_reports_each_channel(api, people, monkeypatch):
    client = _admin(api)
    body = client.post("/api/alerts/test").json()
    assert body["delivered"] is False and body["results"] == {"none": "not sent: no alert destination configured"}
    assert body["status"]["configured"] is False
    monkeypatch.setenv("BALANCE_ALERT_WEBHOOK_URL", WEBHOOK)
    with respx.mock(assert_all_called=True) as mock:
        mock.post(WEBHOOK).mock(return_value=httpx.Response(200))
        body = client.post("/api/alerts/test").json()
    assert body["delivered"] is True and body["results"] == {"webhook": "sent"}
    status = client.get("/api/alerts/status").json()
    assert status["status"]["channels"] == ["webhook"]
    assert [r["delivered"] for r in status["recent"]] == [True, False]
    assert all("Requested by admin secret" in r["message"] for r in status["recent"])


async def test_who_started_the_run_is_recorded(api, people):
    one, _ = people
    member = await api.create_session(one, "member")
    await api._record_run_start("ep01", "run-1", member, "start")
    await api._record_run_start("ep01", "run-2", None, "start_again")
    with sqlite3.connect(_db()) as c:
        rows = c.execute("SELECT run_id, started_by, started_by_role, via FROM episode_runs ORDER BY rowid").fetchall()
    assert rows == [("run-1", one, "member", "start"), ("run-2", None, None, "start_again")]


async def test_a_failing_run_record_never_blocks_the_start(api, people, monkeypatch, capsys):
    monkeypatch.setattr(api, "DATABASE_PATH", os.path.dirname(_db()))
    await api._record_run_start("ep01", "run-1", None, "start")
    assert "not recorded for spend attribution" in capsys.readouterr().out


async def test_starting_an_episode_again_records_who_did(api, people):
    from fastapi.testclient import TestClient
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
    from temporalio.exceptions import ApplicationError
    from hfvg import audio_mix

    one, _ = people
    await create_episode(_db(), "ep05", user_id=one)
    beatmap = audio_mix.episode_dir("ep05") / "BEATMAP.md"
    beatmap.parent.mkdir(parents=True, exist_ok=True)
    beatmap.write_text("# the beatmap the run was started with\n")
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=MagicMock(
        id="ep05", run_id="run-0", status=WorkflowExecutionStatus.FAILED, workflow_type="EpisodeWorkflowV2"))
    handle.result = AsyncMock(side_effect=WorkflowFailureError(cause=ApplicationError("boom")))
    started = MagicMock()
    started.workflow_execution_started_event_attributes.input.payloads = ["the start inputs"]

    async def history(page_size=None):
        yield started

    handle.fetch_history_events = history
    temporal = MagicMock()
    temporal.get_workflow_handle = MagicMock(return_value=handle)
    temporal.start_workflow = AsyncMock(return_value=MagicMock(id="ep05", result_run_id="run-1"))
    temporal.data_converter.decode = AsyncMock(return_value=["ep05", str(beatmap), True, None, None, None, None, True])
    api.temporal_client = temporal
    client = TestClient(api.app)
    client.cookies.set("studio_admin_token", await api.create_session(one, "member"))
    res = client.post("/api/episodes/ep05/start-again")
    assert res.status_code == 200, res.text
    with sqlite3.connect(_db()) as c:
        rows = c.execute("SELECT run_id, started_by, via FROM episode_runs WHERE episode_id = 'ep05'").fetchall()
    assert rows == [("run-1", one, "start_again")]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_display_helpers_unit_tests():
    """Runs web/lib/cost-dashboard.test.mjs (how the page words coverage, balance readings and alert
    destinations) under pytest so red-green mutations of web/lib/cost-dashboard.mjs are caught too."""
    proc = subprocess.run(["node", "--test", "lib/cost-dashboard.test.mjs"], cwd=REPO / "web",
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
