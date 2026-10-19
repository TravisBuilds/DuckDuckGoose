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


# Reference image caps are now managed by the model capabilities registry (hfvg/model_caps.py).
# See docs/MODEL-CAPS.md for details.


def max_refs_for(model_path: str) -> int:
    """
    Max reference images for model_path from the registry.
    Unknown models raise UnknownModelError (no fallback).
    MODEL_MAX_REFS_JSON may lower (never raise) a registry cap unless MODEL_CAPS_ALLOW_RAISE=1.
    """
    from hfvg import model_caps

    registry = model_caps.get_registry()
    caps = registry.resolve(model_path)  # raises UnknownModelError
    limit = caps.limit("max_refs")

    if limit is None or limit.unknown:
        raise model_caps.UnknownModelError(
            f"Model '{model_path}' has no max_refs limit in the registry"
        )

    registry_value = limit.value
    if not isinstance(registry_value, int) or registry_value < 0:
        raise ValueError(f"Invalid max_refs value for {model_path}: {registry_value}")

    # Check for MODEL_MAX_REFS_JSON override
    override_json = os.getenv("MODEL_MAX_REFS_JSON")
    if override_json:
        overrides = json.loads(override_json)
        if model_path in overrides:
            override_value = int(overrides[model_path])
            if override_value < 0:
                raise ValueError(f"MODEL_MAX_REFS_JSON: negative cap for {model_path}")

            # Lowering is always allowed
            if override_value <= registry_value:
                return override_value

            # Raising requires explicit flag
            if os.getenv("MODEL_CAPS_ALLOW_RAISE") == "1":
                source_info = limit.source_urls(registry)
                source_desc = (
                    f"{source_info[0][0]} checked {source_info[0][1]}"
                    if source_info
                    else "registry"
                )
                logger.warning(
                    "MODEL_MAX_REFS_JSON raising cap",
                    model=model_path,
                    registry_value=registry_value,
                    override_value=override_value,
                    source=source_desc,
                )
                return override_value
            else:
                # Silently clamp to registry value (don't raise)
                return registry_value

    return registry_value


def still_model_path() -> str:
    """The still model the provider will call (MODEL_PATH_STILL, default Grok Image 2.0)."""
    return os.getenv("MODEL_PATH_STILL", "xai/grok-imagine-image-2.0")


class Config:
    """Global configuration."""

    DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    
    @staticmethod
    def database_path() -> str:
        """Get database path from environment."""
        return os.getenv("DATABASE_PATH", "./data/studio.db")

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

    # Max retry attempts per shot (server-side enforcement)
    MAX_SHOT_RETRIES: int = int(os.getenv("MAX_SHOT_RETRIES", "1"))
    
    # Placeholder image URL for G1 clip estimates (pricing doesn't depend on image content)
    # Must be a valid public image URL that the provider can access
    # Required: must be explicitly set via env var (no default)
    @staticmethod
    def get_placeholder_image_url() -> str:
        """Get placeholder image URL, raising clear error if not configured."""
        url = os.getenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", "")
        if not url:
            raise ValueError(
                "G1_ESTIMATE_PLACEHOLDER_IMAGE_URL environment variable is required. "
                "Set it to a valid public image URL (1080x1920 recommended) for clip estimates. "
                "Pricing does not depend on image content, so any static placeholder works."
            )
        return url
    
    # Reference image normalization limits (before upload)
    # Large images (e.g. 1520x2688 PNG 4-8 MB) can cause provider failures
    REF_MAX_LONG_SIDE: int = int(os.getenv("REF_MAX_LONG_SIDE", "1280"))
    REF_MAX_BYTES: int = int(os.getenv("REF_MAX_BYTES", "1048576"))  # ~1 MB
    JPEG_QUALITY_START: int = int(os.getenv("JPEG_QUALITY_START", "88"))
    JPEG_QUALITY_MIN: int = int(os.getenv("JPEG_QUALITY_MIN", "65"))

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")


config = Config()
