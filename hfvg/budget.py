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
# G-BUD-1: per-review-round caps (80% stop) and max paid retries per shot per round
DEFAULT_ROUND_CAP_USD = "10.00"
DEFAULT_MAX_PAID_RETRIES_PER_SHOT_PER_ROUND = 1

# Review rounds (one per human review gate that can trigger paid redo work)
ROUND_G2_STILLS = "G2_STILLS"   # still retries while parked at G2 (retry_shot / retry_end_still)
ROUND_G3_CLIPS = "G3_CLIPS"     # clip retries after G2 (retry_clip)
ROUND_AUDIO = "AUDIO"           # ElevenLabs VO / SFX / music generation for the delivery (G-AUD-1b)
REVIEW_ROUNDS = (ROUND_G2_STILLS, ROUND_G3_CLIPS, ROUND_AUDIO)

# Importable default constants (micro-dollars)
EPISODE_CAP_USD_MICROS = usd_to_micros(DEFAULT_EPISODE_CAP_USD)

REVISION_LINE = "L7_revision"

# Required budget lines (single source of truth: derived from code that reserves)
REQUIRED_BUDGET_LINES = ["L1_refs", "L2_drafts", "L3_final_stills", "L4_video", REVISION_LINE]

# Default line caps when policy lacks values (must sum to < episode stop threshold)
# These total $44, under $48 stop (80% of $60 cap) with $4 buffer
DEFAULT_LINE_CAPS_USD_MICROS = {
    "L1_refs": 4_000_000,           # $4.00 - reference generation
    "L2_drafts": 8_000_000,          # $8.00 - draft stills
    "L3_final_stills": 16_000_000,   # $16.00 - final stills
    "L4_video": 16_000_000,          # $16.00 - video clips
}

# ElevenLabs audio lines reserved by hfvg.activities.audio; created with these caps when the gate
# policy has no ElevenLabs lines for the episode ($3.00 total: with the $44 above, still under $48 stop)
DEFAULT_AUDIO_LINE_CAPS_USD_MICROS = {
    "el_vo_takes": 1_000_000,        # $1.00 - TTS (eleven_v3 $0.08 / 1k chars)
    "el_sfx": 500_000,               # $0.50 - sound effects ($0.12 / min)
    "el_music_one_take": 1_500_000,  # $1.50 - music ($0.15 / min)
}

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
    round_cap_usd_micros: int = 10_000_000
    max_paid_retries_per_shot_per_round: int = DEFAULT_MAX_PAID_RETRIES_PER_SHOT_PER_ROUND

    @property
    def episode_stop_usd_micros(self) -> int:
        return compute_stop_usd_micros(self.episode_cap_usd_micros, self.stop_percent)

    @property
    def round_stop_usd_micros(self) -> int:
        return compute_stop_usd_micros(self.round_cap_usd_micros, self.stop_percent)


class RoundBudgetError(ValueError):
    """A paid retry is refused by the per-round cap / per-shot retry limit (before any submit)."""


