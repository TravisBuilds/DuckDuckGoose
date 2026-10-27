"""GAP-20 (ep05full 2026-10-27): the TP guard only checked the PCM mix (-2.4 dBTP), but the AAC
encodes overshot it (192 kbps master -1.55 dBTP, 64 kbps phone +0.13 dBTP) and delivery QC failed a
-2.0 dBTP ceiling. codec_safe_pcm test-encodes at each deliverable's bitrate and pre-limits the PCM
(with makeup back to the loudness target) until the ENCODED file meets the ceiling."""

from __future__ import annotations

import pytest

from hfvg.audio_mix import codec_safe_pcm, delivery_qc, measure_loudness, mix_and_deliver
from tests.test_audio_delivery import _ff, _ok, episode, pytestmark  # noqa: F401

LOUD = {"target_lufs": -12.6, "tolerance_lu": 0.5, "tp_ceiling_dbtp": -2.0, "tp_margin_db": 0.5, "lra": 11}


def _dense_mix(path):
    # dense, peak-limited material (crest ~3.5 dB): sample peaks at -2.5 dBFS, but a 64 kbps AAC
    # encode overshoots well past -2.0 dBTP, as the ep05full mix did
    _ff("-f", "lavfi", "-i", "anoisesrc=d=8:c=pink:a=0.3:seed=7", "-f", "lavfi",
        "-i", "sine=f=3100:d=8", "-filter_complex",
        "[0][1]amix=inputs=2:normalize=0,aformat=sample_rates=48000:channel_layouts=stereo,"
        "volume=11dB,alimiter=limit=0.75:attack=1:release=50:level=disabled",
        "-ar", "48000", "-c:a", "pcm_s24le", str(path))


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_low_bitrate_encode_meets_tp_ceiling(tmp_path):
    src = tmp_path / "mix.wav"
    _dense_mix(src)
    pcm_meas = await measure_loudness(str(src), LOUD)
    assert pcm_meas["true_peak_dbtp"] <= LOUD["tp_ceiling_dbtp"], "the PCM itself meets the ceiling"
    loud = {**LOUD, "target_lufs": round(pcm_meas["integrated_lufs"], 1)}
    pcm, info = await codec_safe_pcm(str(src), 64, loud, str(tmp_path), "phone")
    assert info["encoded_tp_unlimited"] > LOUD["tp_ceiling_dbtp"], "fixture must reproduce the AAC overshoot"
    assert info["ok"] and pcm != str(src), info
    _ff("-i", pcm, "-c:a", "aac", "-b:a", "64k", "-ar", "48000", str(tmp_path / "check.m4a"))
    m = await measure_loudness(str(tmp_path / "check.m4a"), loud)
    assert m["true_peak_dbtp"] <= loud["tp_ceiling_dbtp"]
    assert abs(m["integrated_lufs"] - loud["target_lufs"]) <= loud["tolerance_lu"]


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_clean_encode_is_left_alone(tmp_path):
    src = tmp_path / "quiet.wav"
    _ff("-f", "lavfi", "-i", "sine=f=440:d=4", "-af", "volume=-20dB", "-ar", "48000", "-ac", "2",
        "-c:a", "pcm_s24le", str(src))
    loud = {**LOUD, "target_lufs": -23.0, "tolerance_lu": 3.0}
    pcm, info = await codec_safe_pcm(str(src), 192, loud, str(tmp_path), "master")
    assert pcm == str(src) and info["limit_dbfs"] is None


@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_delivered_files_meet_tp_ceiling(tmp_path):
    mute, plan = episode(tmp_path, seconds=8.0)
    plan["loudness"] = dict(LOUD)
    plan["phone"] = {**plan["phone"], "audio_kbps": 48}
    m = await mix_and_deliver("ep01", str(mute), plan)
    qc = await delivery_qc(m, plan)
    for name in ("nosubs_true_peak", "phone_true_peak"):
        assert _ok(qc, name), next(c for c in qc["checks"] if c["name"] == name)
    assert "codec_tp_guard" in m
