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
    credits_spent: int,
    credits_cap: int,
    paused_for_cap: bool,
    shots_count: int,
    stages: list[dict],
    status: str = "complete",
    error: str | None = None,
) -> dict:
    """
    Generate run report and save as JSON + markdown.
    
    Returns:
        dict with json_path and md_path
    """
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
    )
    
    # Save to reports directory
    output_dir = Path("./data/reports")
    paths = report.save(output_dir)
    
    activity.logger.info(f"Run report saved: {paths}")
    
    return paths
