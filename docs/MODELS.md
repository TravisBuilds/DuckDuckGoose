# Model Selection and Pricing

This document describes the AI models used for generation and their pricing structure.

## Reference Images (Character Sheets, Lineup, etc.)

**Model:** SOUL V2  
**Path:** `higgsfield-ai/soul/v2/standard`  
**API Documentation:** https://docs.higgsfield.ai/docs/models/soul-2/generate

### Pricing
- **Estimate Endpoint:** `POST https://api.higgsfield.ai/estimate/higgsfield-ai/soul/v2/standard`
- **Table Price:** ~0.09 credits per image (from documented estimate)
- **Live Mode:** Provider calls `/estimate` endpoint during submission for exact USD cost; credit reservation uses table price
- **Dry Mode:** Uses table price from `hfvg/credit_pricing.py`

### Characteristics
- Fast character sheet generation
- No reference image input support
- Resolution tiers: 720p, 1080p
- Standard quality baseline

## Still Images (Scene Keyframes with Character/Style References)

**Model:** Grok Image 2.0  
**Path:** `xai/grok-imagine-image-2.0`  
**API Documentation:** https://docs.higgsfield.ai/reference/POST_generate_image

### Pricing
- **Estimate Endpoint:** `POST https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0`
- **Table Prices:**
  - All qualities: ~1.12 credits per image
- **Live Mode:** Provider calls `/estimate` endpoint during submission for exact USD cost; credit reservation uses table price
- **Dry Mode:** Uses table price from `hfvg/credit_pricing.py`

### Characteristics
- **Accepts reference images** for identity and style matching (key requirement)
- Similar to GPT Image in accepting image references
- Max references: 8 images per generation (configured via `MODEL_MAX_REFS`)
- Quality levels: high, standard
- Resolution support: 1k, 2k
- Aspect ratios: 9:16, 16:9, etc.

### Why Grok Image 2.0?
Grok Image 2.0 was selected because it accepts reference images (via `image_urls` parameter) for identity and style control, similar to GPT Image. This is critical for maintaining character consistency across shots while applying the visual style from reference sheets.

## Video Clips

**Models:** Kling 3.0, Seedance 2.5 (router selects based on shot requirements)  
**Paths:** Various provider-specific paths  
**Pricing:** Per-second rates defined in `hfvg/credit_pricing.py`

See `PROVIDERS.md` for video model routing and pricing details.

## Pricing Buffer

All estimates include a small buffer (typically ~10%) to account for:
- API pricing fluctuations
- Parameter variations (longer prompts, more references)
- Quality tier variations

The buffer prevents failed submissions due to minor cost overruns while keeping caps meaningful.

## Model Configuration

Model paths are configured via environment variables:
- `MODEL_PATH_STILL`: Still image model (default: `xai/grok-imagine-image-2.0`)
- `MODEL_PATH_SOUL_V2`: Reference image model (default: `higgsfield-ai/soul/v2/standard`)

## Testing

In dry mode (`DRY_RUN=true`):
- No API calls are made
- Fake outputs are generated locally
- Table prices are used (no live `/estimate` calls)
- Useful for testing workflows without spending credits
