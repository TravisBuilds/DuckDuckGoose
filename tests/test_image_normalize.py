"""Tests for reference image normalization."""

import pytest
import os
from pathlib import Path
from PIL import Image
import tempfile

from hfvg.image_normalize import normalize_reference_image, compute_file_sha256


@pytest.fixture(autouse=True)
def clear_skip_normalize():
    """Clear SKIP_IMAGE_NORMALIZE env var before each test."""
    if "SKIP_IMAGE_NORMALIZE" in os.environ:
        del os.environ["SKIP_IMAGE_NORMALIZE"]
    yield


def create_test_image(path: Path, size: tuple[int, int], mode: str = "RGB") -> None:
    """Create a test image file."""
    img = Image.new(mode, size, color="red")
    img.save(path)


@pytest.mark.asyncio
async def test_normalize_large_png_to_jpeg():
    """Test: large PNG with alpha is converted to JPEG, downscaled, and compressed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create a large PNG with alpha (1520x2688, similar to the failing case)
        source = tmpdir / "large.png"
        create_test_image(source, (1520, 2688), mode="RGBA")
        
        # Normalize
        normalized, original_sha = normalize_reference_image(source)
        
        # Verify normalized file exists
        assert normalized.exists()
        assert normalized.suffix == ".jpg"
        
        # Verify it's smaller
        original_size = source.stat().st_size
        normalized_size = normalized.stat().st_size
        assert normalized_size < original_size
        
        # Verify size is within limits (long side <= 1280)
        img = Image.open(normalized)
        assert max(img.size) <= 1280
        
        # Verify file size is within limit (~1 MB)
        assert normalized_size <= 1048576  # REF_MAX_BYTES default
        
        # Verify original SHA is returned
        assert original_sha == compute_file_sha256(source)


@pytest.mark.asyncio
async def test_normalize_cache_by_sha(tmp_path):
    """Test: normalized images are cached by original SHA256."""
    tmpdir = tmp_path
    
    # Create identical content in two different files
    source1 = tmpdir / "img1.png"
    source2 = tmpdir / "img2.png"
    create_test_image(source1, (1000, 1000))
    create_test_image(source2, (1000, 1000))
    
    # Both should have same SHA
    sha1 = compute_file_sha256(source1)
    sha2 = compute_file_sha256(source2)
    assert sha1 == sha2
    
    # Normalize first file
    cache = tmpdir / "cache"
    norm1, _ = normalize_reference_image(source1, cache_dir=cache)
    assert norm1.exists()
    
    # Normalize second file - should return same cached file
    norm2, _ = normalize_reference_image(source2, cache_dir=cache)
    assert norm1 == norm2


@pytest.mark.asyncio
async def test_normalize_anchor_sha_unchanged():
    """Test: anchor identity (original SHA) is preserved for job IDs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create source image
        source = tmpdir / "anchor.png"
        create_test_image(source, (2000, 2000))
        original_sha = compute_file_sha256(source)
        
        # Normalize
        normalized, returned_sha = normalize_reference_image(source)
        
        # Verify returned SHA is the ORIGINAL, not normalized
        assert returned_sha == original_sha
        assert returned_sha != compute_file_sha256(normalized)


@pytest.mark.asyncio
async def test_normalize_downscale_large_image():
    """Test: images with long side > 1280 are downscaled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create very large image (3000x4000)
        source = tmpdir / "huge.png"
        create_test_image(source, (3000, 4000))
        
        # Normalize
        normalized, _ = normalize_reference_image(source)
        
        # Check dimensions
        img = Image.open(normalized)
        # Long side should be <= 1280
        assert max(img.size) <= 1280
        # Aspect ratio should be preserved
        original_aspect = 4000 / 3000  # 1.333...
        normalized_aspect = max(img.size) / min(img.size)
        assert abs(original_aspect - normalized_aspect) < 0.01


@pytest.mark.asyncio
async def test_normalize_flatten_alpha_to_white():
    """Test: RGBA images are flattened onto white background."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create RGBA image with transparency
        source = tmpdir / "alpha.png"
        img = Image.new("RGBA", (500, 500), (255, 0, 0, 128))  # Semi-transparent red
        img.save(source)
        
        # Normalize
        normalized, _ = normalize_reference_image(source)
        
        # Open normalized and verify it's RGB (no alpha)
        result = Image.open(normalized)
        assert result.mode == "RGB"
        
        # Verify it has white background (pixels should be lighter than pure semi-transparent red)
        pixels = result.getpixel((250, 250))
        # Semi-transparent red (255,0,0,128) on white (255,255,255) should be lighter than (255,0,0)
        assert pixels[0] == 255  # Red channel
        assert pixels[1] > 0  # Green channel should have white mixed in
        assert pixels[2] > 0  # Blue channel should have white mixed in


@pytest.mark.asyncio  
async def test_normalize_compresses_to_size_limit():
    """Test: images are compressed to meet size limit by reducing quality."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create a large, detailed image
        source = tmpdir / "detailed.png"
        # Create complex pattern that doesn't compress well
        img = Image.new("RGB", (2000, 2000))
        pixels = img.load()
        for x in range(2000):
            for y in range(2000):
                pixels[x, y] = ((x * y) % 256, (x + y) % 256, (x - y) % 256)
        img.save(source)
        
        # Normalize
        normalized, _ = normalize_reference_image(source)
        
        # Verify size is within limit
        assert normalized.stat().st_size <= 1048576  # REF_MAX_BYTES
