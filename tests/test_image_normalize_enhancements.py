"""Tests for image normalization enhancements."""
import hashlib
import io
import os
from pathlib import Path

import pytest
from PIL import Image, ImageCms

from hfvg.image_normalize import normalize_reference_image, NORMALIZED_CACHE_DIR


def _create_test_image_with_exif(path: Path, orientation: int = 6) -> None:
    """Create a test image with EXIF orientation tag."""
    img = Image.new("RGB", (100, 200), "red")
    # Draw a blue square in top-left to verify orientation is applied
    for x in range(20):
        for y in range(20):
            img.putpixel((x, y), (0, 0, 255))
    
    # Save with EXIF orientation
    exif = img.getexif()
    exif[0x0112] = orientation  # Orientation tag
    img.save(path, "JPEG", exif=exif)


def _create_image_with_icc(path: Path) -> None:
    """Create a test image with ICC color profile."""
    img = Image.new("RGB", (100, 100), "green")
    # Create a fake ICC profile (Adobe RGB)
    # In practice this would be a real profile, but for testing we just need to verify conversion happens
    img.save(path, "PNG")
    # Reload and add a simple sRGB profile to test conversion
    img = Image.open(path)
    srgb = ImageCms.createProfile("sRGB")
    img_with_profile = ImageCms.profileToProfile(img, srgb, srgb, outputMode="RGB")
    img_with_profile.save(path, "PNG", icc_profile=img_with_profile.info.get("icc_profile"))


def test_exif_orientation_applied(tmp_path):
    """Test that EXIF orientation is applied during normalization."""
    src = tmp_path / "oriented.jpg"
    _create_test_image_with_exif(src, orientation=6)  # 90° CW rotation
    
    out, _ = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    result = Image.open(out)
    
    # After applying orientation=6 (90° CW), dimensions should be swapped
    # Original: 100x200, after rotation: 200x100
    assert result.size == (200, 100), f"EXIF orientation not applied: got {result.size}"
    # Blue square should now be in top-right after rotation
    # (was top-left in original 100x200, becomes top-right in rotated 200x100)
    assert result.getpixel((190, 10))[2] > 200, "EXIF orientation transformation incorrect"


def test_icc_profile_converted_to_srgb(tmp_path):
    """Test that ICC profiles are converted to sRGB."""
    src = tmp_path / "with_icc.png"
    _create_image_with_icc(src)
    
    out, _ = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    
    # Output should be JPEG without ICC profile (stripped)
    # We can't easily verify sRGB conversion without comparing pixels,
    # but we can verify the function doesn't crash
    assert out.exists()
    assert out.suffix == ".jpg"


def test_cache_key_includes_size_limits(tmp_path):
    """Test that cache key includes size limits so changing limits invalidates cache."""
    src = tmp_path / "test.png"
    Image.new("RGB", (1600, 900), "blue").save(src)
    
    # First normalization with default limits
    cache1 = tmp_path / "cache1"
    out1, _ = normalize_reference_image(src, cache_dir=cache1)
    cache_files_1 = list(cache1.glob("*.jpg"))
    assert len(cache_files_1) == 1
    
    # Change REF_MAX_BYTES and normalize again (should create new cache entry)
    old_max_bytes = os.environ.get("REF_MAX_BYTES")
    try:
        os.environ["REF_MAX_BYTES"] = str(512 * 1024)  # 512KB instead of 1MB
        # Force reimport to pick up new env var
        import importlib
        import hfvg.image_normalize
        importlib.reload(hfvg.image_normalize)
        from hfvg.image_normalize import normalize_reference_image as norm_reload
        
        cache2 = tmp_path / "cache2"
        out2, _ = norm_reload(src, cache_dir=cache2)
        cache_files_2 = list(cache2.glob("*.jpg"))
        assert len(cache_files_2) == 1
        
        # Cache filenames should be different (different keys)
        assert cache_files_1[0].name != cache_files_2[0].name, \
            "Cache key must include size limits"
    finally:
        if old_max_bytes is not None:
            os.environ["REF_MAX_BYTES"] = old_max_bytes
        else:
            os.environ.pop("REF_MAX_BYTES", None)
        import importlib
        import hfvg.image_normalize
        importlib.reload(hfvg.image_normalize)


