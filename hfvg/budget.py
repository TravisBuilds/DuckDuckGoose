"""Dollar budget ledger: per-line caps, 80% stop, GC.01 headroom, revision reserve.

Currency: **integer micro-dollars** (``usd_micros``; 1 USD = 1_000_000). No floats anywhere in the
ledger: caps, stops, reserves, commits, releases and the per-job table are all ``int``.

Defaults (override with env, see ``get_budget_settings``):
- Episode cap 60.00 USD (60_000_000), stop at 80% (48_000_000, integer arithmetic): over the cap a
  reserve raises; past the stop it returns False. Each line also has its own cap and 80% stop.
- GC.01 headroom 5.00 USD: a new paid job is refused when
  ``manual_balance - committed_since_entry - reserved < headroom`` (after adding the new hold).
  The manual balance is entered by an admin (Higgsfield has no balance endpoint).
- Separate revision reserve line ``L7_revision`` (10.00 USD), outside the episode cap, usable only
  when the caller passes ``revision=True`` (the approval tag ``revision``).

Reserve / commit / release are atomic (BEGIN IMMEDIATE), idempotent per job id, and never drive a
reservation negative.

Legacy: CREDIT-PLAN.md / HARNESS-GATES line caps are authored in Higgsfield *app credits*. They are
converted once, at import time of a plan, with ``LEGACY_APP_CREDIT_USD_MICROS`` (0.0475 USD per app
credit, from HARNESS-GATES.json `usd.basis`). Credits are never a cap currency after that.
"""

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Any

import aiosqlite

from hfvg.config import config
from hfvg.gates import load_policy
from hfvg.pricing import MICROS_PER_USD, micros_to_usd_str, usd_to_micros

# Documented defaults (env-overridable through get_budget_settings)
DEFAULT_EPISODE_CAP_USD = "60.00"
DEFAULT_STOP_PERCENT = 80
DEFAULT_GC01_HEADROOM_USD = "5.00"
DEFAULT_REVISION_RESERVE_USD = "10.00"

# Importable default constants (micro-dollars)
EPISODE_CAP_USD_MICROS = usd_to_micros(DEFAULT_EPISODE_CAP_USD)

REVISION_LINE = "L7_revision"

# Legacy plan import only: 1 Higgsfield app credit = 0.0475 USD (HARNESS-GATES.json usd.basis)
LEGACY_APP_CREDIT_USD_MICROS = 47_500
# ElevenLabs credits ~ 0.0002 USD each (HARNESS-GATES.json usd.basis)
ELEVENLABS_CREDIT_USD_MICROS = 200

JOB_TERMINAL_STATUSES = ("committed", "released")


@dataclass(frozen=True)
class BudgetSettings:
    episode_cap_usd_micros: int
    stop_percent: int
    gc01_headroom_usd_micros: int
    revision_reserve_usd_micros: int

    @property
    def episode_stop_usd_micros(self) -> int:
        return compute_stop_usd_micros(self.episode_cap_usd_micros, self.stop_percent)


def get_budget_settings() -> BudgetSettings:
    """Read budget settings (env at call time so tests/ops can change them)."""
    return BudgetSettings(
        episode_cap_usd_micros=usd_to_micros(os.getenv("EPISODE_CAP_USD", DEFAULT_EPISODE_CAP_USD)),
        stop_percent=int(os.getenv("EPISODE_STOP_PERCENT", str(DEFAULT_STOP_PERCENT))),
        gc01_headroom_usd_micros=usd_to_micros(
            os.getenv("GC01_HEADROOM_USD", DEFAULT_GC01_HEADROOM_USD)),
        revision_reserve_usd_micros=usd_to_micros(
            os.getenv("REVISION_RESERVE_USD", DEFAULT_REVISION_RESERVE_USD)),
    )


def compute_stop_usd_micros(cap_usd_micros: int, stop_percent: int = DEFAULT_STOP_PERCENT) -> int:
    """Stop threshold = floor(cap * percent / 100) in pure integer arithmetic (no float drift)."""
    return cap_usd_micros * stop_percent // 100


