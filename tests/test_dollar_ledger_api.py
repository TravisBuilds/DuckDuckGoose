"""Dollar ledger: API routes (budget, per-job ledger, manual balance) and live activity wiring.

All data is synthetic. Every amount is a literal in integer usd_micros (1 USD = 1_000_000).
"""

import sys

import aiosqlite
import httpx
import pytest

from hfvg.budget import BudgetLedger
from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode
from tests.ledger_helpers import set_balance


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "dollar_ledger.db")
    await init_studio_db(path)
    await BudgetLedger(path).init_db()
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("DRY_RUN", "true")
    yield path


@pytest.fixture(autouse=True)
def fresh_api_module():
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


def _client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    from fastapi.testclient import TestClient
    from api.main import app

    client = TestClient(app)
    response = client.post("/api/login", json={"admin_secret": "a" * 32})
    assert response.status_code == 200
    return client, response.cookies


@pytest.mark.asyncio
async def test_budget_route_reports_usd_micros(db_path, monkeypatch):
    await create_episode(db_path, "ep99")
    ledger = BudgetLedger(db_path)
    await ledger.set_line("ep99", "L2_drafts", 10_000_000, 8_000_000)
    await ledger.reserve("ep99", "L2_drafts", 1_500_000, "synthetic")
    client, cookies = _client(monkeypatch)

    response = client.get("/api/episodes/ep99/budget", cookies=cookies)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["unit"] == "usd_micros"
    assert data["episode_cap"] == 60_000_000 and data["episode_cap_usd"] == "60.00"
    assert data["episode_stop"] == 48_000_000 and data["episode_stop_usd"] == "48.00"
    assert data["gc01_headroom"] == 5_000_000 and data["gc01_headroom_usd"] == "5.00"
    assert data["episode_total"] == 1_500_000 and data["episode_total_usd"] == "1.50"
    line = [item for item in data["lines"] if item["line_name"] == "L2_drafts"][0]
    assert line["reserved"] == 1_500_000 and line["cap"] == 10_000_000 and line["stop"] == 8_000_000
    assert isinstance(line["reserved"], int)


@pytest.mark.asyncio
async def test_manual_balance_route_admin_only_and_audited(db_path, monkeypatch):
    await create_episode(db_path, "ep99")
    client, cookies = _client(monkeypatch)

    # No cookie (fresh client, no login): refused and nothing stored
    from fastapi.testclient import TestClient
    from api.main import app
    anonymous = TestClient(app)
    assert anonymous.post("/api/budget/balance", json={"balance_usd": "82.40"}).status_code in (401, 403)
    assert (await BudgetLedger(db_path).get_balance_status())["manual_balance_usd_micros"] is None
    # Garbage amount: refused, nothing stored
    bad = client.post("/api/budget/balance", json={"balance_usd": "abc"}, cookies=cookies)
    assert bad.status_code == 422, bad.text

    ok = client.post("/api/budget/balance", json={"balance_usd": "82.40", "note": "console"},
                     cookies=cookies)
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["balance_usd_micros"] == 82_400_000
    assert body["set_at"], "entry carries a timestamp"
    assert body["status"]["balance_remaining_usd_micros"] == 82_400_000

    got = client.get("/api/budget/balance", cookies=cookies).json()
    assert got["manual_balance_usd_micros"] == 82_400_000
    assert got["gc01_headroom_usd_micros"] == 5_000_000

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'set_manual_balance'"
        ) as cur:
            assert (await cur.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_job_ledger_route_and_verdict(db_path, monkeypatch):
    await create_episode(db_path, "ep99")
    ledger = BudgetLedger(db_path)
    await ledger.set_line("ep99", "L4_video", 20_000_000, 16_000_000)
    await ledger.reserve("ep99", "L4_video", 280_000)
    await ledger.record_job("ep99", "job-A01", "A01", "kling-3.0-pro", "final", "L4_video", 280_000)
    client, cookies = _client(monkeypatch)

    data = client.get("/api/episodes/ep99/jobs", cookies=cookies).json()
    assert data["unit"] == "usd_micros"
    assert len(data["jobs"]) == 1
    job = data["jobs"][0]
    assert job["job_id"] == "job-A01" and job["shot_id"] == "A01"
    assert job["model"] == "kling-3.0-pro" and job["tier"] == "final"
    assert job["usd_micros"] == 280_000 and job["usd"] == "0.28"
    assert job["status"] == "reserved" and job["verdict"] is None and job["used"] is None

    assert client.post("/api/episodes/ep99/jobs/nope/verdict",
                       json={"verdict": "passed"}, cookies=cookies).status_code == 404
    ok = client.post("/api/episodes/ep99/jobs/job-A01/verdict",
                     json={"verdict": "needs_review", "used": False}, cookies=cookies)
    assert ok.status_code == 200, ok.text
    job = client.get("/api/episodes/ep99/jobs", cookies=cookies).json()["jobs"][0]
    assert job["verdict"] == "needs_review" and job["used"] == "unused"


async def _live_episode(db_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    await create_episode(db_path, "ep99")
    await approve_g108(db_path, "ep99")
    await set_live_mode(db_path, "ep99", True)
    ledger = BudgetLedger(db_path)
    await ledger.set_line("ep99", "L2_drafts", 10_000_000, 8_000_000)
    await ledger.set_line("ep99", "L7_revision", 10_000_000, 10_000_000)
    return ledger


def _mock_still(respx_mock, usd="0.06"):
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": usd})
    )
    return respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "req-live-1"})
    )


