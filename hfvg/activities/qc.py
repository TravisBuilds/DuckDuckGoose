"""QC activities: still_review and clip_qc agent stubs."""

import asyncio
import random

from temporalio import activity

from hfvg.config import config


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

    raise NotImplementedError("Real QC agent not implemented in milestone 1")


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

    raise NotImplementedError("Real QC agent not implemented in milestone 1")
