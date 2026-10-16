"""Credit pricing for generation models.

All prices in credits (not USD). 1 credit = variable cost depending on model.
These are the credit costs that CreditLedger tracks.

Model selection and pricing sources:
- Reference generation: SOUL V2 (higgsfield-ai/soul/v2/standard)
  https://docs.higgsfield.ai/docs/models/soul-2/generate
  Pricing via /estimate endpoint: ~0.09 credits per image
  
- Still generation with character references: Grok Image 2.0 (xai/grok-imagine-image-2.0)
  Accepts image references for identity/style matching (like GPT Image)
  https://docs.higgsfield.ai/reference/POST_generate_image
  Pricing via /estimate endpoint: ~4-7 credits per image depending on quality
"""

# Reference generation pricing (credits per image)
# Using SOUL V2: higgsfield-ai/soul/v2/standard
REF_CREDITS = {
    "higgsfield-ai/soul/v2/standard": 0.09,  # From /estimate endpoint
    "soul_v2": 0.09,  # Alias for backward compatibility
}

# Still generation pricing (credits per image)
# Using Grok Image 2.0: xai/grok-imagine-image-2.0 (accepts reference images)
# CORRECTED: These values now reflect actual provider costs (~1.12 USD = ~1.12 credits)
STILL_CREDITS = {
    "xai/grok-imagine-image-2.0": {
        "high": 1.5,      # Grok Image 2.0 high quality (~1.5 credits)
        "standard": 1.12,  # Grok Image 2.0 standard (~1.12 credits actual cost)
    },
    "gpt_image_2": {
        "high": 1.5,      # GPT Image 2 high quality (deprecated, use Grok)
        "standard": 1.12,  # GPT Image 2 standard (deprecated, use Grok)
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


def estimate_ref_credits(model: str) -> float:
    """Estimate credits for a reference image generation.
    
    Args:
        model: Model path (e.g., "higgsfield-ai/soul/v2/standard")
    
    Returns:
        Estimated credits (float, preserves precision for small costs like 0.09)
    """
    if model in REF_CREDITS:
        return REF_CREDITS[model]
    # Default for unknown ref models
    return 0.1


def estimate_still_credits(model: str, quality: str = "standard") -> int:
    """Estimate credits for a still image generation.
    
    Args:
        model: Model path (e.g., "xai/grok-imagine-image-2.0", "gpt_image_2")
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


async def call_higgsfield_estimate(model_path: str, params: dict) -> float:
    """
    Call Higgsfield /estimate endpoint to get live credit estimate.
    
    Only called in live mode. In dry mode, use the pricing table above.
    
    Args:
        model_path: Full model path (e.g., "xai/grok-imagine-image-2.0" or "higgsfield-ai/soul/v2/standard")
        params: Generation parameters (prompt, image_urls for refs, resolution, etc.)
    
    Returns:
        Estimated credits (float)
        
    Raises:
        RuntimeError: If API call fails
        
    Example:
        # For still with refs
        cost = await call_higgsfield_estimate(
            "xai/grok-imagine-image-2.0",
            {"prompt": "...", "image_urls": ["https://..."], "resolution": "1k"}
        )
        
        # For ref generation
        cost = await call_higgsfield_estimate(
            "higgsfield-ai/soul/v2/standard",
            {"prompt": "..."}
        )
    
    API Reference:
        https://docs.higgsfield.ai/reference/POST_estimate_{model_path}
    """
    import os
    import httpx
    
    api_key = os.getenv("HIGGSFIELD_API_KEY")
    if not api_key:
        raise RuntimeError("HIGGSFIELD_API_KEY not set for live estimate call")
    
    url = f"https://api.higgsfield.ai/estimate/{model_path}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, json=params, headers=headers, timeout=10.0)
            response.raise_for_status()
            result = response.json()
            # Response format: {"credits": 0.09} or similar
            return float(result.get("credits", 0.0))
        except Exception as e:
            raise RuntimeError(f"Higgsfield /estimate call failed for {model_path}: {e}") from e
