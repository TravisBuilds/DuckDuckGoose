"""Audio generation activities: ElevenLabs stubs."""

import asyncio

from temporalio import activity

from hfvg.config import config


@activity.defn
async def generate_voiceover(script: str, voice_id: str) -> str:
    """
    Generate voiceover from script using ElevenLabs.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_voiceover", "voice": voice_id})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated voiceover with voice {voice_id}")
        return "https://example.com/audio/voiceover.mp3"

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")


@activity.defn
async def generate_sfx(description: str) -> str:
    """
    Generate sound effects from description.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_sfx"})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated SFX: {description}")
        return "https://example.com/audio/sfx.mp3"

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")


@activity.defn
async def generate_music(style: str, duration: float) -> str:
    """
    Generate background music.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_music", "duration": duration})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated music: {style}, {duration}s")
        return "https://example.com/audio/music.mp3"

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")