@pytest.mark.asyncio
async def test_live_still_refused_without_manual_balance(db_path, monkeypatch, respx_mock):
    """GC.01: with no manual balance recorded a live paid job is refused BEFORE the provider call."""
    from hfvg.activities.studio_generation import submit_still_job_enforced

    ledger = await _live_episode(db_path, monkeypatch)
    submit = _mock_still(respx_mock)

    with pytest.raises(ValueError) as exc_info:
        await submit_still_job_enforced("ep99", "A01", "Synthetic prompt", 1)

    assert "manual" in str(exc_info.value).lower()
    assert not submit.called, "provider must not be called without a balance"
    assert (await ledger.get_line_status("ep99", "L2_drafts"))["reserved"] == 0


@pytest.mark.asyncio
async def test_live_still_refused_below_gc01_headroom(db_path, monkeypatch, respx_mock):
    """GC.01: balance 5.05 USD - 0.06 USD hold = 4.99 USD < 5.00 USD headroom -> refused."""
    from hfvg.activities.studio_generation import submit_still_job_enforced

    ledger = await _live_episode(db_path, monkeypatch)
    await ledger.set_manual_balance(5_050_000, "test", "low balance")
    submit = _mock_still(respx_mock)

    with pytest.raises(ValueError) as exc_info:
        await submit_still_job_enforced("ep99", "A01", "Synthetic prompt", 1)

    assert "GC.01" in str(exc_info.value), str(exc_info.value)
    assert not submit.called
    assert (await ledger.get_line_status("ep99", "L2_drafts"))["reserved"] == 0


