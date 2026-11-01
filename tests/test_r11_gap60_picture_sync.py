"""GAP-60 (r11): a loaded worker encoded picture_logo with frames 591-596 frozen on mute frame 590
(same frame count, CFR, so GAP-55 passed). Every re-encoded picture (picture_logo -> nosubs, burned
subs, phone) is now checked frame by frame against the mute assembly, re-encoded on a mismatch and
failed closed after PICTURE_SYNC_TRIES.

Real ffmpeg on small synthetic pictures; no provider calls."""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from hfvg import audio_mix
from hfvg.audio_mix import PictureSyncError, grey_frames, misaligned_frames, mix_and_deliver

from tests.test_r9_gap55_delivery_frames import FPS, FRAMES, SECONDS, logo_episode, pytestmark  # noqa: F401


def freeze(src: str, dst: str, first: int = 60, last: int = 65, keep: int = 59) -> None:
    """Re-encode src with frames first..last showing frame keep (the r11 failure: count unchanged)."""
    tmp = dst + ".tmp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src, "-map", "0:a?",
                    "-filter_complex", f"[0:v]split[a][b];[a][b]freezeframes=first={first}:last={last}:replace={keep}[v]",
                    "-map", "[v]",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy", tmp],
                   check=True)
    Path(tmp).replace(dst)


def ramp(n=40, h=24, w=16, step=6):
    return np.stack([np.full((h, w), (i * step) % 250, np.uint8) for i in range(n)])


# ------------------------------------------------------------------ detector (unit)

def test_identical_picture_is_aligned():
    r = ramp()
    assert misaligned_frames(r, r.copy()) == []


def test_freeze_is_reported_with_the_frame_it_shows():
    r = ramp()
    p = r.copy()
    p[20:24] = r[19]
    bad = misaligned_frames(r, p)
    assert bad == [(20, 19), (21, 19), (22, 19), (23, 20)]  # 23 is 4 from 19 (radius 3): nearest is 20


def test_shift_is_reported():
    r = ramp()
    p = r.copy()
    p[10:30] = r[11:31]
    bad = misaligned_frames(r, p)
    assert (10, 11) in bad and len(bad) == 20


def test_static_hold_and_codec_lag_are_not_flagged():
    r = ramp()
    r[30:36] = r[29]  # a real hold in the mute (identical frames)
    p = r.copy()
    p[31:35] = r[29]
    assert misaligned_frames(r, p) == []
    # codec lag after a cut: picture only ~1.1x closer to the previous frame than to its own
    r2 = ramp(step=2)
    p2 = r2.astype(np.int16).copy()
    p2[10] = r2[10].astype(np.int16) - 1  # own 1.0, previous frame 1.0 away -> tie, not a win
    assert misaligned_frames(r2, p2.clip(0, 255).astype(np.uint8)) == []


def test_upto_limits_the_check_and_short_picture_fails():
    r = ramp()
    p = r.copy()
    p[35:38] = r[34]
    assert misaligned_frames(r, p, upto=30) == []
    assert misaligned_frames(r, p) != []
    assert misaligned_frames(r, p[:20], upto=30) == [(20, -1)]


# ------------------------------------------------------------------ delivery (integration)

@pytest.mark.timeout(300)
async def test_delivery_records_the_sync_check_for_every_picture(tmp_path):
    mute, plan = logo_episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    ps = m["picture_sync"]
    assert ps["reference"] == str(mute)
    assert ps["checked_frames"] == int((SECONDS - plan["logo"]["visible_s"]) * FPS)
    whats = [e["what"] for e in ps["encodes"]]
    assert whats[0] == "picture_logo" and "subs" in whats and any(w.startswith("phone@") for w in whats)
    assert all(e["attempts"][-1]["misaligned"] == 0 for e in ps["encodes"])
    ref = await grey_frames(mute, ps["checked_frames"])
    for name in ("nosubs",):
        assert misaligned_frames(ref, await grey_frames(m["outputs"][name]["path"], ps["checked_frames"])) == []


