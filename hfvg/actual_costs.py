"""Actual provider charges per job, stored next to the estimate (docs/ACTUAL-COSTS.md).

``job_ledger.usd_micros`` stays the dollar guard's number (what it held and committed). The columns
added here record what the provider says it charged, with where the number came from:

- ElevenLabs: the ``character-cost`` response header of the call that produced the audio (credits
  charged), converted at ELEVENLABS_USD_PER_CREDIT.
- Higgsfield: a charge field in the completed request's status body (the documented body has none;
  the names in STATUS_*_FIELDS are read if the provider adds one), else the provider's usage report
  (``GET /models/usage``, see hfvg.provider_usage) allocated to the jobs of that day and model.
  Higgsfield bills the API in USD; a charge reported in credits is converted at
  HIGGSFIELD_USD_PER_CREDIT and left without a dollar value while that rate is unset.

Rows filled after the fact from data already stored are labelled ``actual_backfilled = 1``.

Every change here is additive (new nullable columns and new tables), so older code keeps working
against a migrated database and open workflow runs are unaffected.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

HIGGSFIELD_USD_PER_CREDIT_ENV = "HIGGSFIELD_USD_PER_CREDIT"

SOURCE_EL_HEADER = "elevenlabs_character_cost_header"
SOURCE_HF_STATUS = "higgsfield_status_charge"
SOURCE_HF_USAGE = "higgsfield_usage_report"
PER_REQUEST_SOURCES = (SOURCE_EL_HEADER, SOURCE_HF_STATUS)

ESTIMATE_RESERVATION = "reservation"
ESTIMATE_EL_PRICE = "elevenlabs_price_estimate"
ESTIMATE_BACKFILL = "backfill_job_row"
ESTIMATE_NOT_RECORDED = "not_recorded"

STATUS_USD_FIELDS = ("charged_usd", "cost_usd", "price_usd")
STATUS_CREDIT_FIELDS = ("credits_charged", "charged_credits", "credits_used", "credits_spent")

JOB_LEDGER_COST_COLUMNS = (
    ("estimate_usd_micros", "INTEGER"),
    ("estimate_source", "TEXT"),
    ("actual_usd_micros", "INTEGER"),
    ("actual_units", "REAL"),
    ("actual_unit", "TEXT"),
    ("actual_rate_usd", "TEXT"),
    ("actual_source", "TEXT"),
    ("actual_ref", "TEXT"),
    ("actual_backfilled", "INTEGER NOT NULL DEFAULT 0"),
    ("actual_recorded_at", "TEXT"),
)

COST_TABLES = (
    # Who started each run of an episode (the person, not only the episode's owner). started_by is
    # the session user id; NULL for the Bearer ADMIN_SECRET, which names nobody.
    """CREATE TABLE IF NOT EXISTS episode_runs (
        episode_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        started_by TEXT,
        started_by_role TEXT,
        via TEXT NOT NULL,
        PRIMARY KEY (episode_id, run_id)
    )""",
    # An admin's explicit "this person ran the episode" for spend reports (does not change who may
    # edit the episode). Wins over every automatic attribution.
    """CREATE TABLE IF NOT EXISTS episode_spend_owner (
        episode_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        set_by TEXT,
        set_at TEXT NOT NULL,
        note TEXT
    )""",
    # Every balance reading (preflight before a paid step, the schedule, on demand), with when it
    # was taken and what it was derived from.
    """CREATE TABLE IF NOT EXISTS provider_balance_readings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        read_at TEXT NOT NULL,
        read_for TEXT NOT NULL,
        source TEXT NOT NULL,
        balance_usd_micros INTEGER,
        details TEXT NOT NULL,
        error TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_provider_balance_readings ON provider_balance_readings(provider, read_at)",
    # Higgsfield usage report rows (one per bucket x model x unit x API key) and how they were
    # matched to jobs. allocation: NULL (not yet), 'allocated', or 'unmatched: <reason>'.
    """CREATE TABLE IF NOT EXISTS provider_usage_buckets (
        provider TEXT NOT NULL,
        bucket_start TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        model TEXT NOT NULL,
        unit TEXT NOT NULL,
        auth_method TEXT NOT NULL DEFAULT '',
        quantity TEXT NOT NULL,
        unit_price TEXT,
        cost_usd_micros INTEGER NOT NULL,
        currency TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        allocation TEXT,
        allocated_at TEXT,
        PRIMARY KEY (provider, bucket_start, timeframe, model, unit, auth_method)
    )""",
    """CREATE TABLE IF NOT EXISTS cost_sync_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        ran_at TEXT NOT NULL,
        ok INTEGER NOT NULL,
        summary TEXT NOT NULL,
        error TEXT
    )""",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Charge:
    """What a provider reported charging for one job."""
    units: float
    unit: str  # "credits" or "usd"
    usd_micros: int | None  # None: reported in credits and no rate configured
    rate_usd: str | None  # USD per unit used for usd_micros
    source: str
    ref: str | None = None


