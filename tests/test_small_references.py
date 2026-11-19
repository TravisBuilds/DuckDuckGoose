"""Small references (docs/CREATION-FLOW-FIXES.md, fix 2): pictures from 128 px on the short side are
kept as uploaded, with a plain note under 512 px, and the copy a model gets is upscaled (Lanczos) to
REF_MIN_SHORT_SIDE before upload, within the long-side limit."""

import hashlib

import pytest
from PIL import Image

from hfvg import cast_store, image_normalize, prop_store, world_store
from hfvg.image_normalize import normalize_reference_image
from tests.cast_helpers import attestation, image_bytes


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "cast"))
    for var in ("CAST_MIN_IMAGE_PX", "CAST_UPLOAD_MAX_BYTES", "REF_MIN_SHORT_SIDE"):
        monkeypatch.delenv(var, raising=False)


def _png(path, size, colour=(200, 120, 40)):
    img = Image.new("RGB", size, (235, 235, 235))
    w, h = size
    for x in range(w // 4, w // 2):
        for y in range(h // 4, h // 2):
            img.putpixel((x, y), colour)
    img.save(path)
    return path


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("size,expected", [
    ((300, 200), (768, 512)),
    ((128, 128), (512, 512)),
    ((256, 512), (512, 1024)),
    ((150, 1200), (160, 1280)),  # the long-side limit wins over the short-side target
])
def test_a_small_reference_is_upscaled_for_the_models_and_the_original_is_kept(tmp_path, size, expected):
    src = _png(tmp_path / "small.png", size)
    before = _sha(src)
    out, sha = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    assert Image.open(out).size == expected
    assert out.name.endswith(f"_up{image_normalize.REF_MIN_SHORT_SIDE}.jpg")
    assert sha == before and _sha(src) == before
    # cached: the same file again
    again, _ = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    assert again == out


def test_a_big_enough_reference_keeps_its_size_and_its_cache_key(tmp_path):
    src = _png(tmp_path / "ok.png", (512, 700))
    out, sha = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    assert Image.open(out).size == (512, 700)
    assert out.name == f"{sha}_{image_normalize.REF_MAX_LONG_SIDE}_{image_normalize.REF_MAX_BYTES}.jpg"


def test_the_upscale_is_lanczos_not_a_blocky_repeat(tmp_path):
    src = tmp_path / "edge.png"
    img = Image.new("RGB", (200, 200), (0, 0, 0))
    for x in range(100, 200):
        for y in range(200):
            img.putpixel((x, y), (255, 255, 255))
    img.save(src)
    out, _ = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    up = Image.open(out).convert("L")
    assert up.size == (512, 512)
    # a smooth ramp across the edge (nearest-neighbour would jump from black to white in one pixel)
    row = [up.getpixel((x, 256)) for x in range(248, 264)]
    assert any(40 < v < 215 for v in row), row


def test_a_small_rotated_photo_is_turned_upright_then_upscaled(tmp_path):
    src = tmp_path / "phone.jpg"
    img = Image.new("RGB", (200, 300), "red")
    for x in range(30):
        for y in range(30):
            img.putpixel((x, y), (0, 0, 255))
    exif = img.getexif()
    exif[0x0112] = 6  # stored sideways: turn 90 degrees clockwise to view
    img.save(src, "JPEG", exif=exif)
    out, _ = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    up = Image.open(out)
    assert up.size == (768, 512)
    assert up.getpixel((740, 30))[2] > 200 and up.getpixel((30, 30))[0] > 200


def test_a_prop_photo_and_a_universe_photo_of_300_px_are_kept(tmp_path):
    library = ("episode", "ep41small")
    item = prop_store.create_item_with_upload(library, "place", "Hill top", "a grassy hill", image_bytes(size=(300, 300)),
                                              filename="found.png", attestation=attestation(), actor="tester")
    photo = item["photos"][0]
    assert (photo["width"], cast_store.size_note(photo["width"], photo["height"])) == (
        300, "Small image, we'll sharpen it up.")
    series = cast_store.create_series("Kite Hill", "u1", actor="tester")
    kept = world_store.add_reference_photo(series["series_id"], image_bytes(size=(200, 260)), filename="found.png",
                                           attestation=attestation(), actor="tester")
    assert cast_store.size_note(kept["width"], kept["height"]) == cast_store.VERY_SMALL_IMAGE_NOTE
    with pytest.raises(cast_store.CastError, match="at least 128 px"):
        world_store.add_reference_photo(series["series_id"], image_bytes(size=(120, 600)), filename="tiny.png",
                                        attestation=attestation(), actor="tester")
