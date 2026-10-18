"""Reference image normalization before upload.

Normalizes reference images (still, _end, anchor) before upload:
- Apply EXIF orientation and convert ICC to sRGB
- Convert to JPEG (quality ~88, strip metadata, flatten alpha onto white)
- Downscale so long side <= REF_MAX_LONG_SIDE (default 1280)
- Compress to <= REF_MAX_BYTES (default ~1 MB, lowering quality stepwise if needed)
- Cache normalized files by source SHA256 + size limits for idempotency
"""

import os
import hashlib
from pathlib import Path
from typing import Tuple

from temporalio import activity

try:
    from PIL import Image, ImageOps, ImageCms
except ImportError:
    Image = None  # type: ignore
    ImageOps = None  # type: ignore
    ImageCms = None  # type: ignore


# Normalization limits (configurable via env)
REF_MAX_LONG_SIDE = int(os.getenv("REF_MAX_LONG_SIDE", "1280"))
REF_MAX_BYTES = int(os.getenv("REF_MAX_BYTES", "1048576"))  # ~1 MB
JPEG_QUALITY_START = int(os.getenv("JPEG_QUALITY_START", "88"))
JPEG_QUALITY_MIN = int(os.getenv("JPEG_QUALITY_MIN", "65"))
NORMALIZED_CACHE_DIR = os.getenv("NORMALIZED_CACHE_DIR", "./data/normalized")


def compute_file_sha256(path: Path) -> str:
    """Compute SHA256 of file content."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            sha.update(chunk)
    return sha.hexdigest()


def normalize_reference_image(
    source_path: Path,
    cache_dir: Path | None = None,
) -> Tuple[Path, str]:
    """
    Normalize a reference image for upload:
    - Apply EXIF orientation and convert ICC to sRGB
    - Convert to JPEG (flatten alpha onto white, strip metadata)
    - Downscale so long side <= REF_MAX_LONG_SIDE
    - Compress to <= REF_MAX_BYTES (lowering quality stepwise if needed)
    
    Returns:
        (normalized_path, original_sha256)
        
    The normalized file is cached by original SHA256 + size limits, so repeated calls with
    the same source and limits return the same normalized file.
    
    Never skippable: there is no env/config switch to bypass it (tests that upload fake
    bytes replace this function in-process via tests/fake_image_passthrough.py).
    
    Args:
        source_path: Path to original reference image
        cache_dir: Directory for normalized cache (default: NORMALIZED_CACHE_DIR or ./data/normalized)
    """
    # Compute source SHA256 for cache key
    source_sha = compute_file_sha256(source_path)
    
    if Image is None:
        raise RuntimeError("Pillow not available; cannot normalize images")
    
    # Cache directory (from config or specified)
    if cache_dir is None:
        cache_dir = Path(NORMALIZED_CACHE_DIR)
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Cache key includes source SHA + size limits (changing limits invalidates cache)
    cache_key = f"{source_sha}_{REF_MAX_LONG_SIDE}_{REF_MAX_BYTES}"
    normalized_path = cache_dir / f"{cache_key}.jpg"
    
    # Return cached file if it exists
    if normalized_path.exists():
        original_size = source_path.stat().st_size
        normalized_size = normalized_path.stat().st_size
        if hasattr(activity, "logger"):
            activity.logger.info(
                f"Using cached normalized image: {source_path.name} "
                f"({original_size:,} bytes) -> {normalized_path.name} "
                f"({normalized_size:,} bytes)"
            )
        return (normalized_path, source_sha)
    
    # Load and normalize
    img = Image.open(source_path)
    original_size = source_path.stat().st_size
    original_dimensions = img.size
    
    # Apply EXIF orientation if present
    if ImageOps is not None:
        img = ImageOps.exif_transpose(img)
    
    # Convert ICC profile to sRGB if present
    if ImageCms is not None and img.mode == "RGB" and "icc_profile" in img.info:
        try:
            srgb_profile = ImageCms.createProfile("sRGB")
            input_profile = ImageCms.ImageCmsProfile(img.info["icc_profile"])
            img = ImageCms.profileToProfile(img, input_profile, srgb_profile, outputMode="RGB")
        except Exception:
            # If ICC conversion fails, continue without it (better than crashing)
            pass
    
    # Convert to RGB (flatten alpha onto white background)
    if img.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")
    
    # Downscale if long side > REF_MAX_LONG_SIDE
    long_side = max(img.size)
    if long_side > REF_MAX_LONG_SIDE:
        scale_factor = REF_MAX_LONG_SIDE / long_side
        new_size = (int(img.width * scale_factor), int(img.height * scale_factor))
        img = img.resize(new_size, Image.Resampling.LANCZOS)
    
    # Save as JPEG with quality compression to meet size target
    # Write to temp file, then rename atomically
    temp_path = normalized_path.with_suffix(".tmp")
    quality = JPEG_QUALITY_START
    final_quality = quality
    while quality >= JPEG_QUALITY_MIN:
        try:
            img.save(temp_path, "JPEG", quality=quality, optimize=True, subsampling=0)
        except OSError:
            # Pillow sizes the optimize buffer at width*height bytes; very detailed images
            # overflow it ("Suspension not allowed here"). Save unoptimized instead of crashing.
            img.save(temp_path, "JPEG", quality=quality, optimize=False, subsampling=0)
        normalized_size = temp_path.stat().st_size
        final_quality = quality
        
        if normalized_size <= REF_MAX_BYTES or quality == JPEG_QUALITY_MIN:
            break
        
        # Reduce quality and retry
        quality -= 5
    
    # Atomic rename to final location
    temp_path.rename(normalized_path)
    
    final_size = normalized_path.stat().st_size
    final_dimensions = img.size
    
    if hasattr(activity, "logger"):
        activity.logger.info(
            f"Normalized reference image: {source_path.name} "
            f"{original_dimensions} ({original_size:,} bytes) -> "
            f"{final_dimensions} ({final_size:,} bytes, quality={final_quality})"
        )
    
    return (normalized_path, source_sha)