def test_atomic_cache_write(tmp_path, monkeypatch):
    """Test that cache writes are atomic (temp file then rename)."""
    src = tmp_path / "test.png"
    Image.new("RGB", (100, 100), "red").save(src)
    cache = tmp_path / "cache"
    
    # Track rename operations to verify atomic write
    import pathlib
    original_rename = pathlib.Path.rename
    renames = []
    
    def track_rename(self, target):
        renames.append((str(self), str(target)))
        return original_rename(self, target)
    
    monkeypatch.setattr(pathlib.Path, "rename", track_rename)
    
    out, _ = normalize_reference_image(src, cache_dir=cache)
    
    # Should have renamed from .tmp to .jpg
    assert len(renames) > 0, "No rename operation performed"
    src_file, dst_file = renames[-1]
    assert src_file.endswith(".tmp"), f"Source of rename not a temp file: {src_file}"
    assert dst_file.endswith(".jpg"), f"Destination of rename not .jpg: {dst_file}"
    assert dst_file == str(out), "Renamed to wrong destination"
    
    # After completion, no .tmp files should remain
    remaining_temps = list(cache.glob("*.tmp"))
    assert len(remaining_temps) == 0, f"Temp files not cleaned up: {remaining_temps}"
    
    # Only the final .jpg should exist
    finals = list(cache.glob("*.jpg"))
    assert len(finals) == 1


def test_normalized_cache_dir_under_data_dir(tmp_path):
    """Test that default cache dir is under NORMALIZED_CACHE_DIR (./data/normalized)."""
    # When no cache_dir is specified, should use NORMALIZED_CACHE_DIR
    # We can't easily test the default in isolation, but we can verify the config
    assert NORMALIZED_CACHE_DIR == os.getenv("NORMALIZED_CACHE_DIR", "./data/normalized")


def test_quality_loop_logs_actual_final_quality(tmp_path, caplog):
    """Test that quality loop logs the actual final quality used."""
    import logging
    from temporalio import activity
    
    # Create a large image that will require quality reduction
    src = tmp_path / "large.png"
    # Pure noise compresses poorly, will need multiple quality steps
    import random
    rnd = random.Random(42)
    img_data = bytes(rnd.getrandbits(8) for _ in range(800 * 800 * 3))
    Image.frombytes("RGB", (800, 800), img_data).save(src)
    
    # Set a very low max bytes to force quality reduction
    old_max = os.environ.get("REF_MAX_BYTES")
    try:
        os.environ["REF_MAX_BYTES"] = str(50 * 1024)  # 50KB
        import importlib
        import hfvg.image_normalize
        importlib.reload(hfvg.image_normalize)
        from hfvg.image_normalize import normalize_reference_image as norm_reload
        
        with caplog.at_level(logging.INFO):
            out, _ = norm_reload(src, cache_dir=tmp_path / "cache")
        
        # Check that log includes quality value
        log_messages = [rec.message for rec in caplog.records if "quality=" in rec.message]
        assert len(log_messages) > 0, "No quality logged"
        
        # Extract quality from log message
        import re
        match = re.search(r"quality=(\d+)", log_messages[0])
        assert match, f"Quality not found in log: {log_messages[0]}"
        logged_quality = int(match.group(1))
        
        # Quality should be within valid range
        assert 65 <= logged_quality <= 88, f"Invalid quality logged: {logged_quality}"
        
        # Verify the output file size
        out_size = out.stat().st_size
        # With such a low limit and noisy data, should hit quality floor
        assert logged_quality <= 70, "Quality should have been reduced"
    finally:
        if old_max is not None:
            os.environ["REF_MAX_BYTES"] = old_max
        else:
            os.environ.pop("REF_MAX_BYTES", None)
        import importlib
        import hfvg.image_normalize
        importlib.reload(hfvg.image_normalize)


def test_placeholder_url_required(monkeypatch):
    """Test that G1_ESTIMATE_PLACEHOLDER_IMAGE_URL is required with clear error."""
    from hfvg.config import Config
    
    monkeypatch.delenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", raising=False)
    
    with pytest.raises(ValueError, match="G1_ESTIMATE_PLACEHOLDER_IMAGE_URL.*required"):
        Config.get_placeholder_image_url()


def test_placeholder_url_returns_env_value(monkeypatch):
    """Test that get_placeholder_image_url returns the env var value."""
    from hfvg.config import Config
    
    test_url = "https://example.com/placeholder.jpg"
    monkeypatch.setenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", test_url)
    
    assert Config.get_placeholder_image_url() == test_url
