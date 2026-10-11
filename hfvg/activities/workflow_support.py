"""
Support activities for workflow data loading.

These activities handle non-deterministic I/O operations
that workflows cannot perform directly.
"""

import os
from pathlib import Path
from temporalio import activity

from hfvg.episode_parser import parse_beatmap as _parse_beatmap
from hfvg.gates import load_policy as _load_policy, GatePolicy
from hfvg.shot_data import load_shot_plans, merge_into_shots, resolve_refs


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


def live_data_required(require: bool) -> bool:
    """Per-shot data is mandatory when the episode is not dry or the DRY_RUN kill switch is off."""
    return bool(require) or os.getenv("DRY_RUN", "true").lower() != "true"


@activity.defn
async def parse_beatmap_activity(
    beatmap_path: str | None = None,
    episode_id: str | None = None,
    require_shot_data: bool = False,
) -> dict:
    """
    Parse beatmap file from filesystem (activity for workflow) and overlay per-shot private data.

    Args:
        beatmap_path: Path to BEATMAP.md, or None to use fixture
        episode_id: when given, shots.json/refs.json (SHOTS_JSON_PATH / REFS_JSON_PATH or the
            episode dir) are loaded and merged: prompt as-is, shot_meta, params, tags, refs.
        require_shot_data: live mode: refuse (ShotDataError) when that data is missing/inconsistent.
            Also forced on when DRY_RUN=false.

    Returns:
        Parsed beatmap dict with shots list (wraps parse_beatmap result)
    """
    if beatmap_path:
        shots = _parse_beatmap(beatmap_path)
    else:
        # Use synthetic fixture for dry-run
        fixture_path = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "sample_beatmap.md"
        shots = _parse_beatmap(str(fixture_path))

    # parse_beatmap returns a list of shots; wrap in dict for workflow
    shots = shots if isinstance(shots, list) else []
    if episode_id:
        require = live_data_required(require_shot_data)
        plans = load_shot_plans(episode_id, require)
        shots = merge_into_shots(episode_id, shots, plans, require)
    return {"shots": shots}


@activity.defn
async def recheck_refs_activity(episode_id: str, ref_keys_by_shot: dict[str, list[str]]) -> dict:
    """G2.01 re-check: which refs exist on disk NOW (files may have been produced since planning)."""
    return resolve_refs(episode_id, ref_keys_by_shot)
