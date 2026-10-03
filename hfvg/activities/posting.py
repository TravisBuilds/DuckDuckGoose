"""Posting activities: upload and publish to social platforms."""

import asyncio

from temporalio import activity

from hfvg.config import config


@activity.defn
async def post_to_platform(video_url: str, platform: str, caption: str, metadata: dict) -> str:
    """
    Upload and post video to a social platform.

    Returns post ID/URL.
    """
    activity.heartbeat({"stage": "posting", "platform": platform})

    if config.DRY_RUN:
        await asyncio.sleep(2.0)
        activity.logger.info(f"[DRY-RUN] Posted to {platform}: {caption[:50]}...")
        return f"https://{platform}.com/posts/abc123"

    raise NotImplementedError("Real social posting not implemented in milestone 1")