def get_budget_settings() -> BudgetSettings:
    """Read budget settings (env at call time so tests/ops can change them)."""
    return BudgetSettings(
        episode_cap_usd_micros=usd_to_micros(os.getenv("EPISODE_CAP_USD", DEFAULT_EPISODE_CAP_USD)),
        stop_percent=int(os.getenv("EPISODE_STOP_PERCENT", str(DEFAULT_STOP_PERCENT))),
        gc01_headroom_usd_micros=usd_to_micros(
            os.getenv("GC01_HEADROOM_USD", DEFAULT_GC01_HEADROOM_USD)),
        revision_reserve_usd_micros=usd_to_micros(
            os.getenv("REVISION_RESERVE_USD", DEFAULT_REVISION_RESERVE_USD)),
        round_cap_usd_micros=usd_to_micros(os.getenv("ROUND_CAP_USD", DEFAULT_ROUND_CAP_USD)),
        max_paid_retries_per_shot_per_round=int(os.getenv(
            "MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", str(DEFAULT_MAX_PAID_RETRIES_PER_SHOT_PER_ROUND))),
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
        self.policy = load_policy()

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
            
            # G-AUD-COST: Add characters and credits_used columns for ElevenLabs audio tracking
            async with db.execute("PRAGMA table_info(job_ledger)") as cur:
                job_cols = {r[1] async for r in cur}
            if "characters" not in job_cols:
                await db.execute("ALTER TABLE job_ledger ADD COLUMN characters INTEGER")
            if "credits_used" not in job_cols:
                await db.execute("ALTER TABLE job_ledger ADD COLUMN credits_used REAL")
            # G-BUD-1: per-round holds for paid retries (one row per paid job, keyed by job id).
            # status: held -> committed | released. A released (uncharged) hold does not count.
            await db.execute("""
                CREATE TABLE IF NOT EXISTS round_holds (
                    hold_key TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    round_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL,
                    retry_key TEXT,
                    usd_micros INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
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
        """
        Initialize per-line dollar budgets for an episode from the gate policy.
        
        Always creates standard lines that code reserves against (see REQUIRED_BUDGET_LINES).
        Uses policy values when available, otherwise defaults (see DEFAULT_LINE_CAPS_USD_MICROS).
        Default caps total $44, under $48 stop threshold (80% of $60 cap) with $4 buffer.
        """
        await self.init_db()
        settings = get_budget_settings()

        hf_budget = self.policy.get_higgsfield_budget(episode_id)
        policy_lines = hf_budget.get("lines", {})
        usd_caps = self.policy.get_usd_line_caps(episode_id)

        async with aiosqlite.connect(self.db_path, uri=True) as db:
            # Create standard lines (using shared constant)
            for line_name, default_cap_micros in DEFAULT_LINE_CAPS_USD_MICROS.items():
                if line_name in policy_lines:
                    # Use policy value
                    if line_name in usd_caps:
                        cap = usd_to_micros(str(usd_caps[line_name]))
                    else:
                        cap = app_credits_to_usd_micros(policy_lines[line_name])
                else:
                    # Use default
                    cap = default_cap_micros
                
                stop = compute_stop_usd_micros(cap, settings.stop_percent)
                await self._insert_line(db, episode_id, "higgsfield", line_name, cap, stop)
            
            # Create any additional policy lines not in defaults
            for line_name, cap_app_credits in policy_lines.items():
                if line_name not in DEFAULT_LINE_CAPS_USD_MICROS:
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

    async def ensure_audio_line(self, episode_id: str, line_name: str) -> None:
        """Create an ElevenLabs audio line with its default cap if the policy did not (no-op otherwise)."""
        if line_name not in DEFAULT_AUDIO_LINE_CAPS_USD_MICROS:
            raise ValueError(f"{line_name} is not an audio budget line")
        await self.init_db()
        settings = get_budget_settings()
        cap = DEFAULT_AUDIO_LINE_CAPS_USD_MICROS[line_name]
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await self._insert_line(db, episode_id, "elevenlabs", line_name, cap,
                                    compute_stop_usd_micros(cap, settings.stop_percent))
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
                     reason: str = "", characters: int | None = None, 
                     credits_used: float | None = None) -> bool:
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
                    # G-AUD-COST: Update with actual cost and optionally characters/credits
                    if characters is not None or credits_used is not None:
                        await db.execute("""
                            UPDATE job_ledger 
                            SET status = 'committed', usd_micros = ?, characters = ?, 
                                credits_used = ?, updated_at = ?
                            WHERE job_id = ?
                        """, (actual, characters, credits_used, now, job_id))
                    else:
                        await db.execute("""
                            UPDATE job_ledger SET status = 'committed', usd_micros = ?, updated_at = ?
                            WHERE job_id = ?
                        """, (actual, now, job_id))
                    await self._finalize_round_hold(db, job_id, "committed", actual)
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
                    await self._finalize_round_hold(db, job_id, "released")
                await db.commit()
                return release_amount
            except BaseException:
                await db.rollback()
                raise

    # ------------------------------------------------------------------ review rounds (G-BUD-1)

    @staticmethod
    async def _finalize_round_hold(db, hold_key: str, status: str, actual: int | None = None):
        """Commit/release the round hold that shares the job id (no-op when there is none)."""
        try:
            if actual is None:
                await db.execute(
                    "UPDATE round_holds SET status = ?, updated_at = ? WHERE hold_key = ? AND status = 'held'",
                    (status, _now_iso(), hold_key))
            else:
                await db.execute(
                    "UPDATE round_holds SET status = ?, usd_micros = ?, updated_at = ? "
                    "WHERE hold_key = ? AND status = 'held'",
                    (status, actual, _now_iso(), hold_key))
        except aiosqlite.OperationalError:
            pass  # ledger created before round_holds existed (init_db adds it)

    async def authorize_round_spend(self, episode_id: str, round_id: str, shot_id: str,
                                    hold_key: str, amount_usd_micros: int, *,
                                    retry_key: str | None = None, reason: str = "",
                                    dry_check: bool = False) -> dict[str, Any]:
        """Authorize (and hold) one PAID retry job against its review round, BEFORE any submit.

        Atomic (BEGIN IMMEDIATE) and idempotent per ``hold_key`` (the deterministic job id): calling
        it again for the same job never double-counts; a higher amount only re-checks the delta.

        Refuses (RoundBudgetError) when
        - ``retry_key`` is given and the shot already has ``max_paid_retries_per_shot_per_round``
          OTHER paid retries (distinct retry_key, not released) in this round;
        - the round total (held + committed) plus this job would pass the round's 80% stop;
        - the episode total (budget lines, revision reserve excluded) plus this job would pass the
          episode's 80% stop.
        A released (failed, uncharged) hold does not count. ``dry_check=True`` runs every check and
        writes nothing (pre-check before a retry child is started).
        """
        amount = _require_int("amount_usd_micros", amount_usd_micros, positive=True)
        if round_id not in REVIEW_ROUNDS:
            raise ValueError(f"round_id must be one of {REVIEW_ROUNDS}, got {round_id!r}")
        settings = get_budget_settings()
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute("BEGIN IMMEDIATE")
            try:
                async with db.execute(
                    "SELECT usd_micros, status FROM round_holds WHERE hold_key = ?", (hold_key,)
                ) as cur:
                    existing = await cur.fetchone()
                if existing and existing[1] == "committed":
                    await db.rollback()
                    return {"authorized": True, "idempotent": True, "hold_key": hold_key,
                            "usd_micros": existing[0]}
                prior = existing[0] if existing and existing[1] == "held" else 0
                delta = max(0, amount - prior)

                if retry_key is not None:
                    async with db.execute("""
                        SELECT COUNT(DISTINCT retry_key) FROM round_holds
                        WHERE episode_id = ? AND round_id = ? AND shot_id = ? AND status != 'released'
                          AND retry_key IS NOT NULL AND retry_key != ?
                    """, (episode_id, round_id, shot_id, retry_key)) as cur:
                        other_retries = (await cur.fetchone())[0]
                    limit = settings.max_paid_retries_per_shot_per_round
                    if other_retries >= limit:
                        raise RoundBudgetError(
                            f"Round {round_id}: shot {shot_id} already used {other_retries} paid "
                            f"retr{'y' if other_retries == 1 else 'ies'} (max {limit} per shot per round)"
                        )

                async with db.execute("""
                    SELECT COALESCE(SUM(usd_micros), 0) FROM round_holds
                    WHERE episode_id = ? AND round_id = ? AND status != 'released' AND hold_key != ?
                """, (episode_id, round_id, hold_key)) as cur:
                    round_total = (await cur.fetchone())[0]
                if round_total + prior + delta > settings.round_stop_usd_micros:
                    raise RoundBudgetError(
                        f"Round {round_id} would pass its {settings.stop_percent}% stop: "
                        f"{micros_to_usd_str(round_total + prior)} USD held/spent + "
                        f"{micros_to_usd_str(delta)} USD > {micros_to_usd_str(settings.round_stop_usd_micros)} "
                        f"USD (cap {micros_to_usd_str(settings.round_cap_usd_micros)} USD)"
                    )

                async with db.execute("""
                    SELECT COALESCE(SUM(spent_usd_micros + reserved_usd_micros), 0)
                    FROM budget_lines WHERE episode_id = ? AND line_name != ?
                """, (episode_id, REVISION_LINE)) as cur:
                    episode_total = (await cur.fetchone())[0]
                if delta and episode_total + delta > settings.episode_stop_usd_micros:
                    raise RoundBudgetError(
                        f"Episode {episode_id} would pass its {settings.stop_percent}% stop: "
                        f"{micros_to_usd_str(episode_total)} USD + {micros_to_usd_str(delta)} USD > "
                        f"{micros_to_usd_str(settings.episode_stop_usd_micros)} USD"
                    )

                if dry_check:
                    await db.rollback()
                    return {"authorized": True, "dry_check": True, "hold_key": hold_key,
                            "usd_micros": prior + delta,
                            "round_total_usd_micros": round_total + prior + delta,
                            "round_stop_usd_micros": settings.round_stop_usd_micros}
                now = _now_iso()
                if existing:
                    await db.execute("""
                        UPDATE round_holds SET usd_micros = ?, status = 'held', retry_key = ?,
                               reason = ?, updated_at = ?
                        WHERE hold_key = ?
                    """, (prior + delta, retry_key, reason, now, hold_key))
                else:
                    await db.execute("""
                        INSERT INTO round_holds (hold_key, episode_id, round_id, shot_id, retry_key,
                                                 usd_micros, status, reason, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'held', ?, ?, ?)
                    """, (hold_key, episode_id, round_id, shot_id, retry_key, amount, reason, now, now))
                await db.commit()
                return {"authorized": True, "idempotent": bool(existing), "hold_key": hold_key,
                        "usd_micros": prior + delta if existing else amount,
                        "round_total_usd_micros": round_total + (prior + delta if existing else amount),
                        "round_stop_usd_micros": settings.round_stop_usd_micros}
            except BaseException:
                await db.rollback()
                raise

    async def release_round_hold(self, hold_key: str) -> bool:
        """Release a round hold whose job was never submitted (e.g. the retry child failed to start)."""
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            cur = await db.execute(
                "UPDATE round_holds SET status = 'released', updated_at = ? WHERE hold_key = ? AND status = 'held'",
                (_now_iso(), hold_key))
            await db.commit()
            return cur.rowcount == 1

    async def get_round_hold(self, hold_key: str) -> dict[str, Any] | None:
        await self.init_db()
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT episode_id, round_id, shot_id, retry_key, usd_micros, status
                FROM round_holds WHERE hold_key = ?
            """, (hold_key,)) as cur:
                row = await cur.fetchone()
        if not row:
            return None
        return dict(zip(("episode_id", "round_id", "shot_id", "retry_key", "usd_micros", "status"), row))

    async def get_round_status(self, episode_id: str) -> dict[str, Any]:
        """Per-round totals, caps and per-shot paid retry counts (usd_micros)."""
        settings = get_budget_settings()
        await self.init_db()
        rounds: dict[str, Any] = {
            r: {"round_id": r, "total_usd_micros": 0, "cap_usd_micros": settings.round_cap_usd_micros,
                "stop_usd_micros": settings.round_stop_usd_micros, "paid_retries": {}}
            for r in REVIEW_ROUNDS
        }
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute("""
                SELECT round_id, shot_id, retry_key, usd_micros FROM round_holds
                WHERE episode_id = ? AND status != 'released'
            """, (episode_id,)) as cur:
                rows = await cur.fetchall()
        seen: set[tuple] = set()
        for round_id, shot_id, retry_key, usd in rows:
            r = rounds.setdefault(round_id, {"round_id": round_id, "total_usd_micros": 0,
                                             "cap_usd_micros": settings.round_cap_usd_micros,
                                             "stop_usd_micros": settings.round_stop_usd_micros,
                                             "paid_retries": {}})
            r["total_usd_micros"] += usd
            if retry_key is not None and (round_id, shot_id, retry_key) not in seen:
                seen.add((round_id, shot_id, retry_key))
                r["paid_retries"][shot_id] = r["paid_retries"].get(shot_id, 0) + 1
        for r in rounds.values():
            r["at_stop"] = r["total_usd_micros"] >= r["stop_usd_micros"]
        return {"episode_id": episode_id, "unit": "usd_micros",
                "max_paid_retries_per_shot_per_round": settings.max_paid_retries_per_shot_per_round,
                "rounds": rounds}

    # ------------------------------------------------------------------ per-job ledger

    async def _job_finalized(self, db, job_id: str) -> bool:
        async with db.execute("SELECT status FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
        return bool(row and row[0] in JOB_TERMINAL_STATUSES)

    async def record_job(self, episode_id: str, job_id: str, shot_id: str | None, model: str,
                         tier: str, line_name: str, usd_micros: int, 
                         characters: int | None = None, credits_used: float | None = None) -> bool:
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
                 characters, credits_used, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?)
            """, (job_id, episode_id, shot_id, model, tier, line_name, usd_micros, 
                  characters, credits_used, now, now))
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
