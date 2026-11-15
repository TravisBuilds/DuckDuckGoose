"""Higgsfield usage report (GET /models/usage): parse, page, store, match to jobs, sync.

The responses are synthetic pages in the documented shape (docs.higgsfield.ai usage-and-billing:
time_series[].results[] with model, unit, quantity, unit_price, cost, currency and, with
expand=auth_method, the API key id). respx serves them: no network, nothing spent."""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite
import httpx
import pytest
import respx

from hfvg import actual_costs as ac
from hfvg import provider_usage as pu
from hfvg.budget import BudgetLedger
from hfvg.defaults import REF_MODEL_PATH

BASE = "https://api.higgsfield.ai"
STILL = "higgsfield-ai/soul/v2/standard"
CLIP = "kling-video/v3.0/pro/image-to-video"
DAY = "2026-11-01"
NOW = datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc)
ENV = {"HIGGSFIELD_API_KEY": "key-studio:secret-not-real", "HIGGSFIELD_BASE_URL": BASE, "DRY_RUN": "false"}


def _result(model, quantity, cost, unit="generation", auth="key-studio", currency="USD"):
    return {"model": model, "unit": unit, "quantity": str(quantity), "unit_price": "0.25", "cost": str(cost),
            "currency": currency, "auth_method": auth}


def _bucket(day, *results):
    return {"bucket": f"{day}T00:00:00Z", "results": list(results)}


def _page(buckets, total=None, page=1):
    return {"time_series": buckets, "total": len(buckets) if total is None else total, "page": page, "limit": 100}


def _db() -> str:
    return os.environ["DATABASE_PATH"]


@pytest.fixture(autouse=True)
async def _lines():
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    for line in ("L1_refs", "L2_drafts", "L4_video"):
        await ledger.set_line("ep01", line, 100_000_000, 80_000_000, "higgsfield")


async def _job(job_id, model, usd, *, line="L2_drafts", day=DAY, status="committed"):
    await BudgetLedger(_db()).record_job("ep01", job_id, "A01", model, "draft", line, usd)
    async with aiosqlite.connect(_db()) as db:
        await db.execute("UPDATE job_ledger SET status = ?, created_at = ? WHERE job_id = ?",
                         (status, f"{day}T10:00:00+00:00", job_id))
        await db.commit()


async def _ref_commit(job_id, usd, day=DAY):
    ledger = BudgetLedger(_db())
    assert await ledger.reserve("ep01", "L1_refs", usd, "ref", job_id=job_id)
    await ledger.commit("ep01", "L1_refs", usd, usd, job_id, "ref")
    async with aiosqlite.connect(_db()) as db:
        await db.execute("UPDATE budget_transactions SET timestamp = ? WHERE job_id = ?", (f"{day}T09:00:00+00:00", job_id))
        await db.commit()


async def _actual(job_id):
    async with aiosqlite.connect(_db()) as db:
        async with db.execute("SELECT actual_usd_micros, actual_source, actual_ref FROM job_ledger WHERE job_id = ?",
                              (job_id,)) as cur:
            return await cur.fetchone()


async def _buckets():
    async with aiosqlite.connect(_db()) as db:
        async with db.execute("SELECT model, unit, auth_method, allocation FROM provider_usage_buckets "
                              "ORDER BY model, unit, auth_method") as cur:
            return [tuple(r) for r in await cur.fetchall()]


async def _store_and_allocate(page, env=ENV, now=NOW):
    rows, _, _ = pu.parse_usage_page(page)
    async with aiosqlite.connect(_db()) as db:
        await ac.ensure_cost_schema(db)
        await pu.store_usage_rows(db, rows)
        summary = await pu.allocate_usage(db, now=now, env=env)
        await db.commit()
    return summary


# ------------------------------------------------------------------ parsing

def test_documented_page_parses():
    rows, buckets, total = pu.parse_usage_page(_page([_bucket(DAY, _result(STILL, 120, "11.28"))], total=23))
    assert (buckets, total) == (1, 23)
    (row,) = rows
    assert (row.bucket_start, row.model, row.unit, row.auth_method) == (f"{DAY}T00:00:00Z", STILL, "generation", "key-studio")
    assert (row.quantity, row.cost, row.currency, row.unit_price) == (Decimal(120), Decimal("11.28"), "USD", Decimal("0.25"))


@pytest.mark.parametrize("body", [
    [], {"data": []}, {"time_series": [{"bucket": DAY}]}, {"time_series": [{"bucket": "yesterday", "results": []}]},
    {"time_series": [_bucket(DAY, {"model": STILL, "quantity": "1"})]},
    {"time_series": [_bucket(DAY, {"model": STILL, "quantity": "-1", "cost": "1"})]},
])
def test_unexpected_shapes_fail_loudly(body):
    with pytest.raises(pu.UsageError):
        pu.parse_usage_page(body)


