"""Configuration and dry-run settings."""

import os
from typing import Optional


class Config:
    """Global configuration."""

    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

    DRY_RUN_STILL_DELAY: float = float(os.getenv("DRY_RUN_STILL_DELAY", "2.0"))
    DRY_RUN_CLIP_DELAY: float = float(os.getenv("DRY_RUN_CLIP_DELAY", "5.0"))
    DRY_RUN_QC_DELAY: float = float(os.getenv("DRY_RUN_QC_DELAY", "1.0"))
    DRY_RUN_MEDIA_DELAY: float = float(os.getenv("DRY_RUN_MEDIA_DELAY", "3.0"))
    DRY_RUN_AUDIO_DELAY: float = float(os.getenv("DRY_RUN_AUDIO_DELAY", "2.0"))

    DRY_RUN_FAILURE_RATE: float = float(os.getenv("DRY_RUN_FAILURE_RATE", "0.0"))
    DRY_RUN_CONTENT_BLOCK_RATE: float = float(
        os.getenv("DRY_RUN_CONTENT_BLOCK_RATE", "0.0")
    )
    DRY_RUN_STALL_RATE: float = float(os.getenv("DRY_RUN_STALL_RATE", "0.0"))

    INITIAL_CREDIT_BALANCE: float = float(os.getenv("INITIAL_CREDIT_BALANCE", "10000.0"))
    LOW_BALANCE_THRESHOLD: float = float(os.getenv("LOW_BALANCE_THRESHOLD", "1000.0"))

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")


config = Config()
