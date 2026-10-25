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
async def load_gate_policy_activity(project: str | None = None) -> dict:
    """
    Load gate policy from filesystem (activity for workflow).
    
    Args:
        project: Project name. If None, reads from HFVG_PROJECT env var (default: 'demo')
    
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
        "owner_approval_points": policy.owner_approval_points,
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


@activity.defn
async def upsert_episode_activity(
    episode_id: str,
    beatmap_path: str | None = None,
    dry_run: bool = True,
) -> None:
    """
    Upsert episode record at workflow start.
    
    Sets live_mode = not dry_run. This is the single source of truth for whether
    an episode is in live mode (no separate manual "set live" step).
    
    Args:
        episode_id: Episode identifier
        beatmap_path: Path to beatmap file
        dry_run: If False, sets live_mode=1
    """
    from hfvg.studio_db import upsert_episode
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    await upsert_episode(
        db_path,
        episode_id,
        beatmap_path=beatmap_path,
        live_mode=not dry_run,
    )
    activity.logger.info(
        f"Upserted episode {episode_id} (live_mode={not dry_run}, beatmap={beatmap_path})"
    )


@activity.defn
async def approve_g108_activity(episode_id: str) -> None:
    """
    Approve G1.08 credit plan when G1_SHOT_PLAN gate is approved.
    
    G1 approval implies credit plan approval (they're presented together),
    so this is called automatically when G1 is approved.
    
    Args:
        episode_id: Episode identifier
    """
    from hfvg.studio_db import approve_g108
    
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    await approve_g108(db_path, episode_id, user="workflow")
    activity.logger.info(f"Approved G1.08 credit plan for {episode_id} via G1_SHOT_PLAN approval")