def _decimal(value: Any) -> Decimal | None:
    """A finite, non-negative decimal from a number or numeric string; None otherwise."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return d


def usd_decimal_to_micros(usd: Decimal) -> int:
    return int((usd * 1_000_000).to_integral_value(rounding=ROUND_HALF_UP))


def higgsfield_rate() -> tuple[Decimal | None, str | None]:
    """(USD per Higgsfield credit, problem). Unset is not a problem (the API bills in USD); a value
    that is not a positive number is reported and ignored."""
    raw = os.getenv(HIGGSFIELD_USD_PER_CREDIT_ENV, "").strip()
    if not raw:
        return None, None
    rate = _decimal(raw)
    if rate is None or rate <= 0:
        return None, f"{HIGGSFIELD_USD_PER_CREDIT_ENV} must be a positive number (USD per credit), got {raw!r}"
    return rate, None


def elevenlabs_rate_str() -> str:
    from hfvg.providers.elevenlabs import DEFAULT_USD_PER_CREDIT, USD_PER_CREDIT_ENV
    return os.getenv(USD_PER_CREDIT_ENV, DEFAULT_USD_PER_CREDIT).strip()


def elevenlabs_charge(credits: float) -> Charge:
    """The charge of one ElevenLabs call from its character-cost header (credits). Uses the same
    conversion as the guard's commit, so the recorded actual equals the committed amount."""
    from hfvg.providers.elevenlabs import credits_to_usd_micros
    return Charge(units=float(credits), unit="credits", usd_micros=credits_to_usd_micros(float(credits)),
                  rate_usd=elevenlabs_rate_str(), source=SOURCE_EL_HEADER)


def _credits_charge(credits: Decimal, source: str, ref: str | None = None) -> Charge:
    rate, _problem = higgsfield_rate()
    usd = usd_decimal_to_micros(credits * rate) if rate is not None else None
    return Charge(units=float(credits), unit="credits", usd_micros=usd,
                  rate_usd=str(rate) if rate is not None else None, source=source, ref=ref)


def higgsfield_status_charge(raw: dict[str, Any] | None) -> Charge | None:
    """The charge in a completed Higgsfield request status, or None when it reports none.

    Recognised: a USD field (STATUS_USD_FIELDS), a credits field (STATUS_CREDIT_FIELDS), or ``cost``
    with ``currency`` "USD" / "credits", at the top level or in a ``billing`` / ``usage`` object.
    A bare ``cost`` without a currency is ignored: its unit is unknown."""
    if not isinstance(raw, dict):
        return None
    scopes = [raw] + [raw[k] for k in ("billing", "usage") if isinstance(raw.get(k), dict)]
    for scope in scopes:
        for field in STATUS_USD_FIELDS:
            usd = _decimal(scope.get(field))
            if usd is not None:
                return Charge(units=float(usd), unit="usd", usd_micros=usd_decimal_to_micros(usd),
                              rate_usd="1", source=SOURCE_HF_STATUS, ref=f"status.{field}")
        for field in STATUS_CREDIT_FIELDS:
            credits = _decimal(scope.get(field))
            if credits is not None:
                return _credits_charge(credits, SOURCE_HF_STATUS, f"status.{field}")
        cost = _decimal(scope.get("cost"))
        currency = str(scope.get("currency") or "").strip().lower()
        if cost is not None and currency == "usd":
            return Charge(units=float(cost), unit="usd", usd_micros=usd_decimal_to_micros(cost),
                          rate_usd="1", source=SOURCE_HF_STATUS, ref="status.cost")
        if cost is not None and currency in ("credit", "credits"):
            return _credits_charge(cost, SOURCE_HF_STATUS, "status.cost")
    return None


