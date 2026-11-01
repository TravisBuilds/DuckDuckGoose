"""GAP-71 (ep05v2 r12 v10): a RIFE segment that ends at the clip end has no frame past it. rife-ncnn
holds the last input for output times past it, and rife_request asked n = round(k*n_out/m), so the last
kept output(s) landed past the last input = a duplicated (held) frame. V11 v10 [2.2, 4.04] of a 97-frame
clip: 44 inputs, -n 83, output 82 at t=43.5 > 43 -> the last frame before the V11->V12 cut was held.
Fix: for a clip-end segment ask n >= (n_out-1)*k/(k-1); the cache key changes ONLY for such renders."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hfvg import interp_rife
from tests.test_picture_assembly_round3 import MOVING, make_clip, needs_ffmpeg
from tests.test_r12_gaps import fake_rife  # noqa: F401  (fixture)


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_clip_end_segment_never_maps_past_the_last_input():
    for m in range(2, 130, 3):
        for n_out in range(m, 3 * m + 7, 5):
            k, n = interp_rife.rife_request(m, n_out, False)
            assert k == m and n >= n_out
            assert (n_out - 1) * k / n <= k - 1 + 1e-9, (m, n_out, k, n)
            # still the wanted speed (within one output frame of the segment's length)
            assert abs(n - n_out) <= max(2, n_out // (k - 1) + 1)
    # the V11 v10 case
    k, n = interp_rife.rife_request(44, 83, False)
    assert (k, n) == (44, 84) and 82 * k / n <= 43


def test_has_next_segments_are_unchanged():
    # interior segments keep the GAP-61 mapping (and so their cached renders)
    assert interp_rife.rife_request(72, 120, True) == (73, 122)
    assert interp_rife.rife_request(24, 24, True) == (25, 25)
    assert interp_rife.rife_request(1, 10, False) == (1, 10)


def test_cache_key_unchanged_unless_clip_end():
    sha, segs = "c" * 64, [[0.0, 1.0]]
    old = hashlib.sha256(json.dumps({"v": interp_rife.RIFE_FORMAT_VERSION, "sha": sha, "segs": [[0.0, 1.0]],
                                     "frames": 48, "fps": 24, "model": "rife-v4.6"}, sort_keys=True).encode()).hexdigest()
    assert interp_rife.cache_key(sha, segs, 48, 24, "/m/rife-v4.6") == old
    assert interp_rife.cache_key(sha, segs, 48, 24, "/m/rife-v4.6", clip_end=True) != old


@needs_ffmpeg
@pytest.mark.asyncio
async def test_clip_end_retime_has_no_held_tail_and_new_key(tmp_path, fake_rife):  # noqa: F811
    from hfvg.activities.media import _run
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)   # 48 source frames
    inner = await interp_rife.rife_retime(str(clip), _sha(clip), [[0.0, 1.5]], 60, 24, _run, None)
    end = await interp_rife.rife_retime(str(clip), _sha(clip), [[0.5, 2.0]], 60, 24, _run, None)
    assert inner["key"] == interp_rife.cache_key(_sha(clip), [[0.0, 1.5]], 60, 24, "rife-v4.6")
    assert end["key"] == interp_rife.cache_key(_sha(clip), [[0.5, 2.0]], 60, 24, "rife-v4.6", clip_end=True)
    calls = [tuple(map(int, l.split())) for l in fake_rife.read_text().split("\n") if l]
    k, n = calls[-1]
    assert k == 36 and (60 - 1) * k / n <= k - 1      # the last kept output is a real source moment
    assert end["per_segment"][0]["rife_n"] == n and end["per_segment"][0]["frames"] == 60
