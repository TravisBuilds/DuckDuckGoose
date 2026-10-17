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


# Per-model max reference images (documented limits, harness default capped at 8). Sources:
# - xai/grok-imagine-image-2.0: image_urls maxItems 10
#   https://docs.higgsfield.ai/docs/models/grok-image-2/generate-and-edit  -> harness cap 8
# - alibaba/qwen-image-3/edit: image_urls minItems 1, maxItems 3
#   https://docs.higgsfield.ai/docs/models/qwen-image-3/edit  -> 3
# - bytedance/seedance-2.5/reference-to-video: image_urls maxItems 30 (50 media total)
#   https://docs.higgsfield.ai/docs/models/seedance-2-5/reference-to-video  -> harness cap 8
# - kling-video/v3.0/pro/image-to-video: only image_url + last_image_url frames; "elements" has no
#   documented maxItems (UNCONFIRMED)  https://docs.higgsfield.ai/docs/models/kling-3/pro-image-to-video -> 3
# Any model not in the table falls back to STILL_MAX_REFS (default 3).
DEFAULT_MODEL_MAX_REFS: dict[str, int] = {
    "xai/grok-imagine-image-2.0": 8,
    "alibaba/qwen-image-3/edit": 3,
    "bytedance/seedance-2.5/reference-to-video": 8,
    "kling-video/v3.0/pro/image-to-video": 3,
}


def model_max_refs_table() -> dict[str, int]:
    """Defaults merged with MODEL_MAX_REFS_JSON ('{"model/path": n}'), read at call time."""
    table = dict(DEFAULT_MODEL_MAX_REFS)
    override = os.getenv("MODEL_MAX_REFS_JSON")
    if override:
        for model, cap in json.loads(override).items():
            cap = int(cap)
            if cap < 0:
                raise ValueError(f"MODEL_MAX_REFS_JSON: negative cap for {model}")
            table[model] = cap
    return table


def max_refs_for(model_path: str) -> int:
    """Max reference images for model_path; unknown models fall back to STILL_MAX_REFS (3)."""
    table = model_max_refs_table()
    if model_path in table:
        return table[model_path]
    return int(os.getenv("STILL_MAX_REFS", "3"))


def still_model_path() -> str:
    """The still model the provider will call (MODEL_PATH_STILL, default Grok Image 2.0)."""
    return os.getenv("MODEL_PATH_STILL", "xai/grok-imagine-image-2.0")


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
    # SOUL V2 is the default image model (documented, available via API)
    # https://docs.higgsfield.ai/docs/models/soul-2/generate
    MODEL_PATH_SOUL_V2: str = os.getenv("MODEL_PATH_SOUL_V2", "higgsfield-ai/soul/v2/standard")
    # Legacy GPT Image 2 not in public API: use SOUL V2 instead
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

    # Fallback max refs for models missing from DEFAULT_MODEL_MAX_REFS (see max_refs_for). Over the
    # limit the harness REFUSES (never silently truncates a shot's ref list).
    STILL_MAX_REFS: int = int(os.getenv("STILL_MAX_REFS", "3"))
    
    # Max retry attempts per shot (server-side enforcement)
    MAX_SHOT_RETRIES: int = int(os.getenv("MAX_SHOT_RETRIES", "1"))

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")


config = Config()
