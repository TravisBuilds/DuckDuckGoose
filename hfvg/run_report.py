"""Automatic run report generator: JSON + markdown summaries.

Tracks per-stage metrics:
- Wall-clock time
- Credits spent/reserved
- Generations vs kept
- Rerolls
- Moderation flags
- Retries/timeouts
- Gate wait times
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class StageMetrics:
    """Metrics for one pipeline stage."""
    
    stage_name: str
    wall_clock_seconds: float
    credits_spent: int
    generations_total: int
    generations_kept: int
    rerolls: int
    moderation_flags: int
    retries: int
    timeouts: int
    gate_wait_seconds: float


@dataclass
class RunReport:
    """Complete run report for an episode."""
    
    episode_id: str
    is_canary: bool
    start_time: str
    end_time: str
    total_wall_clock_seconds: float
    total_credits_spent: int | None
    total_credits_cap: int | None
    paused_for_cap: bool
    shots_count: int
    stages: list[StageMetrics]
    status: str  # "complete", "failed", "paused"
    error: str | None = None
    # G-BUD-1: real USD ledger figures (usd_micros) and per-round retry/cap figures
    budget_usd: dict[str, Any] | None = None
    rounds: dict[str, Any] | None = None
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return asdict(self)
    
    def to_json(self) -> str:
        """Serialize to JSON."""
        return json.dumps(self.to_dict(), indent=2)
    
    def to_markdown(self) -> str:
        """Generate markdown summary."""
        lines = [
            f"# Run Report: {self.episode_id}",
            "",
            f"**Status:** {self.status}",
            f"**Mode:** {'CANARY' if self.is_canary else 'FULL'}",
            f"**Start:** {self.start_time}",
            f"**End:** {self.end_time}",
            f"**Duration:** {self.total_wall_clock_seconds:.1f}s",
            "",
            "## Credits",
            f"- Spent: {_fmt(self.total_credits_spent)}/{_fmt(self.total_credits_cap)} credits",
            f"- Paused for cap: {'Yes' if self.paused_for_cap else 'No'}",
            "",
        ]
        if self.budget_usd:
            b = self.budget_usd
            lines += [
                "## Budget (USD)",
                f"- Episode: {_usd(b.get('episode_total'))} of {_usd(b.get('episode_cap'))} cap "
                f"(stop at {_usd(b.get('episode_stop'))})",
                "",
            ]
        if self.rounds:
            r = self.rounds
            lines += [
                "## Review rounds",
                f"- Round cap: {_usd(r.get('round_cap'))} (stop at {_usd(r.get('round_stop'))}); "
                f"max paid retries per shot per round: {r.get('max_paid_retries_per_shot_per_round')}",
            ]
            for name, rd in sorted((r.get("rounds") or {}).items()):
                lines.append(
                    f"- {name}: {_usd(rd.get('total'))} spent/held, paid retries {rd.get('paid_retries', 0)}"
                )
            if r.get("clip_retries"):
                lines.append(f"- Clip retries: {r['clip_retries']}")
            if r.get("rejected_at_g2"):
                lines.append(f"- Rejected at G2 (no clip): {', '.join(r['rejected_at_g2'])}")
            lines.append("")
        lines += [
            "## Shots",
            f"- Total: {self.shots_count}",
            "",
            "## Stages",
            "",
        ]
        
        # Stage table
        lines.append("| Stage | Time (s) | Credits | Gens | Kept | Rerolls | Flags | Retries | Timeouts | Gate Wait (s) |")
        lines.append("|-------|----------|---------|------|------|---------|-------|---------|----------|---------------|")
        
        for stage in self.stages:
            lines.append(
                f"| {stage.stage_name} | {stage.wall_clock_seconds:.1f} | "
                f"{stage.credits_spent} | {stage.generations_total} | {stage.generations_kept} | "
                f"{stage.rerolls} | {stage.moderation_flags} | {stage.retries} | "
                f"{stage.timeouts} | {stage.gate_wait_seconds:.1f} |"
            )
        
        if self.error:
            lines.extend(["", "## Error", "", f"```", self.error, "```"])
        
        return "\n".join(lines)
    
    def save(self, output_dir: str | Path):
        """Save report as JSON and markdown."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        base_name = f"{self.episode_id}_{'canary' if self.is_canary else 'full'}_{timestamp}"
        
        # Save JSON
        json_path = output_dir / f"{base_name}.json"
        json_path.write_text(self.to_json())
        
        # Save markdown
        md_path = output_dir / f"{base_name}.md"
        md_path.write_text(self.to_markdown())
        
        return {
            "json_path": str(json_path),
            "md_path": str(md_path),
        }


def _fmt(value) -> str:
    return "n/a" if value is None else str(value)


def _usd(micros) -> str:
    return "n/a" if micros is None else f"${micros / 1_000_000:.2f}"


def create_run_report(
    episode_id: str,
    is_canary: bool,
    start_time: datetime,
    end_time: datetime,
    credits_spent: int,
    credits_cap: int,
    paused_for_cap: bool,
    shots_count: int,
    stages: list[dict[str, Any]],
    status: str = "complete",
    error: str | None = None,
    budget_usd: dict[str, Any] | None = None,
    rounds: dict[str, Any] | None = None,
) -> RunReport:
    """
    Create a run report from workflow data.
    
    Args:
        episode_id: Episode identifier
        is_canary: CANARY mode flag
        start_time: Workflow start time
        end_time: Workflow end time
        credits_spent: Total credits spent
        credits_cap: Episode credit cap
        paused_for_cap: Whether workflow paused for cap
        shots_count: Number of shots
        stages: List of stage metric dicts
        status: "complete", "failed", or "paused"
        error: Error message if failed
    
    Returns:
        RunReport object
    """
    stage_metrics = [
        StageMetrics(
            stage_name=s["stage_name"],
            wall_clock_seconds=s.get("wall_clock_seconds", 0.0),
            credits_spent=s.get("credits_spent", 0),
            generations_total=s.get("generations_total", 0),
            generations_kept=s.get("generations_kept", 0),
            rerolls=s.get("rerolls", 0),
            moderation_flags=s.get("moderation_flags", 0),
            retries=s.get("retries", 0),
            timeouts=s.get("timeouts", 0),
            gate_wait_seconds=s.get("gate_wait_seconds", 0.0),
        )
        for s in stages
    ]
    
    return RunReport(
        episode_id=episode_id,
        is_canary=is_canary,
        start_time=start_time.isoformat(),
        end_time=end_time.isoformat(),
        total_wall_clock_seconds=(end_time - start_time).total_seconds(),
        total_credits_spent=credits_spent,
        total_credits_cap=credits_cap,
        paused_for_cap=paused_for_cap,
        shots_count=shots_count,
        stages=stage_metrics,
        status=status,
        error=error,
        budget_usd=budget_usd,
        rounds=rounds,
    )
