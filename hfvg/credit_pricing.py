"""Credit pricing for generation models.

All prices in credits (not USD). 1 credit = variable cost depending on model.
These are the credit costs that CreditLedger tracks.
"""

# Still generation pricing (credits per image)
STILL_CREDITS = {
    "gpt_image_2": {
        "high": 6.5,      # GPT Image 2 high quality
        "standard": 4.0,  # GPT Image 2 standard
    },
    "higgsfield_still": {
        "high": 5.0,      # Higgsfield high quality
        "standard": 3.0,  # Higgsfield standard
    },
}

# Video generation pricing (credits per second)
CLIP_CREDITS_PER_SECOND = {
    "kling_3.0": {
        "pro": 1.5,       # Kling 3.0 pro mode ~1.5/s
        "standard": 1.0,  # Kling 3.0 standard
    },
    "seedance_2.5": {
        "1080p": 12.0,    # Seedance 2.5 at 1080p: 12 credits/s
        "720p": 7.0,      # Seedance 2.5 at 720p: 7 credits/s
        "480p": 3.0,      # Seedance 2.5 at 480p: 3 credits/s
    },
}

# Fallback/default costs for unknown models
DEFAULT_STILL_CREDITS = 5.0
DEFAULT_CLIP_CREDITS_PER_SECOND = 5.0


def estimate_still_credits(model: str, quality: str = "standard") -> int:
    """Estimate credits for a still image generation.
    
    Args:
        model: Model name (e.g., "gpt_image_2", "higgsfield_still")
        quality: Quality level (e.g., "high", "standard")
    
    Returns:
        Estimated credits (integer)
    """
    if model in STILL_CREDITS:
        return int(STILL_CREDITS[model].get(quality, DEFAULT_STILL_CREDITS))
    return int(DEFAULT_STILL_CREDITS)


def estimate_clip_credits(model: str, duration: float, resolution: str | None = None) -> int:
    """Estimate credits for a video clip generation.
    
    Args:
        model: Model name (e.g., "kling_3.0", "seedance_2.5")
        duration: Duration in seconds
        resolution: Resolution for Seedance (e.g., "1080p", "720p", "480p")
    
    Returns:
        Estimated credits (integer)
    """
    if model.startswith("kling"):
        rate = CLIP_CREDITS_PER_SECOND.get("kling_3.0", {}).get("standard", DEFAULT_CLIP_CREDITS_PER_SECOND)
    elif model.startswith("seedance"):
        if resolution:
            rate = CLIP_CREDITS_PER_SECOND.get("seedance_2.5", {}).get(resolution, DEFAULT_CLIP_CREDITS_PER_SECOND)
        else:
            rate = CLIP_CREDITS_PER_SECOND.get("seedance_2.5", {}).get("720p", DEFAULT_CLIP_CREDITS_PER_SECOND)
    else:
        rate = DEFAULT_CLIP_CREDITS_PER_SECOND
    
    return int(rate * duration)
