"""GAP-30: SFX beds can hold their VO duck for the whole line window.

The default ("activity") duck follows the VO signal, so a bed swells back up in every pause
inside a spoken line; a reviewer measuring VO vs background over the line window then sees
the bed at full level for part of the line. duck_mode "window" holds the duck across the line
(the same curve as the music bed). The default is unchanged.
"""
import json
import os
import shutil
import subprocess

import pytest

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError, mix_and_deliver, normalize_plan
from tests.test_audio_delivery import FAST_ENCODE, TEST_SIZE, make_noise, make_picture, make_tone

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")


def _line_with_pause(path, tone_s=0.5, pause_s=1.0):
    a, b = path.with_suffix(".a.wav"), path.with_suffix(".b.wav")
    make_tone(a, tone_s, 300, -6.0)
    make_tone(b, tone_s, 300, -6.0)
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(a), "-i", str(b), "-filter_complex",
                    f"[0]apad=pad_dur={pause_s}[p];[p][1]concat=n=2:v=0:a=1", "-ac", "2", str(path)], check=True)
    return path


def _episode(tmp_path, ep, mode):
    root = tmp_path / "data" / "episodes" / ep
    make_picture(root / "edit" / "mute_assembly.mp4", 6.0, size=TEST_SIZE)
    _line_with_pause(root / "audio" / "vo" / "L01.wav")          # 0.5 s tone, 1.0 s pause, 0.5 s tone
    make_noise(root / "audio" / "lib" / "bed.wav", 6.0, -18.0)
    bed = {"id": "B01", "at": 0.0, "dur": 6.0, "path": "audio/lib/bed.wav", "loop": True,
           "level_db": -24.0, "duck_db": -12.0, "fade_in_s": 0.0, "fade_out_s": 0.0}
    if mode:
        bed["duck_mode"] = mode
    raw = {"vo": {"voice_id": "v", "lines": [{"id": "L01", "at": 1.0, "path": "audio/vo/L01.wav", "text": "One."}]},
           "sfx": [bed], **FAST_ENCODE}
    (root / "audio" / "audio_plan.json").write_text(json.dumps(raw))
    return root / "edit" / "mute_assembly.mp4", audio_mix.load_plan_file(root / "audio" / "audio_plan.json")


async def _pause_vs_clear(tmp_path, monkeypatch, ep, mode):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    mute, plan = _episode(tmp_path, ep, mode)
    m = await mix_and_deliver(ep, str(mute), plan)
    sfx = await audio_mix.decode(m["paths"]["sfx_stem"])
    sr = audio_mix.SR
    pause = audio_mix.rms_db(sfx[int(1.8 * sr):int(2.2 * sr)])   # inside the line, in its pause
    clear = audio_mix.rms_db(sfx[int(4.5 * sr):int(5.5 * sr)])   # no VO
    return m, clear - pause


def test_duck_mode_is_validated(tmp_path):
    base = {"vo": {"voice_id": "v", "lines": [{"id": "L01", "at": 0.5, "text": "x"}]},
            "sfx": [{"id": "S", "at": 0, "dur": 1, "path": "x.wav", "duck_db": -6}]}
    assert normalize_plan(base, tmp_path)["sfx"][0]["duck_mode"] == "activity"   # default unchanged
    base["sfx"][0]["duck_mode"] = "window"
    assert normalize_plan(base, tmp_path)["sfx"][0]["duck_mode"] == "window"
    base["sfx"][0]["duck_mode"] = "sidechain"
    with pytest.raises(AudioInputError):
        normalize_plan(base, tmp_path)


@pytest.mark.timeout(240)
async def test_window_duck_holds_through_pauses(tmp_path, monkeypatch):
    m, drop = await _pause_vs_clear(tmp_path, monkeypatch, "ep01", "window")
    assert abs(drop - 12.0) < 1.5, drop
    assert m["sfx"][0]["duck_mode"] == "window"


@pytest.mark.timeout(240)
async def test_activity_duck_releases_in_pauses(tmp_path, monkeypatch):
    _, drop = await _pause_vs_clear(tmp_path, monkeypatch, "ep02", None)
    assert drop < 3.0, drop
