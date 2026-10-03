"""Configuration and dry-run settings."""

import os
from typing import Optional


class Config:
    """Global configuration."""

    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    # Near-zero delays for fast testing (configurable via env vars)
    DRY_RUN_STILL_DELAY: float = float(os.getenv("DRY_RUN_STILL_DELAY", "0.05"))
    DRY_RUN_CLIP_DELAY: float = float(os.getenv("DRY_RUN_CLIP_DELAY", "0.1"))
    DRY_RUN_QC_DELAY: float = float(os.getenv("DRY_RUN_QC_DELAY", "0.02"))
    DRY_RUN_MEDIA_DELAY: float = float(os.getenv("DRY_RUN_MEDIA_DELAY", "0.05"))
    DRY_RUN_AUDIO_DELAY: float = float(os.getenv("DRY_RUN_AUDIO_DELAY", "0.05"))

    # Failure injection off by default (tests can enable)
    DRY_RUN_FAILURE_RATE: float = float(os.getenv("DRY_RUN_FAILURE_RATE", "0.0"))
    DRY_RUN_CONTENT_BLOCK_RATE: float = float(
        os.getenv("DRY_RUN_CONTENT_BLOCK_RATE", "0.0")
    )
    DRY_RUN_STALL_RATE: float = float(os.getenv("DRY_RUN_STALL_RATE", "0.0"))

    # Asset registry paths
    SERIES_PATH: str = os.getenv("SERIES_PATH", "./series")
    EPISODE_PATH: str = os.getenv("EPISODE_PATH", "./episode")

    # Model paths (REQUIRED - verify from cloud.higgsfield.ai)
    # No defaults: fail fast if not configured (see docs/PROVIDERS.md)
    MODEL_PATH_GPT_IMAGE_2: str = os.getenv("MODEL_PATH_GPT_IMAGE_2", "")
    MODEL_PATH_SEEDANCE: str = os.getenv("MODEL_PATH_SEEDANCE", "")
    MODEL_PATH_KLING: str = os.getenv("MODEL_PATH_KLING", "")

    INITIAL_CREDIT_BALANCE: float = float(os.getenv("INITIAL_CREDIT_BALANCE", "10000.0"))
    LOW_BALANCE_THRESHOLD: float = float(os.getenv("LOW_BALANCE_THRESHOLD", "1000.0"))

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")


config = Config()
