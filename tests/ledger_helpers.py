"""Shared helpers for dollar-ledger tests (synthetic data only; all amounts are int usd_micros)."""

from hfvg.budget import BudgetLedger, compute_stop_usd_micros


async def insert_line(db, episode_id: str, line_name: str, cap_usd_micros: int,
                      stop_usd_micros: int | None = None, provider: str = "higgsfield"):
    """Insert/replace a budget line on an OPEN aiosqlite connection (tables must exist)."""
    if stop_usd_micros is None:
        stop_usd_micros = compute_stop_usd_micros(cap_usd_micros)
    await db.execute("""
        INSERT OR REPLACE INTO budget_lines
        (line_id, episode_id, provider, line_name, cap_usd_micros, stop_usd_micros,
         spent_usd_micros, reserved_usd_micros, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, 0, datetime('now'))
    """, (f"{episode_id}:{line_name}", episode_id, provider, line_name,
          cap_usd_micros, stop_usd_micros))


async def set_balance(db_path: str, usd: str = "100.00", note: str = "test balance"):
    """Record a manual GC.01 balance (required before any live paid job)."""
    from hfvg.pricing import usd_to_micros
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.set_manual_balance(usd_to_micros(usd), "test", note)


async def expect_value_error(awaitable, contains: str) -> str:
    """Await `awaitable`, require ValueError whose text contains `contains`.

    Raises a real AssertionError (not pytest's "DID NOT RAISE") when the call succeeds or the
    message differs, so a surviving mutation is classified as an assertion failure.
    """
    try:
        await awaitable
    except ValueError as e:
        assert contains in str(e), f"expected {contains!r} in ValueError text, got: {e}"
        return str(e)
    raise AssertionError(f"expected ValueError containing {contains!r}, but the call succeeded")
