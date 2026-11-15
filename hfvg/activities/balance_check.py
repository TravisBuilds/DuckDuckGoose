"""Scheduled balance check (BalanceCheckWorkflow, docs/ACTUAL-COSTS.md).

One pass: read the Higgsfield usage report and match it to jobs (actual costs), read every
provider balance (stored with its timestamp, low / critical / stale alerts fire from the reading),
and alert when the latest Higgsfield entry did not move although the studio spent since the
previous one. Read-only towards the providers: nothing here starts, reserves or charges a job.
"""

from __future__ import annotations

import os

from temporalio import activity


def _db_path() -> str:
    return os.getenv("DATABASE_PATH", "./data/studio.db")


@activity.defn
async def scheduled_balance_check() -> dict:
    from hfvg.balance_monitor import BalanceMonitor, init_balance_tables, manual_balance_consistency
    from hfvg.balance_notifier import BalanceNotifier, alert_config_status, init_notifier_tables
    from hfvg.budget import BudgetLedger
    from hfvg.provider_usage import sync_higgsfield_usage

    db_path = _db_path()
    await BudgetLedger(db_path).init_db()
    await init_balance_tables(db_path)
    await init_notifier_tables(db_path)

    usage = await sync_higgsfield_usage(db_path)
    result = await BalanceMonitor(db_path).check_all_balances(read_for="schedule")
    consistency = await manual_balance_consistency(db_path)
    unchanged_alert = None
    if consistency["unchanged_despite_spend"]:
        unchanged_alert = await BalanceNotifier(db_path).send_unchanged_balance_alert(
            "higgsfield", consistency["balance_usd"], consistency["spent_between_usd"],
            consistency["previous_set_at"], consistency["set_at"])
    alerts = alert_config_status()
    for warning in alerts["warnings"]:
        activity.logger.warning(f"balance alerts: {warning}")
    return {
        "balances": {name: {"balance_usd": f"{b.balance_usd:.2f}" if b.balance_usd is not None else None,
                            "source": b.source} for name, b in result.providers.items()},
        "warnings": result.warnings,
        "errors": result.errors,
        "usage_sync": {k: usage.get(k) for k in ("ok", "skipped", "error", "rows", "mode", "jobs_updated",
                                                 "allocated_buckets", "unmatched_buckets") if k in usage},
        "unchanged_balance": consistency["unchanged_despite_spend"],
        "unchanged_alert": unchanged_alert,
        "alert_channels": alerts["channels"],
        "alert_warnings": alerts["warnings"],
    }
