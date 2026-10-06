"""
Support activities for workflow data loading.

These activities handle non-deterministic I/O operations
that workflows cannot perform directly.
"""

from pathlib import Path
from temporalio import activity

from hfvg.episode_parser import parse_beatmap as _parse_beatmap
from hfvg.gates import load_policy as _load_policy, GatePolicy


@activity.defn
async def load_gate_policy_activity(project: str = "mid-mountain-rest") -> dict:
    """
    Load gate policy from filesystem (activity for workflow).
    
    Args:
        project: Project name
    
    Returns:
        Policy data as dict (serializable)
    """
    policy = _load_policy(project)
    
    # Return serializable policy data
    return {
        "schema_version": policy.schema_version,
        "series": policy.series,
        "harness": policy.harness,
        "gates": policy.gates,
        "travis_approval_points": policy.travis_approval_points,
        "budgets": policy.budgets,
        "rules": policy.rules,
    }


@activity.defn
async def parse_beatmap_activity(beatmap_path: str | None = None) -> dict:
    """
    Parse beatmap file from filesystem (activity for workflow).
    
    Args:
        beatmap_path: Path to BEATMAP.md, or None to use fixture
    
    Returns:
        Parsed beatmap dict with shots, scenes, metadata
    """
    if beatmap_path:
        return _parse_beatmap(beatmap_path)
    else:
        # Use synthetic fixture for dry-run
        fixture_path = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "sample_beatmap.md"
        return _parse_beatmap(str(fixture_path))
