"""QC activities: still_review and clip_qc agent stubs."""

import asyncio
import os
import random

from temporalio import activity

from hfvg.config import config


@activity.defn
async def precheck_still_qc(episode_id: str, shot_id: str, prompt: str, params: dict) -> dict:
    """
    Pre-flight QC checks before submitting still generation.
    
    Checks prompt and parameters for obvious issues that would fail QC.
    This runs BEFORE any paid generation to catch issues early.
    
    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "precheck_qc", "shot": shot_id})
    
    # In dry run, always pass precheck
    if config.DRY_RUN:
        return {"passed": True, "issues": []}
    
    # In live mode, check for basic issues
    issues = []
    
    # Check prompt length
    if not prompt or len(prompt) < 10:
        issues.append("Prompt too short")
    
    # Check for banned words (example)
    banned_words = ["nsfw", "explicit", "gore"]
    if any(word in prompt.lower() for word in banned_words):
        issues.append("Prompt contains banned content")
    
    passed = len(issues) == 0
    
    activity.logger.info(
        f"Pre-flight QC for {shot_id}: {'PASS' if passed else 'FAIL'} - {issues}"
    )
    
    return {"passed": passed, "issues": issues}


@activity.defn
async def precheck_clip_qc(episode_id: str, shot_id: str, prompt: str, duration: float) -> dict:
    """
    Pre-flight QC checks before submitting clip generation.
    
    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "precheck_clip_qc", "shot": shot_id})
    
    # In dry run, always pass precheck
    if config.DRY_RUN:
        return {"passed": True, "issues": []}
    
    # In live mode, check for basic issues
    issues = []
    
    # Check duration
    if duration < 3.0 or duration > 15.0:
        issues.append(f"Duration {duration}s outside valid range (3-15s)")
    
    # Check prompt
    if not prompt or len(prompt) < 10:
        issues.append("Prompt too short")
    
    passed = len(issues) == 0
    
    activity.logger.info(
        f"Pre-flight clip QC for {shot_id}: {'PASS' if passed else 'FAIL'} - {issues}"
    )
    
    return {"passed": passed, "issues": issues}