async def ensure_cost_schema(db: aiosqlite.Connection) -> None:
    """Add the cost columns and tables (idempotent, additive). The caller commits."""
    async with db.execute("PRAGMA table_info(job_ledger)") as cur:
        cols = {r[1] async for r in cur}
    if cols:
        for col, decl in JOB_LEDGER_COST_COLUMNS:
            if col not in cols:
                await db.execute(f"ALTER TABLE job_ledger ADD COLUMN {col} {decl}")
    for statement in COST_TABLES:
        await db.execute(statement)


_RECORD_SQL = """
    UPDATE job_ledger SET actual_usd_micros = ?, actual_units = ?, actual_unit = ?, actual_rate_usd = ?,
        actual_source = ?, actual_ref = ?, actual_backfilled = ?, actual_recorded_at = ?
    WHERE job_id = ?
"""
_ALLOCATION_GUARD = (" AND ((actual_source IS NULL AND actual_usd_micros IS NULL)"
                     f" OR actual_source = '{SOURCE_HF_USAGE}')")


def charge_update(charge: Charge, job_id: str, *, backfilled: bool = False,
                  recorded_at: str | None = None) -> tuple[str, tuple]:
    """(SQL, params) storing the charge on the job row. A per-request charge replaces anything;
    a usage-report allocation fills only an empty row or replaces an earlier allocation."""
    sql = _RECORD_SQL
    if charge.source not in PER_REQUEST_SOURCES:
        sql = sql.rstrip() + _ALLOCATION_GUARD
    return sql, (charge.usd_micros, charge.units, charge.unit, charge.rate_usd, charge.source, charge.ref,
                 1 if backfilled else 0, recorded_at or now_iso(), job_id)


async def record_charge(db_path: str, job_id: str, charge: Charge) -> bool:
    """Store a provider-reported charge on the job row. True when the row changed."""
    sql, params = charge_update(charge, job_id)
    async with aiosqlite.connect(db_path, uri=True) as db:
        await ensure_cost_schema(db)
        cur = await db.execute(sql, params)
        await db.commit()
        return cur.rowcount == 1


async def record_status_charge(db_path: str, job_id: str, raw: dict[str, Any] | None) -> Charge | None:
    """Best effort: store the charge a completed Higgsfield status reports. Never raises (the job
    already completed; bookkeeping must not fail the poll)."""
    try:
        charge = higgsfield_status_charge(raw)
        if charge is not None:
            await record_charge(db_path, job_id, charge)
        return charge
    except Exception as e:  # noqa: BLE001 - recording the actual is informational
        logger.warning(f"could not record the Higgsfield charge for {job_id}: {e}")
        return None


async def set_job_estimate(db_path: str, job_id: str, estimate_usd_micros: int, source: str) -> bool:
    """Record the pre-call estimate of a job (kept when the commit later stores the actual)."""
    async with aiosqlite.connect(db_path, uri=True) as db:
        await ensure_cost_schema(db)
        cur = await db.execute(
            "UPDATE job_ledger SET estimate_usd_micros = ?, estimate_source = ? WHERE job_id = ?",
            (int(estimate_usd_micros), source, job_id))
        await db.commit()
        return cur.rowcount == 1


_NEEDS_BACKFILL_SQL = """
    SELECT
      EXISTS(SELECT 1 FROM job_ledger WHERE estimate_source IS NULL),
      EXISTS(SELECT 1 FROM job_ledger WHERE actual_usd_micros IS NULL AND actual_source IS NULL
             AND cost_source = 'provider_header' AND credits IS NOT NULL AND status = 'committed'),
      EXISTS(SELECT 1 FROM job_ledger WHERE actual_usd_micros IS NOT NULL AND actual_source IS NULL),
      EXISTS(SELECT 1 FROM job_ledger WHERE actual_usd_micros IS NULL AND actual_unit = 'credits'
             AND actual_units IS NOT NULL AND line_name NOT LIKE 'el|_%' ESCAPE '|')
"""