def test_split_pro_rata_adds_up_exactly():
    assert pu.split_pro_rata(1_000_000, [1, 1, 1]) == [333_334, 333_333, 333_333]
    assert sum(pu.split_pro_rata(999_999, [100_000, 300_000, 7])) == 999_999
    assert pu.split_pro_rata(10, [0, 0]) == [5, 5]
    assert pu.split_pro_rata(5, []) == []


# ------------------------------------------------------------------ fetching

async def test_fetch_reads_every_page_with_the_documented_parameters():
    seen = []

    def respond(request):
        params = request.url.params
        seen.append((params.get("page"), params.get_list("expand"), params.get("timeframe"), request.headers["authorization"]))
        if params["page"] == "1":
            return httpx.Response(200, json=_page([_bucket(DAY, _result(STILL, 1, "0.25"))] * 100, total=101))
        return httpx.Response(200, json=_page([_bucket("2026-11-02", _result(CLIP, 1, "1.5"))], total=101, page=2))

    with respx.mock() as mock:
        mock.get(f"{BASE}/models/usage").mock(side_effect=respond)
        rows = await pu.fetch_usage(datetime(2026, 11, 1, tzinfo=timezone.utc), NOW, env=ENV)
    assert len(rows) == 101 and rows[-1].model == CLIP
    assert [s[0] for s in seen] == ["1", "2"]
    assert seen[0][1] == ["time_series", "auth_method"] and seen[0][2] == "day"
    assert seen[0][3] == "Key key-studio:secret-not-real"


async def test_fetch_splits_ranges_over_31_days():
    windows = []

    def respond(request):
        windows.append((request.url.params["start"], request.url.params["end"]))
        return httpx.Response(200, json=_page([]))

    with respx.mock() as mock:
        mock.get(f"{BASE}/models/usage").mock(side_effect=respond)
        await pu.fetch_usage(datetime(2026, 9, 1, tzinfo=timezone.utc), NOW, env=ENV)
    assert windows == [("2026-09-01T00:00:00Z", "2026-10-02T00:00:00Z"), ("2026-10-02T00:00:00Z", "2026-11-02T00:00:00Z"),
                       ("2026-11-02T00:00:00Z", "2026-11-03T00:00:00Z")]


async def test_usage_spend_since_sums_the_whole_account_in_usd():
    page = _page([{"bucket": "2026-11-02T10:00:00Z", "results": [_result(STILL, 2, "0.5"), _result(CLIP, 1, "1.5", auth="key-other")]}])
    with respx.mock() as mock:
        route = mock.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json=page))
        spend = await pu.usage_spend_since(datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc), now=NOW, env=ENV)
    assert spend == Decimal("2.0")
    params = route.calls[0].request.url.params
    assert (params["timeframe"], params["bound_to_timeframe"], params.get_list("expand")) == ("hour", "false", ["time_series"])
    assert await pu.usage_spend_since(NOW, env={}) is None, "no key: no figure, no call"


async def test_usage_spend_in_another_currency_is_refused():
    page = _page([_bucket(DAY, _result(STILL, 2, "40", currency="credits"))])
    with respx.mock() as mock:
        mock.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json=page))
        with pytest.raises(pu.UsageError, match="expected USD"):
            await pu.usage_spend_since(datetime(2026, 11, 1, tzinfo=timezone.utc), now=NOW, env=ENV)


# ------------------------------------------------------------------ allocation

async def test_matching_day_and_model_counts_split_the_reported_cost():
    await _job("still-a", STILL, 100_000)
    await _job("still-b", STILL, 300_000)
    summary = await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 2, "0.5"))]))
    assert summary["allocated_buckets"] == 1 and summary["jobs_updated"] == 2 and summary["mode"] == "api_key"
    assert await _actual("still-a") == (125_000, ac.SOURCE_HF_USAGE, f"usage:{DAY}:{STILL}")
    assert await _actual("still-b") == (375_000, ac.SOURCE_HF_USAGE, f"usage:{DAY}:{STILL}")


async def test_reference_images_without_a_job_row_take_their_share():
    await _job("still-a", REF_MODEL_PATH, 100_000)
    await _ref_commit("ref-ep01-lead", 100_000)
    await _store_and_allocate(_page([_bucket(DAY, _result(REF_MODEL_PATH, 2, "0.4"))]))
    assert (await _actual("still-a"))[0] == 200_000, "the reference image keeps its half"


async def test_counts_that_differ_stay_unmatched_and_untouched():
    await _job("clip-a", CLIP, 336_000, line="L4_video")
    summary = await _store_and_allocate(_page([_bucket(DAY, _result(CLIP, 2, "3.0"), _result(STILL, 1, "0.3"),
                                                       _result(CLIP, 5, "0.5", unit="second"))]))
    assert summary["unmatched_buckets"] == 3 and summary["jobs_updated"] == 0
    assert await _actual("clip-a") == (None, None, None)
    allocations = {(m, u): a for m, u, _, a in await _buckets()}
    assert allocations[(CLIP, "generation")] == "unmatched: provider reports 2 generations, the studio ledger 1"
    assert allocations[(STILL, "generation")] == "unmatched: no studio generations of this model that day"
    assert allocations[(CLIP, "second")].startswith("unmatched: unit 'second'")