@activity.defn
async def review_still(asset_url: str, rubric: dict, episode_id: str = None, shot_id: str = None) -> dict:
    """
    Agent-based still review against a rubric.

    Args:
        asset_url: URL of the still asset to review
        rubric: Review rubric (not used in current stub)
        episode_id: Episode identifier (required for live mode checks)
        shot_id: Shot identifier (for logging)

    Returns:
        dict with 'passed' (bool), 'issues' (list of strings), and 'escalate' (bool) in live mode
    """
    # Heartbeat only if in activity context
    try:
        activity.heartbeat({"stage": "reviewing_still", "url": asset_url})
    except RuntimeError:
        # Not in activity context (e.g., direct test call) - skip heartbeat
        pass


    # Check live mode from DB
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        return {"passed": True, "issues": []}
    
    # Fail closed: if episode_id is missing, cannot verify live mode, so escalate
    if not episode_id:
        activity.logger.error(
            f"[FAIL CLOSED] Still review called without episode_id. "
            f"Cannot verify live mode. Escalating to human review. Asset: {asset_url}"
        )
        return {
            "passed": False,
            "issues": ["episode_id not provided - cannot verify live mode"],
            "escalate": True,
        }
    
    # Check DB requirements
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
                (episode_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    # Episode not found - fail closed
                    activity.logger.error(
                        f"[FAIL CLOSED] Episode {episode_id} not found in DB. "
                        f"Escalating to human review. Asset: {asset_url}"
                    )
                    return {
                        "passed": False,
                        "issues": [f"Episode {episode_id} not found"],
                        "escalate": True,
                    }
                
                live_mode = bool(row[0])
                g108_approved = bool(row[1])
                
                # Fail closed: if DRY_RUN=false but live requirements not met, escalate
                # Don't fall back to dry mode when DRY_RUN=false is explicitly set
                if not live_mode or not g108_approved:
                    activity.logger.error(
                        f"[FAIL CLOSED] Still review: DRY_RUN=false but live requirements not met "
                        f"(live_mode={live_mode}, g108={g108_approved}). Escalating."
                    )
                    return {
                        "passed": False,
                        "issues": [f"DRY_RUN=false but episode not in live mode or G1.08 not approved"],
                        "escalate": True,
                    }
    except Exception as e:
        # Fail closed on DB error
        activity.logger.error(f"[FAIL CLOSED] DB error checking live mode: {e}. Escalating.")
        return {
            "passed": False,
            "issues": [f"DB error: {str(e)[:100]}"],
            "escalate": True,
        }
    
    # Live mode confirmed: always escalate to human review (real QC not implemented)
    activity.logger.info(
        f"[LIVE MODE] Still review for {episode_id}/{shot_id}. "
        f"Escalating to human review. Asset: {asset_url}"
    )
    return {
        "passed": False,
        "issues": ["Real QC agent not implemented - escalate to human review"],
        "escalate": True,  # Signal that this needs human review, not retry
    }


@activity.defn
async def review_clip(asset_url: str, rubric: dict, episode_id: str = None, shot_id: str = None) -> dict:
    """
    Agent-based clip QC against a rubric.

    Args:
        asset_url: URL of the clip asset to review
        rubric: Review rubric (not used in current stub)
        episode_id: Episode identifier (required for live mode checks)
        shot_id: Shot identifier (for logging)

    Returns:
        dict with 'passed' (bool), 'issues' (list of strings), and 'escalate' (bool) in live mode
    """
    # Heartbeat only if in activity context
    try:
        activity.heartbeat({"stage": "reviewing_clip", "url": asset_url})
    except RuntimeError:
        # Not in activity context (e.g., direct test call) - skip heartbeat
        pass


    # Check live mode from DB
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # DRY_RUN forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)
        return {"passed": True, "issues": []}
    
    # Fail closed: if episode_id is missing, cannot verify live mode, so escalate
    if not episode_id:
        activity.logger.error(
            f"[FAIL CLOSED] Clip review called without episode_id. "
            f"Cannot verify live mode. Escalating to human review. Asset: {asset_url}"
        )
        return {
            "passed": False,
            "issues": ["episode_id not provided - cannot verify live mode"],
            "escalate": True,
        }
    
    # Check DB requirements
    try:
        import aiosqlite
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
                (episode_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    # Episode not found - fail closed
                    activity.logger.error(
                        f"[FAIL CLOSED] Episode {episode_id} not found in DB. "
                        f"Escalating to human review. Asset: {asset_url}"
                    )
                    return {
                        "passed": False,
                        "issues": [f"Episode {episode_id} not found"],
                        "escalate": True,
                    }
                
                live_mode = bool(row[0])
                g108_approved = bool(row[1])
                
                # Fail closed: if DRY_RUN=false but live requirements not met, escalate
                # Don't fall back to dry mode when DRY_RUN=false is explicitly set
                if not live_mode or not g108_approved:
                    activity.logger.error(
                        f"[FAIL CLOSED] Clip review: DRY_RUN=false but live requirements not met "
                        f"(live_mode={live_mode}, g108={g108_approved}). Escalating."
                    )
                    return {
                        "passed": False,
                        "issues": [f"DRY_RUN=false but episode not in live mode or G1.08 not approved"],
                        "escalate": True,
                    }
    except Exception as e:
        # Fail closed on DB error
        activity.logger.error(f"[FAIL CLOSED] DB error checking live mode: {e}. Escalating.")
        return {
            "passed": False,
            "issues": [f"DB error: {str(e)[:100]}"],
            "escalate": True,
        }

    # Live mode confirmed: always escalate to human review (real QC not implemented)
    activity.logger.info(
        f"[LIVE MODE] Clip review for {episode_id}/{shot_id}. "
        f"Escalating to human review. Asset: {asset_url}"
    )
    return {
        "passed": False,
        "issues": ["Real QC agent not implemented - escalate to human review"],
        "escalate": True,  # Signal that this needs human review, not retry
    }
