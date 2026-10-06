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
async def review_still(asset_url: str, rubric: dict) -> dict:
    """
    Agent-based still review against a rubric.

    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "reviewing_still", "url": asset_url})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)

        passed = random.random() > 0.2

        activity.logger.info(
            f"[DRY-RUN] Still review: {'PASS' if passed else 'FAIL'} for {asset_url}"
        )

        return {
            "passed": passed,
            "issues": [] if passed else ["Character drift detected", "Height mismatch"],
        }

    # In live mode, check if we're actually in dry run via env var
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    if dry_run:
        # Still in dry run despite config - allow it
        await asyncio.sleep(0.1)
        return {"passed": True, "issues": []}
    
    # Fail closed: reject in live mode until real QC agent is implemented
    # This should escalate to human review, not retry automatically
    activity.logger.error(
        f"[LIVE MODE] Still review attempted but real QC not implemented. "
        f"Escalating to human review. Asset: {asset_url}"
    )
    return {
        "passed": False,
        "issues": ["Real QC agent not implemented - escalate to human review"],
        "escalate": True,  # Signal that this needs human review, not retry
    }


@activity.defn
async def review_clip(asset_url: str, rubric: dict) -> dict:
    """
    Agent-based clip QC against a rubric.

    Returns:
        dict with 'passed' (bool) and 'issues' (list of strings)
    """
    activity.heartbeat({"stage": "reviewing_clip", "url": asset_url})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_QC_DELAY)

        passed = random.random() > 0.15

        activity.logger.info(f"[DRY-RUN] Clip QC: {'PASS' if passed else 'FAIL'} for {asset_url}")

        return {
            "passed": passed,
            "issues": [] if passed else ["Motion stiffness", "Prop continuity break"],
        }

    # In live mode, check if we're actually in dry run via env var
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    if dry_run:
        # Still in dry run despite config - allow it
        await asyncio.sleep(0.1)
        return {"passed": True, "issues": []}
    
    # Fail closed: reject in live mode until real QC agent is implemented
    # This should escalate to human review, not retry automatically
    activity.logger.error(
        f"[LIVE MODE] Clip review attempted but real QC not implemented. "
        f"Escalating to human review. Asset: {asset_url}"
    )
    return {
        "passed": False,
        "issues": ["Real QC agent not implemented - escalate to human review"],
        "escalate": True,  # Signal that this needs human review, not retry
    }