async def test_unsettled_days_wait():
    await _job("still-a", STILL, 100_000, day="2026-11-02")
    summary = await _store_and_allocate(_page([_bucket("2026-11-02", _result(STILL, 1, "0.25"))]))
    assert summary["unsettled_rows"] == 1 and await _actual("still-a") == (None, None, None)
    later = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    async with aiosqlite.connect(_db()) as db:
        summary = await pu.allocate_usage(db, now=later, env=ENV)
        await db.commit()
    assert summary["allocated_buckets"] == 1 and (await _actual("still-a"))[0] == 250_000


async def test_other_api_keys_are_not_this_studios_spend():
    await _job("still-a", STILL, 100_000)
    summary = await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 1, "0.25"), _result(STILL, 3, "0.75", auth="key-other"))]))
    assert summary["mode"] == "api_key" and summary["other_key_rows"] == 1
    assert (await _actual("still-a"))[0] == 250_000
    assert ("higgsfield-ai/soul/v2/standard", "generation", "key-other", "other_api_key") in await _buckets()


async def test_without_per_key_rows_the_whole_account_is_matched():
    await _job("still-a", STILL, 100_000)
    await _job("still-b", STILL, 100_000)
    summary = await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 1, "0.2", auth="key-x"), _result(STILL, 1, "0.2", auth="key-y"))]))
    assert summary["mode"] == "account" and summary["allocated_buckets"] == 1
    assert (await _actual("still-a"))[0] + (await _actual("still-b"))[0] == 400_000


async def test_a_bucket_that_changes_is_matched_again():
    await _job("still-a", STILL, 100_000)
    await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 1, "0.25"))]))
    await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 1, "0.30"))]))
    assert (await _actual("still-a"))[0] == 300_000


async def test_allocation_never_replaces_a_per_request_charge():
    await _job("still-a", STILL, 100_000)
    await ac.record_charge(_db(), "still-a", ac.Charge(0.11, "usd", 110_000, "1", ac.SOURCE_HF_STATUS))
    summary = await _store_and_allocate(_page([_bucket(DAY, _result(STILL, 1, "0.25"))]))
    assert summary["jobs_updated"] == 0
    assert await _actual("still-a") == (110_000, ac.SOURCE_HF_STATUS, None)


# ------------------------------------------------------------------ sync runs

async def _sync_runs():
    async with aiosqlite.connect(_db()) as db:
        async with db.execute("SELECT ok, summary, error FROM cost_sync_runs ORDER BY id") as cur:
            return [(ok, json.loads(summary), error) for ok, summary, error in await cur.fetchall()]


async def test_sync_is_skipped_in_dry_run_without_a_record():
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json=_page([])))
        result = await pu.sync_higgsfield_usage(_db(), now=NOW, env={**ENV, "DRY_RUN": "true"})
    assert result["skipped"].startswith("DRY_RUN") and route.call_count == 0
    assert await _sync_runs() == []


async def test_sync_without_a_key_is_recorded_as_skipped():
    result = await pu.sync_higgsfield_usage(_db(), now=NOW, env={"DRY_RUN": "false"})
    assert result == {"ok": False, "skipped": "HIGGSFIELD_API_KEY not set"}
    assert await _sync_runs() == [(0, result, "HIGGSFIELD_API_KEY not set")]


async def test_sync_failure_is_recorded_without_the_secret():
    with respx.mock() as mock:
        mock.get(f"{BASE}/models/usage").mock(
            return_value=httpx.Response(401, text="bad credentials Key key-studio:secret-not-real"))
        result = await pu.sync_higgsfield_usage(_db(), now=NOW, env=ENV)
    assert result["ok"] is False and "HTTP 401" in result["error"]
    assert "secret-not-real" not in result["error"] and "secret-not-real" not in json.dumps(await _sync_runs())


async def test_sync_stores_and_matches_the_report():
    await _job("still-a", STILL, 100_000)
    with respx.mock() as mock:
        mock.get(f"{BASE}/models/usage").mock(
            return_value=httpx.Response(200, json=_page([_bucket(DAY, _result(STILL, 1, "0.25"))])))
        result = await pu.sync_higgsfield_usage(_db(), now=NOW, env={**ENV, "HIGGSFIELD_USAGE_LOOKBACK_DAYS": "5"})
    assert result["ok"] is True and result["rows"] == 1 and result["jobs_updated"] == 1
    assert (await _actual("still-a"))[0] == 250_000
    ((ok, summary, error),) = await _sync_runs()
    assert ok == 1 and error is None and summary["allocated_buckets"] == 1
