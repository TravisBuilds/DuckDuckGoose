"""Media processing activities: ffmpeg operations."""

import asyncio

from temporalio import activity

from hfvg.config import config


@activity.defn
async def trim_clips(clip_urls: list[str], edit_decisions: dict) -> str:
    """
    Trim clips according to edit decisions.

    Returns URL of trimmed output.
    """
    activity.heartbeat({"stage": "trimming", "clips": len(clip_urls)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Trimmed {len(clip_urls)} clips")
        return f"https://example.com/edits/trimmed-{len(clip_urls)}.mp4"

    raise NotImplementedError("Real ffmpeg not implemented in milestone 1")


@activity.defn
async def render_edit(trimmed_url: str, transitions: dict) -> str:
    """
    Apply transitions and render picture lock.

    Returns URL of picture-locked output.
    """
    activity.heartbeat({"stage": "rendering_edit"})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info("[DRY-RUN] Rendered edit with transitions")
        return "https://example.com/edits/picture-lock.mp4"

    raise NotImplementedError("Real ffmpeg not implemented in milestone 1")


@activity.defn
async def mix_audio(video_url: str, audio_tracks: list[str]) -> str:
    """
    Mix audio tracks (VO, SFX, music) with video.

    Returns URL of final mixed output.
    """
    activity.heartbeat({"stage": "mixing_audio", "tracks": len(audio_tracks)})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_MEDIA_DELAY)
        activity.logger.info(f"[DRY-RUN] Mixed {len(audio_tracks)} audio tracks")
        return "https://example.com/final/mixed.mp4"

    raise NotImplementedError("Real ffmpeg not implemented in milestone 1")
