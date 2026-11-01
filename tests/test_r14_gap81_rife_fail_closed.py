"""GAP-81 (ep05v2 r14 v12): after the 2026-10-31 reboot the Mesa lavapipe Vulkan ICD was gone;
rife-ncnn-vulkan printed "vkCreateInstance failed -9", still exited 0 and wrote smeared frames, which the
harness cached and assembled (6 shots of the interim v12 mute). Fresh renders and cache hits must now be
checked frame-by-frame against their source neighbours and fail closed (RifeBadOutput); a poisoned cache
entry is moved to <cache>/bad and re-rendered, never reused."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hfvg import interp_rife
from tests.test_picture_assembly_round3 import MOVING, make_clip, needs_ffmpeg
from tests.test_r12_gaps import fake_rife  # noqa: F401  (fixture)

BROKEN = str(Path(__file__).with_name("fake_rife_broken.py"))


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _img(path, arr):
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(path)
    return path


def _frames(tmp_path, n=4):
    """Moving gradient frames (a bar moving right) as PNGs."""
    ins = []
    for j in range(n):
        a = np.full((160, 90, 3), 40.0)
        a[:, 10 + 3 * j:30 + 3 * j] = 220.0
        a[40:60, :, 1] = 30 + 5 * j
        ins.append(_img(tmp_path / f"in{j}.png", a))
    return ins


def test_true_in_betweens_pass(tmp_path):
    ins = _frames(tmp_path)
    outs = []
    for i in range(7):                       # step 0.5: blends of neighbours
        t = i * 0.5
        j = int(t)
        a = np.asarray(Image.open(ins[j]), dtype=np.float32)
        b = np.asarray(Image.open(ins[min(j + 1, 3)]), dtype=np.float32)
        f = t - j
        outs.append(_img(tmp_path / f"o{i}.png", (1 - f) * a + f * b))
    r = interp_rife.check_interp_frames(outs, ins, 0.5, "seg")
    assert r["checked"] == 7


def test_smeared_or_blank_frame_fails_even_if_frame0_is_fine(tmp_path):
    ins = _frames(tmp_path)
    good = [ins[0], ins[1]]
    smear = np.repeat(np.asarray(Image.open(ins[1]), dtype=np.float32).mean(axis=1, keepdims=True), 90, axis=1)
    for bad in (_img(tmp_path / "smear.png", 255 - smear), _img(tmp_path / "blank.png", np.zeros((160, 90, 3)))):
        with pytest.raises(interp_rife.RifeBadOutput, match="frame 2"):
            interp_rife.check_interp_frames(good + [bad], ins, 1.0, "seg")


def test_bound_scales_with_real_motion_but_not_past_it(tmp_path):
    ins = _frames(tmp_path)
    assert interp_rife.CHECK_FLOOR <= 8.0 and interp_rife.CHECK_PAIR_FACTOR <= 2.0
    with pytest.raises(interp_rife.RifeBadOutput):
        interp_rife.check_interp_frames([], ins, 1.0, "seg")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_broken_rife_exit0_fails_closed_and_caches_nothing(tmp_path, fake_rife, monkeypatch):  # noqa: F811
    from hfvg.activities.media import _run
    monkeypatch.setenv("RIFE_NCNN_BIN", BROKEN)
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)
    with pytest.raises(interp_rife.RifeBadOutput, match="vkCreateInstance"):
        await interp_rife.rife_retime(str(clip), _sha(clip), [[0.0, 1.5]], 60, 24, _run, None)
    cache = tmp_path / "rifecache"
    assert not list(cache.glob("*.mp4")) and not list(cache.glob("*.json"))


@needs_ffmpeg
@pytest.mark.asyncio
async def test_poisoned_cache_entry_is_moved_aside_and_rerendered(tmp_path, fake_rife):  # noqa: F811
    from hfvg.activities.media import _run
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)
    sha = _sha(clip)
    first = await interp_rife.rife_retime(str(clip), sha, [[0.0, 1.5]], 60, 24, _run, None)
    assert not first["cached"]
    again = await interp_rife.rife_retime(str(clip), sha, [[0.0, 1.5]], 60, 24, _run, None)
    assert again["cached"]                                   # a good entry is still reused
    mezz = Path(first["path"])
    meta = mezz.with_suffix(".json")
    good_size = mezz.stat().st_size
    # poison: same frame count, smeared content, meta size patched so the old check would accept it
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mezz), "-vf", "negate,boxblur=10:5",
                    "-c:v", "libx264", "-crf", "10", "-pix_fmt", "yuv420p", str(tmp_path / "p.mp4")], check=True)
    os.replace(tmp_path / "p.mp4", mezz)
    m = json.loads(meta.read_text())
    m["size_bytes"] = mezz.stat().st_size
    meta.write_text(json.dumps(m))
    r = await interp_rife.rife_retime(str(clip), sha, [[0.0, 1.5]], 60, 24, _run, None)
    assert not r["cached"] and r["key"] == first["key"]
    bad = mezz.parent / "bad"
    assert (bad / mezz.name).is_file() and (bad / f"{first['key']}.reason.txt").is_file()
    assert abs(Path(r["path"]).stat().st_size - good_size) < good_size * 0.05


def test_cache_entry_without_per_segment_is_not_trusted(tmp_path):
    import asyncio
    with pytest.raises(interp_rife.RifeBadOutput):
        asyncio.run(interp_rife.verify_cached(tmp_path / "x.mp4", tmp_path / "s.mp4", [], None))


def test_media_maps_bad_render_to_nonretryable_error():
    src = Path(interp_rife.__file__).with_name("activities").joinpath("media.py").read_text()
    assert 'type="RifeBadOutput", non_retryable=True' in src and "except RifeBadOutput" in src


@needs_ffmpeg
@pytest.mark.asyncio
async def test_gap82_frame_check_does_not_block_the_event_loop(tmp_path, fake_rife, monkeypatch):  # noqa: F811
    """GAP-82 (r14): the GAP-81 check is CPU-bound (hundreds of 1080x1920 PNGs); run inline it starved
    the activity heartbeat and activity 72 died with 'Heartbeat timeout'. It must run off the loop."""
    import asyncio
    import time
    from hfvg.activities.media import _run
    real = interp_rife.check_interp_frames

    def slow(*a, **k):
        time.sleep(0.6)
        return real(*a, **k)
    monkeypatch.setattr(interp_rife, "check_interp_frames", slow)
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)
    ticks, gaps = [], []

    async def ticker():
        last = time.monotonic()
        while True:
            await asyncio.sleep(0.05)
            now = time.monotonic()
            gaps.append(now - last)
            last = now
    t = asyncio.create_task(ticker())
    cached = []
    for _ in range(2):                       # fresh render, then a verified cache hit
        r = await interp_rife.rife_retime(str(clip), _sha(clip), [[0.0, 1.5]], 60, 24, _run, None)
        cached.append(r["cached"])
        await asyncio.sleep(0.15)                # let the ticker record the gap
        assert max(gaps) < 0.45, cached
    t.cancel()
    assert cached == [False, True]
