"""Activity to generate run report at workflow completion."""

from datetime import datetime
from pathlib import Path
from temporalio import activity

from hfvg.run_report import create_run_report


@activity.defn
async def generate_run_report(
    episode_id: str,
    is_canary: bool,
    start_time_iso: str,
    end_time_iso: str,
    credits_spent: int | None,
    credits_cap: int | None,
    paused_for_cap: bool,
    shots_count: int,
    stages: list[dict],
    status: str = "complete",
    error: str | None = None,
    round_stats: dict | None = None,
) -> dict:
    """
    Generate run report and save as JSON + markdown.

    G-BUD-1: credits_spent / credits_cap of None mean "read the real values from the credit
    ledger" (the workflow no longer passes a placeholder cap). The USD ledger summary and the
    per-round caps/retry counts are always read from hfvg.budget and added to the report.
    Figures that cannot be read are reported as n/a, never invented.
    
    Returns:
        dict with json_path and md_path
    """
    figures = await _ledger_figures(episode_id)
    if credits_spent is None:
        credits_spent = figures["credits_spent"]
    if credits_cap is None:
        credits_cap = figures["credits_cap"]
    rounds = dict(figures["rounds"] or {})
    rounds.update(round_stats or {})
    start_time = datetime.fromisoformat(start_time_iso)
    end_time = datetime.fromisoformat(end_time_iso)
    
    report = create_run_report(
        episode_id=episode_id,
        is_canary=is_canary,
        start_time=start_time,
        end_time=end_time,
        credits_spent=credits_spent,
        credits_cap=credits_cap,
        paused_for_cap=paused_for_cap,
        shots_count=shots_count,
        stages=stages,
        status=status,
        error=error,
        budget_usd=figures["budget_usd"],
        rounds=rounds or None,
    )
    
    # Save to reports directory
    output_dir = Path("./data/reports")
    paths = report.save(output_dir)
    
    activity.logger.info(f"Run report saved: {paths}")
    
    return paths


async def _ledger_figures(episode_id: str) -> dict:
    """Real credit + USD + per-round figures for the report (None where unavailable)."""
    out = {"credits_spent": None, "credits_cap": None, "budget_usd": None, "rounds": None}
    try:
        import os
        from hfvg.credit_ledger import CreditLedger
        ledger = CreditLedger(os.getenv("DATABASE_PATH", "./data/studio.db"))
        await ledger.init_db()
        status = await ledger.get_episode_status(episode_id)
        out["credits_spent"] = status["spent_credits"]
        out["credits_cap"] = status["cap_credits"]
    except Exception as e:  # report what we can; never substitute a made-up number
        activity.logger.warning(f"Run report: credit ledger unavailable: {e}")
    try:
        import os
        from hfvg.budget import BudgetLedger
        budget = BudgetLedger(os.getenv("DATABASE_PATH", "./data/studio.db"))
        summary = await budget.get_episode_summary(episode_id)
        out["budget_usd"] = {
            k: summary[k] for k in ("episode_total", "episode_cap", "episode_stop", "episode_at_stop")
        }
        rounds = await budget.get_round_status(episode_id)
        from hfvg.budget import get_budget_settings
        settings = get_budget_settings()
        out["rounds"] = {
            "round_cap": settings.round_cap_usd_micros,
            "round_stop": settings.round_stop_usd_micros,
            "max_paid_retries_per_shot_per_round": rounds["max_paid_retries_per_shot_per_round"],
            "rounds": {
                name: {"total": r["total_usd_micros"], "at_stop": r["at_stop"],
                       "paid_retries": sum(r["paid_retries"].values()),
                       "paid_retries_by_shot": dict(r["paid_retries"])}
                for name, r in rounds["rounds"].items()
            },
        }
    except Exception as e:
        activity.logger.warning(f"Run report: USD ledger unavailable: {e}")
    return out
