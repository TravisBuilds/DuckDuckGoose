"""Higgsfield usage report (``GET /models/usage``): real spend, per-job actual costs, balance.

Documented at docs.higgsfield.ai/docs/enterprise/usage-and-billing (checked 2026-11): the completed
generations of the account the API key belongs to and their cost in USD per time bucket and model,
optionally per API key (``expand=auth_method``). Range at most 31 days, ``limit`` at most 100
buckets per page. Higgsfield calls it analytics, not a billing record: it can lag and miss requests,
so a bucket is only matched to jobs once it has settled (HIGGSFIELD_USAGE_SETTLE_HOURS after the
bucket ends), and a bucket that changes later is matched again.

Matching (``allocate_usage``): per settled UTC day and model, the report's generation count must
equal the studio's own count for that day and model (job ledger rows plus reference images, which
have no job row); then the reported cost is split over those generations in proportion to their
estimates and stored as each job's actual (source ``higgsfield_usage_report``). When the report
breaks usage down by API key and one of them is this studio's key, only that key's rows are used;
otherwise the whole account's rows are (correct only when the account is used by the studio
alone; the dashboard says which). Anything that does not match is kept and shown as unmatched
provider spend, never forced onto jobs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

import aiosqlite
import httpx

from hfvg.actual_costs import (
    Charge,
    SOURCE_HF_USAGE,
    _decimal,
    charge_update,
    ensure_cost_schema,
    now_iso,
    usd_decimal_to_micros,
)

logger = logging.getLogger(__name__)

PROVIDER = "higgsfield"
USAGE_PATH = "/models/usage"
MAX_RANGE = timedelta(days=31)
PAGE_LIMIT = 100
MAX_PAGES = 40
SETTLE_HOURS_ENV = "HIGGSFIELD_USAGE_SETTLE_HOURS"
DEFAULT_SETTLE_HOURS = 6
LOOKBACK_DAYS_ENV = "HIGGSFIELD_USAGE_LOOKBACK_DAYS"
DEFAULT_LOOKBACK_DAYS = 35
REF_EDIT_JOB = re.compile(r"-edit[0-9a-f]{8}$")


class UsageError(RuntimeError):
    """The usage report could not be read (HTTP error or a response not in the documented shape)."""


@dataclass(frozen=True)
class UsageRow:
    bucket_start: str
    model: str
    unit: str
    auth_method: str
    quantity: Decimal
    unit_price: Decimal | None
    cost: Decimal
    currency: str


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def api_key_parts(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """(key id, secret) of HIGGSFIELD_API_KEY ("id:secret"), None when unset or malformed."""
    raw = (_env(env).get("HIGGSFIELD_API_KEY") or "").strip()
    if ":" not in raw:
        return None
    key_id, secret = raw.split(":", 1)
    return (key_id, secret) if key_id and secret else None


def _int_env(env: Mapping[str, str] | None, name: str, default: int) -> int:
    raw = (_env(env).get(name) or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning(f"{name}={raw!r} is not a whole number; using {default}")
        return default
    return value if value >= 0 else default


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str) -> datetime:
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def parse_usage_page(body: Any) -> tuple[list[UsageRow], int, int | None]:
    """(rows, buckets on this page, total buckets) of one /models/usage page."""
    if not isinstance(body, dict) or not isinstance(body.get("time_series"), list):
        raise UsageError("unexpected /models/usage response: no time_series list")
    rows: list[UsageRow] = []
    for bucket in body["time_series"]:
        start = bucket.get("bucket") if isinstance(bucket, dict) else None
        results = bucket.get("results") if isinstance(bucket, dict) else None
        if not isinstance(start, str) or not isinstance(results, list):
            raise UsageError(f"unexpected /models/usage bucket: {str(bucket)[:200]}")
        try:
            start_iso = iso_z(_parse_ts(start))
        except ValueError:
            raise UsageError(f"unexpected /models/usage bucket time: {start!r}") from None
        for r in results:
            quantity = _decimal(r.get("quantity")) if isinstance(r, dict) else None
            cost = _decimal(r.get("cost")) if isinstance(r, dict) else None
            if quantity is None or cost is None or not r.get("model"):
                raise UsageError(f"unexpected /models/usage result: {str(r)[:200]}")
            rows.append(UsageRow(bucket_start=start_iso, model=str(r["model"]), unit=str(r.get("unit") or ""),
                                 auth_method=str(r.get("auth_method") or ""), quantity=quantity,
                                 unit_price=_decimal(r.get("unit_price")), cost=cost,
                                 currency=str(r.get("currency") or "USD").upper()))
    total = body.get("total")
    total = int(total) if isinstance(total, int) or (isinstance(total, str) and total.isdigit()) else None
    return rows, len(body["time_series"]), total


async def fetch_usage(start: datetime, end: datetime, *, timeframe: str = "day", per_key: bool = True,
                      bound_to_timeframe: bool = True, client: httpx.AsyncClient | None = None,
                      env: Mapping[str, str] | None = None) -> list[UsageRow]:
    """All usage rows between start and end (split into 31-day windows, every page read)."""
    key = api_key_parts(env)
    if key is None:
        raise UsageError("HIGGSFIELD_API_KEY is not set as id:secret")
    base = (_env(env).get("HIGGSFIELD_BASE_URL") or "https://api.higgsfield.ai").rstrip("/")
    headers = {"Authorization": f"Key {key[0]}:{key[1]}"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    rows: list[UsageRow] = []
    try:
        window_start = start
        while window_start < end:
            window_end = min(end, window_start + MAX_RANGE)
            page = 1
            while True:
                params = [("start", iso_z(window_start)), ("end", iso_z(window_end)), ("timeframe", timeframe),
                          ("expand", "time_series"), ("page", str(page)), ("limit", str(PAGE_LIMIT))]
                if per_key:
                    params.append(("expand", "auth_method"))
                if not bound_to_timeframe:
                    params.append(("bound_to_timeframe", "false"))
                resp = await client.get(f"{base}{USAGE_PATH}", params=params, headers=headers)
                if resp.status_code != 200:
                    raise UsageError(f"GET {USAGE_PATH} -> HTTP {resp.status_code}: {resp.text[:300]}")
                try:
                    body = resp.json()
                except ValueError:
                    raise UsageError(f"GET {USAGE_PATH} returned non-JSON: {resp.text[:200]}") from None
                page_rows, buckets, total = parse_usage_page(body)
                rows.extend(page_rows)
                if buckets == 0 or total is None or page * PAGE_LIMIT >= total:
                    break
                page += 1
                if page > MAX_PAGES:
                    raise UsageError(f"GET {USAGE_PATH}: more than {MAX_PAGES} pages for one window")
            window_start = window_end
    finally:
        if own:
            await client.aclose()
    return rows


async def usage_spend_since(since: datetime, *, now: datetime | None = None, client: httpx.AsyncClient | None = None,
                            env: Mapping[str, str] | None = None) -> Decimal | None:
    """USD the provider reports for the whole account (every API key) since ``since``, from hourly
    buckets not widened to bucket boundaries. None when no API key is configured. At most the last
    31 days are read (the range limit); older spend is not included."""
    if api_key_parts(env) is None:
        return None
    end = now or datetime.now(timezone.utc)
    start = max(since, end - MAX_RANGE)
    if start >= end:
        return Decimal("0")
    rows = await fetch_usage(start, end, timeframe="hour", per_key=False, bound_to_timeframe=False,
                             client=client, env=env)
    other = {r.currency for r in rows if r.currency != "USD"}
    if other:
        raise UsageError(f"usage report in {sorted(other)}, expected USD")
    return sum((r.cost for r in rows), Decimal("0"))


async def store_usage_rows(db: aiosqlite.Connection, rows: list[UsageRow], timeframe: str = "day") -> int:
    """Upsert report rows. A row whose quantity or cost changed is matched again."""
    fetched = now_iso()
    for r in rows:
        cost_micros = usd_decimal_to_micros(r.cost) if r.currency == "USD" else 0
        allocation = None if r.currency == "USD" else f"unmatched: cost in {r.currency}, not USD"
        await db.execute(
            """INSERT INTO provider_usage_buckets (provider, bucket_start, timeframe, model, unit, auth_method,
                   quantity, unit_price, cost_usd_micros, currency, fetched_at, allocation, allocated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
               ON CONFLICT(provider, bucket_start, timeframe, model, unit, auth_method) DO UPDATE SET
                   allocation = CASE WHEN provider_usage_buckets.quantity = excluded.quantity
                       AND provider_usage_buckets.cost_usd_micros = excluded.cost_usd_micros
                       THEN provider_usage_buckets.allocation ELSE excluded.allocation END,
                   allocated_at = CASE WHEN provider_usage_buckets.quantity = excluded.quantity
                       AND provider_usage_buckets.cost_usd_micros = excluded.cost_usd_micros
                       THEN provider_usage_buckets.allocated_at ELSE NULL END,
                   quantity = excluded.quantity, unit_price = excluded.unit_price,
                   cost_usd_micros = excluded.cost_usd_micros, currency = excluded.currency,
                   fetched_at = excluded.fetched_at""",
            (PROVIDER, r.bucket_start, timeframe, r.model, r.unit, r.auth_method, str(r.quantity),
             str(r.unit_price) if r.unit_price is not None else None, cost_micros, r.currency, fetched, allocation))
    return len(rows)


def _norm_model(model: str | None) -> str:
    return (model or "").strip().strip("/").lower()


def split_pro_rata(total_micros: int, weights: list[int]) -> list[int]:
    """total split in proportion to weights (equal split when they sum to 0); the shares add up to
    total exactly (largest remainder)."""
    n = len(weights)
    if n == 0:
        return []
    if sum(weights) <= 0:
        weights = [1] * n
    whole = sum(weights)
    raw = [total_micros * w for w in weights]
    shares = [x // whole for x in raw]
    leftover = total_micros - sum(shares)
    order = sorted(range(n), key=lambda i: (raw[i] % whole, -i), reverse=True)
    for i in order[:leftover]:
        shares[i] += 1
    return shares


async def _ledger_generations(db: aiosqlite.Connection, day: str) -> dict[str, list[tuple[str | None, int]]]:
    """This studio's Higgsfield generations committed for a UTC day, per normalised model:
    [(job_id or None for a reference image without a job row, estimate usd_micros)]."""
    from hfvg.config import still_model_path
    from hfvg.defaults import REF_MODEL_PATH

    out: dict[str, list[tuple[str | None, int]]] = defaultdict(list)
    async with db.execute(
        "SELECT job_id, model, COALESCE(estimate_usd_micros, usd_micros) FROM job_ledger "
        "WHERE status = 'committed' AND line_name NOT LIKE 'el|_%' ESCAPE '|' AND substr(created_at, 1, 10) = ?",
        (day,),
    ) as cur:
        async for job_id, model, estimate in cur:
            out[_norm_model(model)].append((job_id, int(estimate or 0)))
    async with db.execute(
        "SELECT bt.job_id, bt.amount FROM budget_transactions bt WHERE bt.txn_type = 'commit' "
        "AND bt.line_id IN (SELECT line_id FROM budget_lines WHERE line_name = 'L1_refs') "
        "AND substr(bt.job_id, 1, 4) = 'ref-' AND substr(bt.timestamp, 1, 10) = ? "
        "AND NOT EXISTS (SELECT 1 FROM job_ledger j WHERE j.job_id = bt.job_id)",
        (day,),
    ) as cur:
        async for job_id, amount in cur:
            model = still_model_path() if REF_EDIT_JOB.search(job_id or "") else REF_MODEL_PATH
            out[_norm_model(model)].append((None, int(amount or 0)))
    return out


async def allocate_usage(db: aiosqlite.Connection, *, now: datetime | None = None,
                         settle_hours: int | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Match settled day buckets to jobs and store each matched job's share as its actual."""
    now = now or datetime.now(timezone.utc)
    settle = timedelta(hours=_int_env(env, SETTLE_HOURS_ENV, DEFAULT_SETTLE_HOURS) if settle_hours is None
                       else settle_hours)
    key = api_key_parts(env)
    key_id = key[0] if key else None
    async with db.execute(
        "SELECT COUNT(*) FROM provider_usage_buckets WHERE provider = ? AND timeframe = 'day' AND auth_method = ?",
        (PROVIDER, key_id or "\x00"),
    ) as cur:
        key_mode = bool(key_id) and (await cur.fetchone())[0] > 0

    async with db.execute(
        "SELECT bucket_start, model, unit, auth_method, quantity, cost_usd_micros FROM provider_usage_buckets "
        "WHERE provider = ? AND timeframe = 'day' AND allocation IS NULL ORDER BY bucket_start",
        (PROVIDER,),
    ) as cur:
        pending = await cur.fetchall()

    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    summary = {"mode": "api_key" if key_mode else "account", "allocated_buckets": 0, "unmatched_buckets": 0,
               "jobs_updated": 0, "other_key_rows": 0, "unsettled_rows": 0}
    stamp = now_iso()
    for bucket_start, model, unit, auth_method, quantity, cost in pending:
        if _parse_ts(bucket_start) + timedelta(days=1) + settle > now:
            summary["unsettled_rows"] += 1
            continue
        if key_mode and auth_method != key_id:
            await db.execute(
                "UPDATE provider_usage_buckets SET allocation = 'other_api_key', allocated_at = ? WHERE provider = ? "
                "AND bucket_start = ? AND timeframe = 'day' AND model = ? AND unit = ? AND auth_method = ?",
                (stamp, PROVIDER, bucket_start, model, unit, auth_method))
            summary["other_key_rows"] += 1
            continue
        g = groups.setdefault((bucket_start, model, unit), {"quantity": Decimal("0"), "cost": 0, "auth": []})
        g["quantity"] += Decimal(quantity)
        g["cost"] += int(cost)
        g["auth"].append(auth_method)

    ledger_cache: dict[str, dict[str, list[tuple[str | None, int]]]] = {}
    for (bucket_start, model, unit), g in groups.items():
        day = bucket_start[:10]
        if day not in ledger_cache:
            ledger_cache[day] = await _ledger_generations(db, day)
        generations = ledger_cache[day].get(_norm_model(model), [])
        quantity: Decimal = g["quantity"]
        if not generations:
            verdict = "unmatched: no studio generations of this model that day"
        elif unit.lower() not in ("generation", "generations"):
            verdict = f"unmatched: unit {unit!r} cannot be counted against jobs"
        elif quantity != len(generations):
            verdict = f"unmatched: provider reports {quantity} generations, the studio ledger {len(generations)}"
        else:
            verdict = "allocated"
            ref = f"usage:{day}:{model}"
            for (job_id, _est), share in zip(generations, split_pro_rata(g["cost"], [e for _, e in generations])):
                if job_id is None:
                    continue
                charge = Charge(units=share / 1_000_000, unit="usd", usd_micros=share, rate_usd="1",
                                source=SOURCE_HF_USAGE, ref=ref)
                sql, params = charge_update(charge, job_id, recorded_at=stamp)
                cur = await db.execute(sql, params)
                summary["jobs_updated"] += cur.rowcount
        summary["allocated_buckets" if verdict == "allocated" else "unmatched_buckets"] += 1
        for auth_method in g["auth"]:
            await db.execute(
                "UPDATE provider_usage_buckets SET allocation = ?, allocated_at = ? WHERE provider = ? "
                "AND bucket_start = ? AND timeframe = 'day' AND model = ? AND unit = ? AND auth_method = ?",
                (verdict, stamp, PROVIDER, bucket_start, model, unit, auth_method))
    return summary


def _redact(text: str, env: Mapping[str, str] | None) -> str:
    key = api_key_parts(env)
    if key:
        text = text.replace(key[1], "[REDACTED]").replace(key[0], "[key-id]")
    return re.sub(r"Key [^\s:]+:[^\s,]+", "Key [REDACTED]", text)[:500]


async def sync_higgsfield_usage(db_path: str, *, now: datetime | None = None,
                                client: httpx.AsyncClient | None = None,
                                env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Fetch the last HIGGSFIELD_USAGE_LOOKBACK_DAYS of daily usage, store it, match settled buckets
    to jobs, and record the run. Never raises: the outcome (or the redacted error) is returned and
    kept in cost_sync_runs for the dashboard."""
    now = now or datetime.now(timezone.utc)
    if (_env(env).get("DRY_RUN", "true")).strip().lower() in ("true", "1"):
        return {"ok": False, "skipped": "DRY_RUN: no provider calls"}
    if api_key_parts(env) is None:
        result: dict[str, Any] = {"ok": False, "skipped": "HIGGSFIELD_API_KEY not set"}
    else:
        days = _int_env(env, LOOKBACK_DAYS_ENV, DEFAULT_LOOKBACK_DAYS) or DEFAULT_LOOKBACK_DAYS
        start = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            rows = await fetch_usage(start, now, timeframe="day", per_key=True, client=client, env=env)
        except (UsageError, httpx.HTTPError) as e:
            result = {"ok": False, "error": _redact(f"{type(e).__name__}: {e}", env)}
        else:
            async with aiosqlite.connect(db_path) as db:
                await ensure_cost_schema(db)
                stored = await store_usage_rows(db, rows)
                allocation = await allocate_usage(db, now=now, env=env)
                await db.commit()
            result = {"ok": True, "rows": stored, "from": iso_z(start), "to": iso_z(now), **allocation}
    async with aiosqlite.connect(db_path) as db:
        await ensure_cost_schema(db)
        await db.execute(
            "INSERT INTO cost_sync_runs (provider, ran_at, ok, summary, error) VALUES (?, ?, ?, ?, ?)",
            (PROVIDER, iso_z(now), int(bool(result.get("ok"))), json.dumps(result),
             result.get("error") or result.get("skipped")))
        await db.commit()
    return result
