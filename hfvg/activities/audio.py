"""Audio generation activities: ElevenLabs stubs."""

import asyncio
import os

from temporalio import activity

from hfvg.config import config


@activity.defn
async def generate_voiceover(episode_id: str, script: str, voice_id: str) -> str:
    """
    Generate voiceover from script using ElevenLabs.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_voiceover", "voice": voice_id})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated voiceover with voice {voice_id}")
        return "https://example.com/audio/voiceover.mp3"

    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_voiceover")

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")


@activity.defn
async def generate_sfx(episode_id: str, description: str) -> str:
    """
    Generate sound effects from description.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_sfx"})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated SFX: {description}")
        return "https://example.com/audio/sfx.mp3"

    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_sfx")

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")


@activity.defn
async def generate_music(episode_id: str, style: str, duration: float) -> str:
    """
    Generate background music.

    Returns URL of generated audio.
    """
    activity.heartbeat({"stage": "generating_music", "duration": duration})

    if config.DRY_RUN:
        await asyncio.sleep(config.DRY_RUN_AUDIO_DELAY)
        activity.logger.info(f"[DRY-RUN] Generated music: {style}, {duration}s")
        return "https://example.com/audio/music.mp3"

    # Smoke gate before spending
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "generate_music")

    raise NotImplementedError("Real ElevenLabs not implemented in milestone 1")
