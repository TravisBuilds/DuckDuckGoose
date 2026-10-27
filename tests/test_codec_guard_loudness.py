"""GAP-31: the codec TP guard also fixes an encode that meets the TP ceiling but sits under the
loudness band (loudnorm under-shoots when its own TP limit caps the gain). Previously the guard
returned early on TP alone and the master shipped at -14.0 LUFS against a -12.6 target.
Gross level errors (more than MAX_LOUDNESS_RESTORE_LU off) are still left to delivery QC
(test_codec_tp_guard.py::test_clean_encode_is_left_alone)."""
import os
import shutil
import subprocess

import pytest

from hfvg import audio_mix

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")

LOUD = {**audio_mix.DEFAULT_LOUDNESS, "target_lufs": -12.6, "tolerance_lu": 0.5, "tp_ceiling_dbtp": -2.0}


def _peaky_quiet_mix(path):
    # pink noise with sparse clicks: ~-14.1 LUFS (1.5 LU under -12.6, like a past run) with TP well under -2 dBTP
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:duration=12:seed=3",
                    "-f", "lavfi", "-i", "sine=frequency=2000:sample_rate=48000:duration=12",
                    "-filter_complex",
                    "[0]volume=1dB[n];[1]volume='if(lt(mod(t,1),0.02),1,0)':eval=frame,volume=-3dB[k];"
                    "[n][k]amix=inputs=2:normalize=0,alimiter=limit=0.70:level=disabled",
                    "-ac", "2", "-c:a", "pcm_s24le", str(path)], check=True)
    return str(path)


@pytest.mark.timeout(240)
async def test_guard_restores_loudness_when_tp_already_passes(tmp_path):
    mix = _peaky_quiet_mix(tmp_path / "mix.wav")
    m0 = await audio_mix.measure_loudness(mix, LOUD)
    assert m0["true_peak_dbtp"] <= LOUD["tp_ceiling_dbtp"] - 0.1          # TP alone would pass
    assert m0["integrated_lufs"] < LOUD["target_lufs"] - LOUD["tolerance_lu"]  # but it is too quiet
    assert LOUD["target_lufs"] - m0["integrated_lufs"] <= audio_mix.MAX_LOUDNESS_RESTORE_LU  # a small miss
    pcm, info = await audio_mix.codec_safe_pcm(mix, 192, LOUD, str(tmp_path), "master")
    assert pcm != mix and info["tries"], info
    assert info["ok"] is True, info
    enc = info["encoded"]
    assert abs(enc["integrated_lufs"] - LOUD["target_lufs"]) <= LOUD["tolerance_lu"], enc
    assert enc["true_peak_dbtp"] <= LOUD["tp_ceiling_dbtp"], enc


@pytest.mark.timeout(240)
async def test_guard_leaves_a_passing_mix_alone(tmp_path):
    ok = str(tmp_path / "ok.wav")   # in band after AAC (~-12.6 LUFS) with true peak far under the ceiling
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y",
                    "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:duration=12:seed=3",
                    "-af", "volume=9dB,alimiter=limit=0.6:level=disabled", "-ac", "2", "-c:a", "pcm_s24le", ok],
                   check=True)
    m1 = await audio_mix.measure_loudness(ok, LOUD)
    assert abs(m1["integrated_lufs"] - LOUD["target_lufs"]) <= LOUD["tolerance_lu"] - 0.1, m1
    assert m1["true_peak_dbtp"] <= LOUD["tp_ceiling_dbtp"] - 1.0, m1
    pcm, info = await audio_mix.codec_safe_pcm(ok, 192, LOUD, str(tmp_path), "master")
    assert pcm == ok and info["tries"] == [] and info["ok"] is True, info