def _require_int(name: str, value: Any, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(
            f"{name} must be an int number of usd_micros, got {type(value).__name__} ({value!r}); "
            "convert with hfvg.pricing.usd_to_micros"
        )
    if positive and value <= 0:
        raise ValueError(f"{name} must be > 0 usd_micros, got {value}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0 usd_micros, got {value}")
    return value


def app_credits_to_usd_micros(app_credits: Any) -> int:
    """Legacy plan import: Higgsfield app credits -> usd_micros (rounds up)."""
    dec = Decimal(str(app_credits)) * LEGACY_APP_CREDIT_USD_MICROS
    return int(dec.to_integral_value(rounding=ROUND_CEILING))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BudgetLedger:
    """Per-line dollar ledger (integer micro-dollars) with 80% stop and GC.01 headroom."""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or config.DB_PATH
        self.policy = load_policy("mid-mountain-rest")

    # ------------------------------------------------------------------ schema

    async def init_db(self):
        """Create (and migrate to) the dollar-ledger tables."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Migrate a legacy credit-unit ledger out of the way (kept for audit, never read)
            async with db.execute("PRAGMA table_info(budget_lines)") as cur:
                cols = [r[1] async for r in cur]
            if cols and "cap_usd_micros" not in cols:
                await db.execute("ALTER TABLE budget_lines RENAME TO budget_lines_legacy_credits")
                async with db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='budget_transactions'"
                ) as cur:
                    if await cur.fetchone():
                        await db.execute(
                            "ALTER TABLE budget_transactions RENAME TO budget_transactions_legacy_credits")

            await db.execute("""
                CREATE TABLE IF NOT EXISTS budget_lines (
                    line_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    line_name TEXT NOT NULL,
                    cap_usd_micros INTEGER NOT NULL,
                    stop_usd_micros INTEGER NOT NULL,
                    spent_usd_micros INTEGER NOT NULL DEFAULT 0,
                    reserved_usd_micros INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT
                )
            """)
            # amount: integer micro-dollars
            await db.execute("""
                CREATE TABLE IF NOT EXISTS budget_transactions (
                    txn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    line_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    job_id TEXT,
                    txn_type TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    timestamp TEXT NOT NULL,
                    reason TEXT,
                    FOREIGN KEY (line_id) REFERENCES budget_lines(line_id)
                )
            """)
            # Per-job ledger: one row per provider job
            await db.execute("""
                CREATE TABLE IF NOT EXISTS job_ledger (
                    job_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    shot_id TEXT,
                    model TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    line_name TEXT NOT NULL,
                    usd_micros INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    verdict TEXT,
                    used INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            # Manual balance entries (append-only history; latest row is current)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS manual_balance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    balance_usd_micros INTEGER NOT NULL,
                    set_at TEXT NOT NULL,
                    set_by TEXT NOT NULL,
                    note TEXT,
                    after_txn_id INTEGER NOT NULL
                )
            """)
            await db.commit()

    # ------------------------------------------------------------------ line setup

    async def _insert_line(self, db, episode_id: str, provider: str, line_name: str,
                           cap: int, stop: int, replace: bool = False):
        verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
        await db.execute(f"""
            {verb} INTO budget_lines
            (line_id, episode_id, provider, line_name, cap_usd_micros, stop_usd_micros,
             spent_usd_micros, reserved_usd_micros, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)
        """, (f"{episode_id}:{line_name}", episode_id, provider, line_name, cap, stop, _now_iso()))

    async def _insert_revision_line(self, db, episode_id: str):
        settings = get_budget_settings()
        cap = settings.revision_reserve_usd_micros
        # The revision reserve is a hard reserve: usable up to its cap (stop == cap)
        await self._insert_line(db, episode_id, "higgsfield", REVISION_LINE, cap, cap)

    async def init_episode_budget(self, episode_id: str):
        """Initialize per-line dollar budgets for an episode from the gate policy."""
        await self.init_db()
        settings = get_budget_settings()

        hf_budget = self.policy.get_higgsfield_budget(episode_id)
        lines = hf_budget.get("lines", {})
        usd_caps = self.policy.get_usd_line_caps(episode_id)

        async with aiosqlite.connect(self.db_path, uri=True) as db:
            for line_name, cap_app_credits in lines.items():
                if line_name in usd_caps:
                    cap = usd_to_micros(str(usd_caps[line_name]))
                else:
                    cap = app_credits_to_usd_micros(cap_app_credits)
                stop = compute_stop_usd_micros(cap, settings.stop_percent)
                await self._insert_line(db, episode_id, "higgsfield", line_name, cap, stop)

            await self._insert_revision_line(db, episode_id)

            # ElevenLabs lines (credits converted to USD at ~0.0002 USD/credit)
            el_budget = self.policy.get_elevenlabs_budget(episode_id)
            for line_name, el_credits in el_budget.get("lines", {}).items():
                cap = int(Decimal(str(el_credits)) * ELEVENLABS_CREDIT_USD_MICROS)
                stop = compute_stop_usd_micros(cap, settings.stop_percent)
                await self._insert_line(db, episode_id, "elevenlabs", f"el_{line_name}", cap, stop)

            await db.commit()

    async def init_episode_budget_from_plan(self, episode_id: str, credit_plan: dict):
        """
        Initialize budget from a parsed credit plan (authored in Higgsfield app credits).

        Each line cap/stop is converted ONCE to usd_micros (legacy 0.0475 USD/app credit).

        Raises:
            ValueError: If any stop > cap.
        """
        await self.init_db()
        for line_name, line_data in credit_plan["lines"].items():
            if line_data["stop"] > line_data["cap"]:
                raise ValueError(
                    f"Invalid credit plan for {line_name}: stop_threshold ({line_data['stop']}) "
                    f"must be <= budget_cap ({line_data['cap']}) (app credits)"
                )
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            for line_name, line_data in credit_plan["lines"].items():
                cap = app_credits_to_usd_micros(line_data["cap"])
                stop = app_credits_to_usd_micros(line_data["stop"])
                await self._insert_line(db, episode_id, "higgsfield", line_name, cap, stop)
            await self._insert_revision_line(db, episode_id)
            await db.commit()

    async def set_line(
        self, episode_id: str, line_name: str, cap_usd_micros: int,
        stop_usd_micros: int | None = None, provider: str = "higgsfield",
    ):
        """
        Set a budget line directly (admin/tests). Stop defaults to 80% of the cap (integer math).
        Replaces an existing line and resets its spent/reserved to 0.
        """
        _require_int("cap_usd_micros", cap_usd_micros)
        if stop_usd_micros is None:
            stop_usd_micros = compute_stop_usd_micros(
                cap_usd_micros, get_budget_settings().stop_percent)
        _require_int("stop_usd_micros", stop_usd_micros)
        if stop_usd_micros > cap_usd_micros:
            raise ValueError("stop_usd_micros must be <= cap_usd_micros")
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await self._insert_line(db, episode_id, provider, line_name,
                                    cap_usd_micros, stop_usd_micros, replace=True)
            await db.commit()

    # ------------------------------------------------------------------ manual balance (GC.01)

    async def set_manual_balance(self, balance_usd_micros: int, set_by: str, note: str = "") -> dict:
        """Record the manually entered Higgsfield USD balance (no balance endpoint exists)."""
        _require_int("balance_usd_micros", balance_usd_micros)
        await self.init_db()
        set_at = _now_iso()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute("SELECT COALESCE(MAX(txn_id), 0) FROM budget_transactions") as cur:
                after_txn_id = (await cur.fetchone())[0]
            await db.execute("""
                INSERT INTO manual_balance (balance_usd_micros, set_at, set_by, note, after_txn_id)
                VALUES (?, ?, ?, ?, ?)
            """, (balance_usd_micros, set_at, set_by, note, after_txn_id))
            await db.commit()
        return {"balance_usd_micros": balance_usd_micros, "set_at": set_at, "set_by": set_by,
                "note": note}

    async def get_balance_status(self) -> dict[str, Any]:
        """GC.01 status: latest manual balance, holds, headroom and what is left above it."""
        await self.init_db()
        settings = get_budget_settings()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            return await self._balance_status(db, settings)

    async def _balance_status(self, db, settings: BudgetSettings) -> dict[str, Any]:
        async with db.execute("""
            SELECT balance_usd_micros, set_at, set_by, note, after_txn_id
            FROM manual_balance ORDER BY id DESC LIMIT 1
        """) as cur:
            row = await cur.fetchone()
        async with db.execute(
            "SELECT COALESCE(SUM(reserved_usd_micros), 0) FROM budget_lines"
        ) as cur:
            reserved_total = (await cur.fetchone())[0]
        status: dict[str, Any] = {
            "manual_balance_usd_micros": None,
            "set_at": None,
            "set_by": None,
            "reserved_usd_micros": reserved_total,
            "spent_since_entry_usd_micros": 0,
            "gc01_headroom_usd_micros": settings.gc01_headroom_usd_micros,
            "balance_remaining_usd_micros": None,
        }
        if row is None:
            return status
        balance, set_at, set_by, note, after_txn_id = row
        async with db.execute("""
            SELECT COALESCE(SUM(amount), 0) FROM budget_transactions
            WHERE txn_type = 'commit' AND txn_id > ?
        """, (after_txn_id,)) as cur:
            spent_since = (await cur.fetchone())[0]
        status.update({
            "manual_balance_usd_micros": balance,
            "set_at": set_at,
            "set_by": set_by,
            "note": note,
            "spent_since_entry_usd_micros": spent_since,
            "balance_remaining_usd_micros": balance - spent_since - reserved_total,
        })
        return status

    # ------------------------------------------------------------------ reserve / commit / release

    async def reserve(self, episode_id: str, line_name: str, amount_usd_micros: int,
                      reason: str = "", *, job_id: str | None = None, revision: bool = False,
                      require_balance: bool = False) -> bool:
        """
        Reserve dollars from a line before generation (atomic).

        Args:
            amount_usd_micros: int micro-dollars (> 0)
            revision: the approval tag ``revision``; REQUIRED to draw on the revision reserve
            require_balance: live preflight - refuse if no manual balance has been recorded

        Returns:
            True if reserved, False if it would pass the line's stop threshold.

        Raises:
            ValueError: unknown line, revision tag missing, episode cap / line cap exceeded,
                        GC.01 headroom violated (or no manual balance when required).
        """
        amount = _require_int("amount_usd_micros", amount_usd_micros, positive=True)
        line_id = f"{episode_id}:{line_name}"
        settings = get_budget_settings()

        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute("""
                    SELECT spent_usd_micros, reserved_usd_micros, stop_usd_micros, cap_usd_micros
                    FROM budget_lines WHERE line_id = ?
                """, (line_id,)) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Budget line {line_id} not found")
                spent, reserved, stop, cap = row

                # Revision reserve: only with the approval tag `revision`
                if line_name == REVISION_LINE and not revision:
                    raise ValueError(
                        f"{REVISION_LINE} is the revision reserve: it can only be used with "
                        "the approval tag 'revision'"
                    )

                # Episode cap (revision reserve is outside the episode cap)
                episode_total = 0
                if line_name != REVISION_LINE:
                    async with db.execute("""
                        SELECT COALESCE(SUM(spent_usd_micros + reserved_usd_micros), 0)
                        FROM budget_lines WHERE episode_id = ? AND line_name != ?
                    """, (episode_id, REVISION_LINE)) as cursor:
                        episode_total = (await cursor.fetchone())[0]
                    if episode_total + amount > settings.episode_cap_usd_micros:
                        raise ValueError(
                            f"Episode {episode_id} would exceed episode cap. "
                            f"Current: {micros_to_usd_str(episode_total)} USD, "
                            f"requested: {micros_to_usd_str(amount)} USD, "
                            f"cap: {micros_to_usd_str(settings.episode_cap_usd_micros)} USD"
                        )

                # Episode stop (80% of the episode cap): at/over it the next reserve is refused
                if line_name != REVISION_LINE and episode_total + amount > settings.episode_stop_usd_micros:
                    await db.rollback()
                    return False

                # Hard cap (line level)
                if spent + reserved + amount > cap:
                    raise ValueError(
                        f"Budget cap exceeded: {micros_to_usd_str(spent + reserved + amount)} USD > "
                        f"{micros_to_usd_str(cap)} USD for {line_id}"
                    )

                # GC.01 headroom against the manually entered balance
                balance = await self._balance_status(db, settings)
                if balance["manual_balance_usd_micros"] is None:
                    if require_balance:
                        raise ValueError(
                            "GC.01: no manual Higgsfield balance recorded; set it "
                            "(POST /api/budget/balance) before any paid job"
                        )
                else:
                    balance_after_hold = balance["balance_remaining_usd_micros"] - amount
                    if balance_after_hold < settings.gc01_headroom_usd_micros:
                        raise ValueError(
                            f"GC.01 headroom: balance {micros_to_usd_str(balance['manual_balance_usd_micros'])} "
                            f"USD minus holds/spend would leave "
                            f"{micros_to_usd_str(balance_after_hold)} USD, below the "
                            f"{micros_to_usd_str(settings.gc01_headroom_usd_micros)} USD headroom"
                        )

                # Atomic conditional UPDATE: single source of truth for the stop threshold
                cursor = await db.execute("""
                    UPDATE budget_lines
                    SET reserved_usd_micros = reserved_usd_micros + ?
                    WHERE line_id = ?
                      AND (spent_usd_micros + reserved_usd_micros + ?) <= stop_usd_micros
                """, (amount, line_id, amount))
                if cursor.rowcount == 0:
                    await db.rollback()
                    return False

                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, ?, 'reserve', ?, ?, ?)
                """, (line_id, episode_id, job_id, amount, _now_iso(), reason))
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def commit(self, episode_id: str, line_name: str, reserved_usd_micros: int,
                     actual_usd_micros: int | None = None, job_id: str | None = None,
                     reason: str = "") -> bool:
        """
        Commit actual spend: release the hold, add the ACTUAL cost to spent.

        actual > reserved: spent grows by the actual cost (never silently capped at the hold)
        and an overage warning is logged. actual < reserved: only the actual is spent.
        The hold released is clamped to what is really reserved, so it never goes negative.

        Idempotent per job id: a second commit/release for a job already finalized is a no-op.

        Returns:
            True if applied, False if the job was already finalized (no-op).
        """
        reserved_amount = _require_int("reserved_usd_micros", reserved_usd_micros)
        if actual_usd_micros is None:
            actual_usd_micros = reserved_amount
        actual = _require_int("actual_usd_micros", actual_usd_micros)
        line_id = f"{episode_id}:{line_name}"

        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if job_id and await self._job_finalized(db, job_id):
                    await db.rollback()
                    return False

                async with db.execute(
                    "SELECT reserved_usd_micros FROM budget_lines WHERE line_id = ?", (line_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Budget line {line_id} not found")
                hold_released = min(reserved_amount, row[0])
                spent_amount = actual

                await db.execute("""
                    UPDATE budget_lines
                    SET reserved_usd_micros = reserved_usd_micros - ?,
                        spent_usd_micros = spent_usd_micros + ?
                    WHERE line_id = ?
                """, (hold_released, spent_amount, line_id))

                now = _now_iso()
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, ?, 'commit', ?, ?, ?)
                """, (line_id, episode_id, job_id, actual, now, reason))

                if actual < reserved_amount:
                    await db.execute("""
                        INSERT INTO budget_transactions
                        (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                        VALUES (?, ?, ?, 'release', ?, ?, ?)
                    """, (line_id, episode_id, job_id, reserved_amount - actual, now,
                          f"Unused from reserve: {reason}"))
                elif actual > reserved_amount:
                    overage = actual - reserved_amount
                    await db.execute("""
                        INSERT INTO budget_transactions
                        (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                        VALUES (?, ?, ?, 'overage_warning', ?, ?, ?)
                    """, (line_id, episode_id, job_id, overage, now,
                          f"Cost exceeded reserve by {micros_to_usd_str(overage)} USD: {reason}"))

                if job_id:
                    await db.execute("""
                        UPDATE job_ledger SET status = 'committed', usd_micros = ?, updated_at = ?
                        WHERE job_id = ?
                    """, (actual, now, job_id))
                await db.commit()
                return True
            except BaseException:
                await db.rollback()
                raise

    async def release(self, episode_id: str, line_name: str, amount_usd_micros: int,
                      reason: str = "", job_id: str | None = None) -> int:
        """
        Release a hold without spending (block, failure, refund).

        Atomic and idempotent: releases at most what is actually reserved (never negative);
        a job already committed/released is a no-op.

        Returns:
            usd_micros actually released.
        """
        amount = _require_int("amount_usd_micros", amount_usd_micros)
        if amount == 0:
            return 0
        line_id = f"{episode_id}:{line_name}"

        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                if job_id and await self._job_finalized(db, job_id):
                    await db.rollback()
                    return 0
                async with db.execute(
                    "SELECT reserved_usd_micros FROM budget_lines WHERE line_id = ?", (line_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError(f"Budget line {line_id} not found")
                release_amount = min(amount, row[0])

                await db.execute("""
                    UPDATE budget_lines
                    SET reserved_usd_micros = reserved_usd_micros - ?
                    WHERE line_id = ?
                """, (release_amount, line_id))

                note = reason if release_amount == amount else (
                    f"Partial: requested {amount}, released {release_amount} (all available). {reason}")
                now = _now_iso()
                await db.execute("""
                    INSERT INTO budget_transactions
                    (line_id, episode_id, job_id, txn_type, amount, timestamp, reason)
                    VALUES (?, ?, ?, 'release', ?, ?, ?)
                """, (line_id, episode_id, job_id, release_amount, now, note))
                if job_id:
                    await db.execute("""
                        UPDATE job_ledger SET status = 'released', updated_at = ? WHERE job_id = ?
                    """, (now, job_id))
                await db.commit()
                return release_amount
            except BaseException:
                await db.rollback()
                raise

    # ------------------------------------------------------------------ per-job ledger

    async def _job_finalized(self, db, job_id: str) -> bool:
        async with db.execute("SELECT status FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return bool(row and row[0] in JOB_TERMINAL_STATUSES)

    async def record_job(self, episode_id: str, job_id: str, shot_id: str | None, model: str,
                         tier: str, line_name: str, usd_micros: int) -> bool:
        """Insert the per-job ledger row once the provider returned a job id (idempotent)."""
        if tier not in ("draft", "final", "retry", "revision"):
            raise ValueError(f"tier must be draft/final/retry/revision, got {tier!r}")
        _require_int("usd_micros", usd_micros, positive=True)
        await self.init_db()
        now = _now_iso()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            cursor = await db.execute("""
                INSERT OR IGNORE INTO job_ledger
                (job_id, episode_id, shot_id, model, tier, line_name, usd_micros, status,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?)
            """, (job_id, episode_id, shot_id, model, tier, line_name, usd_micros, now, now))
            await db.commit()
            return cursor.rowcount == 1

    async def mark_job_status(self, job_id: str, status: str):
        """Set a non-terminal job status such as 'pending_reconcile' (never overrides terminal)."""
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("""
                UPDATE job_ledger SET status = ?, updated_at = ?
                WHERE job_id = ? AND status NOT IN ('committed', 'released')
            """, (status, _now_iso(), job_id))
            await db.commit()

    async def set_job_verdict(self, job_id: str, verdict: str, used: bool | None = None) -> bool:
        """Record the QC/human verdict and whether the asset was used in the cut."""
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            cursor = await db.execute("""
                UPDATE job_ledger SET verdict = ?, used = ?, updated_at = ? WHERE job_id = ?
            """, (verdict, None if used is None else int(used), _now_iso(), job_id))
            await db.commit()
            return cursor.rowcount == 1

    async def get_jobs(self, episode_id: str) -> list[dict[str, Any]]:
        """Per-job ledger rows for an episode (oldest first)."""
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT job_id, shot_id, model, tier, line_name, usd_micros, status, verdict, used,
                       created_at, updated_at
                FROM job_ledger WHERE episode_id = ? ORDER BY created_at, rowid
            """, (episode_id,)) as cursor:
                rows = await cursor.fetchall()
        jobs = []
        for (job_id, shot_id, model, tier, line_name, usd, status, verdict, used,
             created_at, updated_at) in rows:
            jobs.append({
                "job_id": job_id, "shot_id": shot_id, "model": model, "tier": tier,
                "line_name": line_name, "usd_micros": usd, "usd": micros_to_usd_str(usd),
                "status": status, "verdict": verdict,
                "used": "unused" if used == 0 else ("used" if used == 1 else None),
                "created_at": created_at, "updated_at": updated_at,
            })
        return jobs

    # ------------------------------------------------------------------ status

    async def get_line_status(self, episode_id: str, line_name: str) -> dict[str, Any]:
        """Status of a budget line (all amounts int usd_micros)."""
        line_id = f"{episode_id}:{line_name}"
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT spent_usd_micros, reserved_usd_micros, cap_usd_micros, stop_usd_micros,
                       provider
                FROM budget_lines WHERE line_id = ?
            """, (line_id,)) as cursor:
                row = await cursor.fetchone()
        if not row:
            raise ValueError(f"Budget line {line_id} not found")
        spent, reserved, cap, stop, provider = row
        total_committed = spent + reserved
        available = stop - total_committed
        at_stop = total_committed >= stop
        return {
            "line_id": line_id,
            "provider": provider,
            "spent": spent,
            "reserved": reserved,
            "total_committed": total_committed,
            "budget_cap": cap,
            "stop_threshold": stop,
            "available": available,
            "at_stop": at_stop,
            "unit": "usd_micros",
        }

    async def get_episode_summary(self, episode_id: str) -> dict[str, Any]:
        """Budget summary for an episode (all amounts int usd_micros)."""
        await self.init_db()
        settings = get_budget_settings()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT line_id, provider, line_name, spent_usd_micros, reserved_usd_micros,
                       cap_usd_micros, stop_usd_micros
                FROM budget_lines WHERE episode_id = ?
            """, (episode_id,)) as cursor:
                rows = await cursor.fetchall()
            balance = await self._balance_status(db, settings)

        lines = []
        hf_total = el_total = episode_total = 0
        for _line_id, provider, name, spent, reserved, cap, stop in rows:
            total = spent + reserved
            lines.append({
                "line_name": name, "provider": provider, "spent": spent, "reserved": reserved,
                "total": total, "cap": cap, "stop": stop, "at_stop": total >= stop,
                "unit": "usd_micros", "revision_reserve": name == REVISION_LINE,
            })
            if name != REVISION_LINE:
                episode_total += total
            if provider == "higgsfield":
                hf_total += total
            elif provider == "elevenlabs":
                el_total += total

        return {
            "episode_id": episode_id,
            "unit": "usd_micros",
            "lines": lines,
            "higgsfield_total": hf_total,
            "elevenlabs_total": el_total,
            "episode_total": episode_total,
            "episode_cap": settings.episode_cap_usd_micros,
            "episode_stop": settings.episode_stop_usd_micros,
            "episode_at_stop": episode_total >= settings.episode_stop_usd_micros,
            "gc01_headroom": settings.gc01_headroom_usd_micros,
            "balance": balance,
        }