async def backfill_job_costs(db: aiosqlite.Connection) -> dict[str, int]:
    """Fill estimates and actuals for rows written before this tracking existed (idempotent; a
    read-only check first, so the common case takes no write lock). The caller commits.

    - estimate: Higgsfield rows hold the provider estimate as usd_micros (reserved = estimate); an
      ElevenLabs row committed without a header committed its estimate. Other ElevenLabs rows
      (the header replaced the number, or only the upper-bound reservation is known) are marked
      not_recorded.
    - actual: ElevenLabs rows committed from the header kept the credits; their actual is those
      credits at ELEVENLABS_USD_PER_CREDIT, labelled backfilled.
    - actuals written by hfvg.cost_tracking.CostTracker.record_actual_cost (the failed-job zero,
      older header rows) get a source label: cost_tracker_<cost_source>.
    - Higgsfield charges reported in credits get their dollar value once a rate is configured.
    """
    counts = {"estimates": 0, "elevenlabs_actuals": 0, "tracker_labels": 0, "repriced": 0}
    async with db.execute("PRAGMA table_info(job_ledger)") as cur:
        cols = {r[1] async for r in cur}
    needed = {"estimate_source", "actual_source", "cost_source", "credits", "actual_unit"}
    if not needed <= cols:
        return counts
    async with db.execute(_NEEDS_BACKFILL_SQL) as cur:
        need_estimates, need_el, need_labels, need_reprice = await cur.fetchone()
    if not (need_estimates or need_el or need_labels or need_reprice):
        return counts

    if need_estimates:
        cur = await db.execute(
            "UPDATE job_ledger SET estimate_usd_micros = usd_micros, estimate_source = ? "
            "WHERE estimate_source IS NULL AND (line_name NOT LIKE 'el|_%' ESCAPE '|' "
            "OR COALESCE(cost_source, '') = 'estimate')", (ESTIMATE_BACKFILL,))
        counts["estimates"] = cur.rowcount
        await db.execute("UPDATE job_ledger SET estimate_source = ? WHERE estimate_source IS NULL",
                         (ESTIMATE_NOT_RECORDED,))

    if need_el:
        async with db.execute(
            "SELECT job_id, credits, updated_at FROM job_ledger WHERE actual_usd_micros IS NULL "
            "AND actual_source IS NULL AND cost_source = 'provider_header' AND credits IS NOT NULL "
            "AND status = 'committed'"
        ) as cur:
            rows = await cur.fetchall()
        for job_id, credits, updated_at in rows:
            try:
                charge = elevenlabs_charge(float(credits))
            except ValueError as e:  # bad ELEVENLABS_USD_PER_CREDIT: leave for a later pass
                logger.warning(f"ElevenLabs backfill skipped: {e}")
                break
            sql, params = charge_update(charge, job_id, backfilled=True, recorded_at=updated_at)
            await db.execute(sql, params)
            counts["elevenlabs_actuals"] += 1

    if need_labels:
        cur = await db.execute(
            "UPDATE job_ledger SET actual_source = 'cost_tracker_' || COALESCE(cost_source, 'unknown'), "
            "actual_recorded_at = COALESCE(actual_recorded_at, updated_at) "
            "WHERE actual_usd_micros IS NOT NULL AND actual_source IS NULL")
        counts["tracker_labels"] = cur.rowcount

    if need_reprice:
        rate, _problem = higgsfield_rate()
        if rate is not None:
            async with db.execute(
                "SELECT job_id, actual_units FROM job_ledger WHERE actual_usd_micros IS NULL "
                "AND actual_unit = 'credits' AND actual_units IS NOT NULL "
                "AND line_name NOT LIKE 'el|_%' ESCAPE '|'"
            ) as cur:
                rows = await cur.fetchall()
            for job_id, units in rows:
                usd = usd_decimal_to_micros(Decimal(str(units)) * rate)
                await db.execute(
                    "UPDATE job_ledger SET actual_usd_micros = ?, actual_rate_usd = ? WHERE job_id = ?",
                    (usd, str(rate), job_id))
                counts["repriced"] += 1
    if any(counts.values()):
        logger.info(f"job cost backfill: {counts}")
    return counts


def pct(part: int, whole: int) -> float | None:
    """part/whole as a percentage with one decimal; None when there is nothing to divide."""
    if whole <= 0:
        return None
    value = part * 100 / whole
    return round(value, 1) if math.isfinite(value) else None
