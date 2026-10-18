"""Reference image normalization before upload.

Normalizes reference images (still, _end, anchor) before upload:
- Convert to JPEG (quality ~88, sRGB, strip metadata, flatten alpha onto white)
- Downscale so long side <= REF_MAX_LONG_SIDE (default 1280)
- Compress to <= REF_MAX_BYTES (default ~1 MB, lowering quality stepwise if needed)
- Cache normalized files by source SHA256 for idempotency
"""

import os
import hashlib
from pathlib import Path
from typing import Tuple

from temporalio import activity

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore


# Normalization limits (configurable via env)
REF_MAX_LONG_SIDE = int(os.getenv("REF_MAX_LONG_SIDE", "1280"))
REF_MAX_BYTES = int(os.getenv("REF_MAX_BYTES", "1048576"))  # ~1 MB
JPEG_QUALITY_START = int(os.getenv("JPEG_QUALITY_START", "88"))
JPEG_QUALITY_MIN = int(os.getenv("JPEG_QUALITY_MIN", "65"))


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
    - Convert to JPEG (sRGB, flatten alpha onto white, strip metadata)
    - Downscale so long side <= REF_MAX_LONG_SIDE
    - Compress to <= REF_MAX_BYTES (lowering quality stepwise if needed)
    
    Returns:
        (normalized_path, original_sha256)
        
    The normalized file is cached by original SHA256, so repeated calls with
    the same source (even if renamed/moved) return the same normalized file.
    
    Never skippable: there is no env/config switch to bypass it (tests that upload fake
    bytes replace this function in-process via tests/fake_image_passthrough.py).
    
    Args:
        source_path: Path to original reference image
        cache_dir: Directory for normalized cache (default: <source_dir>/.normalized)
    """
    # Compute source SHA256 for cache key
    source_sha = compute_file_sha256(source_path)
    
    if Image is None:
        raise RuntimeError("Pillow not available; cannot normalize images")
    
    # Cache directory (next to source, or specified)
    if cache_dir is None:
        cache_dir = source_path.parent / ".normalized"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Cached normalized file: .normalized/<sha256>.jpg
    normalized_path = cache_dir / f"{source_sha}.jpg"
    
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
    quality = JPEG_QUALITY_START
    while quality >= JPEG_QUALITY_MIN:
        try:
            img.save(normalized_path, "JPEG", quality=quality, optimize=True, subsampling=0)
        except OSError:
            # Pillow sizes the optimize buffer at width*height bytes; very detailed images
            # overflow it ("Suspension not allowed here"). Save unoptimized instead of crashing.
            img.save(normalized_path, "JPEG", quality=quality, optimize=False, subsampling=0)
        normalized_size = normalized_path.stat().st_size
        
        if normalized_size <= REF_MAX_BYTES or quality == JPEG_QUALITY_MIN:
            break
        
        # Reduce quality and retry
        quality -= 5
    
    final_size = normalized_path.stat().st_size
    final_dimensions = img.size
    
    if hasattr(activity, "logger"):
        activity.logger.info(
            f"Normalized reference image: {source_path.name} "
            f"{original_dimensions} ({original_size:,} bytes) -> "
            f"{final_dimensions} ({final_size:,} bytes, quality={quality})"
        )
    
    return (normalized_path, source_sha)
