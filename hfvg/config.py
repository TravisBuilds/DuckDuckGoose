"""Configuration and dry-run settings."""

import json
import os
from typing import Optional


def _load_rate_table() -> dict[str, dict[str, int]]:
    """Per-second USD rate table in integer micro-dollars (override: MODEL_USD_PER_SECOND_JSON).

    SOURCE / CAVEAT: these are LIST PRICES BEFORE ANY DISCOUNT, derived, not quoted by an API:
    - seedance-2.5: Higgsfield pricing text (480p 0.2056, 720p 0.4622, 1080p 1.1372 USD/s). The
      estimate endpoint for Seedance returns a pricing description, not USD, so this table is used.
    - kling-3.0-pro: fallback only (estimate normally returns usd); 0.056 USD/s derived from the
      live canary (5 s clip = 0.28 USD).
    Real billing may be lower (discounts); the ledger therefore reserves conservatively.
    """
    table = {
        "seedance-2.5": {"480p": 205_600, "720p": 462_200, "1080p": 1_137_200},
        "kling-3.0-pro": {"default": 56_000},
    }
    override = os.getenv("MODEL_USD_PER_SECOND_JSON")
    if override:
        loaded = json.loads(override)
        table = {m: {k: int(v) for k, v in rates.items()} for m, rates in loaded.items()}
    return table


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

    # Model paths (from docs.higgsfield.ai)
    # GPT Image 2 not in public API: must be configured from cloud.higgsfield.ai console
    MODEL_PATH_GPT_IMAGE_2: str = os.getenv("MODEL_PATH_GPT_IMAGE_2", "")
    # Seedance 2.5 image-to-video (documented)
    MODEL_PATH_SEEDANCE: str = os.getenv(
        "MODEL_PATH_SEEDANCE", "bytedance/seedance-2.5/image-to-video"
    )
    # Kling 3.0 Pro image-to-video (documented)
    MODEL_PATH_KLING: str = os.getenv(
        "MODEL_PATH_KLING", "kling-video/v3.0/pro/image-to-video"
    )

    INITIAL_CREDIT_BALANCE: float = float(os.getenv("INITIAL_CREDIT_BALANCE", "10000.0"))
    LOW_BALANCE_THRESHOLD: float = float(os.getenv("LOW_BALANCE_THRESHOLD", "1000.0"))

    # Per-second USD rate table (integer micro-dollars) for models without a USD estimate.
    # LIST PRICE BEFORE DISCOUNT, derived: see _load_rate_table().
    MODEL_USD_PER_SECOND_MICROS: dict = _load_rate_table()

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")


config = Config()