@pytest.mark.asyncio
async def test_live_still_records_job_and_reserves_estimate_usd(db_path, monkeypatch, respx_mock):
    from hfvg.activities.studio_generation import (
        commit_job_budget,
        release_job_budget,
        submit_still_job_enforced,
    )

    ledger = await _live_episode(db_path, monkeypatch)
    await set_balance(db_path, "100.00")
    submit = _mock_still(respx_mock, usd="0.06")

    result = await submit_still_job_enforced("ep99", "A01", "Synthetic prompt", 1)

    assert submit.call_count == 1
    assert result["job_id"] == "req-live-1"
    assert result["line_name"] == "L2_drafts"
    assert result["reserved_amount"] == 60_000
    status = await ledger.get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 60_000
    jobs = await ledger.get_jobs("ep99")
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "req-live-1" and jobs[0]["shot_id"] == "A01"
    assert jobs[0]["model"] == "xai/grok-imagine-image-2.0"
    assert jobs[0]["tier"] == "draft" and jobs[0]["usd_micros"] == 60_000
    assert jobs[0]["status"] == "reserved"

    # Commit twice (activity retry): spent exactly once
    for _ in range(2):
        await commit_job_budget("ep99", "A01", "req-live-1", "still", "L2_drafts", 60_000, None)
    status = await ledger.get_line_status("ep99", "L2_drafts")
    assert status["spent"] == 60_000 and status["reserved"] == 0
    # A late release for a committed job changes nothing
    await release_job_budget("ep99", "A01", "req-live-1", "still", "L2_drafts", 60_000, "late")
    status = await ledger.get_line_status("ep99", "L2_drafts")
    assert status["spent"] == 60_000 and status["reserved"] == 0
    assert (await ledger.get_jobs("ep99"))[0]["status"] == "committed"


@pytest.mark.asyncio
async def test_revision_tier_draws_on_revision_reserve(db_path, monkeypatch, respx_mock):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    ledger = await _live_episode(db_path, monkeypatch)
    await set_balance(db_path, "100.00")
    _mock_still(respx_mock, usd="0.06")

    result = await submit_still_job_enforced(
        "ep99", "A01", "Synthetic prompt", 1, None, "1k", "medium", "9:16", "revision"
    )

    assert result["line_name"] == "L7_revision"
    assert (await ledger.get_line_status("ep99", "L7_revision"))["reserved"] == 60_000
    assert (await ledger.get_line_status("ep99", "L2_drafts"))["reserved"] == 0
    jobs = await ledger.get_jobs("ep99")
    assert jobs[0]["tier"] == "revision" and jobs[0]["line_name"] == "L7_revision"


@pytest.mark.asyncio
async def test_still_estimate_without_usd_is_refused(db_path, monkeypatch, respx_mock):
    """No USD in the estimate and no rate table for stills: fail closed, nothing reserved."""
    from hfvg.activities.studio_generation import submit_still_job_enforced

    ledger = await _live_episode(db_path, monkeypatch)
    await set_balance(db_path, "100.00")
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0"})
    )
    submit = respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"})
    )

    with pytest.raises(ValueError):
        await submit_still_job_enforced("ep99", "A01", "Synthetic prompt", 1)

    assert not submit.called
    assert (await ledger.get_line_status("ep99", "L2_drafts"))["reserved"] == 0


@pytest.mark.asyncio
async def test_clip_estimate_without_usd_uses_rate_table(db_path, monkeypatch, respx_mock):
    """Kling estimate with a pricing description only: per-second rate table (derived, list price)."""
    from hfvg.providers import KlingVideoProvider

    respx_mock.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"type": "description", "pricing_description": "per second"})
    )
    provider = KlingVideoProvider(api_key="test_id:test_secret", base_url="https://api.higgsfield.ai")
    try:
        micros = await provider.estimate_usd_micros("https://example.com/s.jpg", "p", 5)
    finally:
        await provider.close()
    assert micros == 280_000  # 5 s x 56_000 usd_micros/s from the configured rate table


def test_rate_table_literals_and_ceiling():
    from hfvg.pricing import rate_table_estimate

    est = rate_table_estimate("seedance-2.5", "480p", 4)
    assert est.usd_micros == 822_400 and est.derived is True and est.source == "rate_table"
    assert rate_table_estimate("seedance-2.5", "720p", 4).usd_micros == 1_848_800
    assert rate_table_estimate("seedance-2.5", "1080p", 4).usd_micros == 4_548_800
    with pytest.raises(ValueError):
        rate_table_estimate("no-such-model", "480p", 4)
