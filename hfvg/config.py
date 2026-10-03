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

    DB_PATH: str = os.getenv("DB_PATH", "hfvg.db")

    TEMPORAL_HOST: str = os.getenv("TEMPORAL_HOST", "localhost:7233")
    TEMPORAL_NAMESPACE: str = os.getenv("TEMPORAL_NAMESPACE", "default")
    TASK_QUEUE: str = os.getenv("TASK_QUEUE", "hfvg-tasks")

    @staticmethod
    def get_higgsfield_credentials() -> Optional[str]:
        """
        Get Higgsfield API credentials in combined format: <key-id>:<secret>
        
        Checks in priority order:
        1. HF_KEY (combined format)
        2. HF_API_KEY containing colon (combined format)
        3. HF_API_KEY + HF_API_SECRET (separate, combine them)
        4. HF_API_KEY_ID + HF_API_KEY_SECRET (docs naming, combine them)
        
        Returns None if no credentials found.
        Never logs or prints the credential value.
        """
        # 1. HF_KEY (combined)
        hf_key = os.getenv("HF_KEY")
        if hf_key:
            return hf_key
        
        # 2. HF_API_KEY containing colon (combined)
        hf_api_key = os.getenv("HF_API_KEY")
        if hf_api_key and ":" in hf_api_key:
            return hf_api_key
        
        # 3. HF_API_KEY + HF_API_SECRET (separate)
        hf_api_secret = os.getenv("HF_API_SECRET")
        if hf_api_key and hf_api_secret:
            return f"{hf_api_key}:{hf_api_secret}"
        
        # 4. HF_API_KEY_ID + HF_API_KEY_SECRET (docs naming)
        hf_api_key_id = os.getenv("HF_API_KEY_ID")
        hf_api_key_secret = os.getenv("HF_API_KEY_SECRET")
        if hf_api_key_id and hf_api_key_secret:
            return f"{hf_api_key_id}:{hf_api_key_secret}"
        
        return None


config = Config()