@pytest.mark.timeout(300)
async def test_frozen_overlay_is_reencoded(tmp_path, monkeypatch):
    mute, plan = logo_episode(tmp_path)
    real = audio_mix.overlay_logo
    calls = []

    async def flaky(src, logo, total_s, out, master, start=None, frames=None):
        await real(src, logo, total_s, out, master, start=start, frames=frames)
        calls.append(out)
        if len(calls) == 1:
            freeze(out, out)

    monkeypatch.setattr(audio_mix, "overlay_logo", flaky)
    m = await mix_and_deliver("ep01", str(mute), plan)
    assert len(calls) == 2
    logo_enc = m["picture_sync"]["encodes"][0]
    assert logo_enc["what"] == "picture_logo"
    assert [a["misaligned"] > 0 for a in logo_enc["attempts"]] == [True, False]
    assert logo_enc["attempts"][0]["frames"][0] == [60, 59]
    ref = await grey_frames(mute, m["picture_sync"]["checked_frames"])
    got = await grey_frames(m["outputs"]["nosubs"]["path"], m["picture_sync"]["checked_frames"])
    assert misaligned_frames(ref, got) == []


@pytest.mark.timeout(400)
async def test_overlay_that_keeps_freezing_fails_closed(tmp_path, monkeypatch):
    mute, plan = logo_episode(tmp_path)
    real = audio_mix.overlay_logo
    calls = []

    async def frozen(src, logo, total_s, out, master, start=None, frames=None):
        await real(src, logo, total_s, out, master, start=start, frames=frames)
        calls.append(out)
        freeze(out, out)

    monkeypatch.setattr(audio_mix, "overlay_logo", frozen)
    with pytest.raises(PictureSyncError, match="GAP-60: picture_logo"):
        await mix_and_deliver("ep01", str(mute), plan)
    assert len(calls) == audio_mix.PICTURE_SYNC_TRIES
    assert not Path(audio_mix.delivery_paths("ep01")["manifest"]).exists()


@pytest.mark.timeout(400)
async def test_frozen_phone_fails_closed(tmp_path, monkeypatch):
    mute, plan = logo_episode(tmp_path)
    real = audio_mix.encode_phone

    async def frozen(mute_, wav, ass, out, *a, **k):
        await real(mute_, wav, ass, out, *a, **k)
        freeze(out, out)

    monkeypatch.setattr(audio_mix, "encode_phone", frozen)
    with pytest.raises(PictureSyncError, match="GAP-60: phone@"):
        await mix_and_deliver("ep01", str(mute), plan)
    assert not Path(audio_mix.delivery_paths("ep01")["manifest"]).exists()


@pytest.mark.timeout(400)
async def test_frozen_subtitle_burn_fails_closed(tmp_path, monkeypatch):
    mute, plan = logo_episode(tmp_path)
    real = audio_mix.run_cmd

    async def run(cmd, what, cwd=None, **k):
        out = await real(cmd, what, cwd=cwd, **k)
        if what == "burn subtitles":
            freeze(cmd[-1], cmd[-1])
        return out

    monkeypatch.setattr(audio_mix, "run_cmd", run)
    with pytest.raises(PictureSyncError, match="GAP-60: subs"):
        await mix_and_deliver("ep01", str(mute), plan)


def test_low_bitrate_lag_after_a_cut_is_not_a_freeze():
    """The v9 phone (228 kbps): right after a cut the encode sits ~1.1x closer to the previous
    frame than to its own (codec lag, not a freeze). Only a clear win (>= PICTURE_SYNC_RATIO) counts."""
    rng = np.random.default_rng(7)
    base = rng.integers(40, 200, (48, 32)).astype(np.int16)
    ref = []
    for i in range(12):
        f = base.copy()
        f[: 4 * i] += 8  # each frame brightens a few more rows (slow motion)
        ref.append(f)
    ref = np.stack(ref).clip(0, 255).astype(np.uint8)
    pic = ref.copy()
    noise = rng.integers(-3, 4, (48, 32))
    pic[6] = (ref[5].astype(np.int16) + noise).clip(0, 255).astype(np.uint8)
    own = np.abs(pic[6].astype(np.int16) - ref[6]).mean()
    prev = np.abs(pic[6].astype(np.int16) - ref[5]).mean()
    assert prev + audio_mix.PICTURE_SYNC_TOL < own < prev * audio_mix.PICTURE_SYNC_RATIO  # the lag case
    assert misaligned_frames(ref, pic) == []
    pic[6] = ref[5]  # an exact repeat is a freeze
    assert misaligned_frames(ref, pic) == [(6, 5)]
