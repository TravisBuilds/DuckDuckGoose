"""GAP-34 bed_dips and GAP-35 per-line subtitle_dur (ep05v2 G3 round 2, Studio item 9).

Studio flagged two bed bursts (music swell + a lapping transient) that sit above the bed level in
VO gaps, and a subtitle that hangs ~1.8 s after its line because the locked VO file carries room
tone. The plan can now dip the music+SFX buses over a window (VO untouched) and end a cue with the
spoken words; the mix refuses a subtitle_dur that would cut the words."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pytest

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError, delivery_qc, dip_envelope, mix_and_deliver, validate_bed_dips
from tests.test_audio_delivery import _ok, episode, pytestmark  # noqa: F401


def test_delivery_version_bumped():
    assert audio_mix.DELIVERY_VERSION == 6  # GAP-60 (r11) picture sync guard; 5 was GAP-55 (r9) CFR + frame-count QC


def test_bed_dips_validation_and_envelope():
    with pytest.raises(AudioInputError):
        validate_bed_dips([{"start": 2.0, "end": 2.0, "gain_db": -6}])
    with pytest.raises(AudioInputError):
        validate_bed_dips([{"start": 1.0, "end": 2.0, "gain_db": 3}])  # a dip never boosts
    with pytest.raises(AudioInputError):
        validate_bed_dips([{"start": 1.0, "end": 2.0}])  # gain is required
    dips = validate_bed_dips([{"start": 1.0, "end": 2.0, "gain_db": -12, "ramp_s": 0.2}])
    sr = audio_mix.SR
    g = dip_envelope(3 * sr, dips)
    assert g[int(0.5 * sr)] == pytest.approx(1.0)                      # before the ramp
    assert 20 * np.log10(g[int(1.5 * sr)]) == pytest.approx(-12, abs=0.01)  # inside
    assert 1e-3 < 1 - g[int(0.9 * sr)] < 1 - 10 ** (-12 / 20)          # mid-ramp, partial
    assert g[int(2.5 * sr)] == pytest.approx(1.0)                      # after


def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *a], check=True)


@pytest.mark.timeout(300)
async def test_bed_dip_lowers_music_and_sfx_not_vo(tmp_path):
    mute, plan = episode(tmp_path)
    base = await mix_and_deliver("ep01", str(mute), plan)
    sr = audio_mix.SR
    m0 = await audio_mix.decode(base["paths"]["music_stem"])
    s0 = await audio_mix.decode(base["paths"]["sfx_stem"])
    v0 = await audio_mix.decode(base["paths"]["vo_stem"])
    dipped = {**plan, "bed_dips": validate_bed_dips([{"start": 4.0, "end": 4.9, "gain_db": -10}])}
    m = await mix_and_deliver("ep01", str(mute), dipped)
    assert m["reused"] is False and m["input_key"] != base["input_key"]
    assert m["bed_dips"] and m["bed_dips"][0]["gain_db"] == -10
    m1 = await audio_mix.decode(m["paths"]["music_stem"])
    s1 = await audio_mix.decode(m["paths"]["sfx_stem"])
    v1 = await audio_mix.decode(m["paths"]["vo_stem"])
    w = slice(int(4.2 * sr), int(4.8 * sr))   # music clear of VO, S01 hit at 4.3-4.9
    assert audio_mix.rms_db(m0[w]) - audio_mix.rms_db(m1[w]) == pytest.approx(10, abs=0.6)
    assert audio_mix.rms_db(s0[w]) - audio_mix.rms_db(s1[w]) == pytest.approx(10, abs=0.6)
    out = slice(int(2.6 * sr), int(3.4 * sr))  # outside the dip: untouched
    assert audio_mix.rms_db(m0[out]) == pytest.approx(audio_mix.rms_db(m1[out]), abs=0.05)
    assert np.max(np.abs(v0 - v1)) < 1e-4    # VO never dipped
    qc = await delivery_qc(m, dipped)
    assert qc["verdict"] == "PASS", qc["failed"]


@pytest.mark.timeout(300)
async def test_subtitle_dur_ends_cue_with_the_words(tmp_path):
    mute, plan = episode(tmp_path)
    vo = Path(plan["vo"]["lines"][0]["path"])
    # 0.7 s of speech-like tone, then 0.5 s of room tone (-84 dB) = a 1.2 s locked file
    _ff("-f", "lavfi", "-i", "sine=frequency=300:sample_rate=48000:duration=0.7",
        "-f", "lavfi", "-i", "anoisesrc=color=pink:sample_rate=48000:duration=0.5:seed=3",
        "-filter_complex", "[0:a]volume=-4dB[a];[1:a]volume=-84dB[b];[a][b]concat=n=2:v=0:a=1,"
        "aformat=channel_layouts=stereo", str(vo))
    lines = [{**plan["vo"]["lines"][0], "subtitle_dur": 0.8}, plan["vo"]["lines"][1]]
    p = {**plan, "vo": {**plan["vo"], "lines": lines}}
    m = await mix_and_deliver("ep01", str(mute), p)
    c = {x["id"]: x for x in m["cues"]}
    assert c["L01"]["end"] == pytest.approx(0.5 + 0.8, abs=0.01)
    assert c["L02"]["end"] == pytest.approx(2.5 + 1.0, abs=0.05)   # unset: whole line
    found = audio_mix.parse_ass_cues(m["paths"]["ass"])
    assert found[0][1] == pytest.approx(1.3, abs=0.02)
    l01 = next(x for x in m["vo_lines"] if x["id"] == "L01")
    assert l01["speech_end_s"] == pytest.approx(0.7, abs=0.05)
    qc = await delivery_qc(m, p)
    assert _ok(qc, "subtitle_timing"), qc["failed"]
    # a cue that would cut the words is refused before anything is published
    lines_bad = [{**plan["vo"]["lines"][0], "subtitle_dur": 0.4}, plan["vo"]["lines"][1]]
    with pytest.raises(AudioInputError, match="ends before the speech"):
        await mix_and_deliver("ep01", str(mute), {**plan, "vo": {**plan["vo"], "lines": lines_bad}})


def test_plan_file_parses_subtitle_dur_and_dips(tmp_path):
    raw = {"vo": {"lines": [{"id": "L01", "at": 1.0, "path": "a.wav", "subtitle_dur": 3.1}]},
           "bed_dips": [{"start": 101.0, "end": 101.8, "gain_db": -8, "note": "music swell"}]}
    pl = audio_mix.normalize_plan(raw, tmp_path)
    assert pl["vo"]["lines"][0]["subtitle_dur"] == 3.1
    assert pl["bed_dips"] == [{"start": 101.0, "end": 101.8, "gain_db": -8.0, "ramp_s": 0.15,
                               "note": "music swell"}]
    with pytest.raises(AudioInputError):
        audio_mix.normalize_plan({"vo": {"lines": [{"id": "L01", "at": 1, "path": "a.wav",
                                                     "subtitle_dur": 0}]}}, tmp_path)


def test_bed_dips_follow_the_edit_remap():
    ref = {"total_s": 8.0, "clips": [{"id": "A", "start": 0.0, "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0},
                                     {"id": "B", "start": 4.0, "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0}]}
    cur = {"total_s": 7.0, "clips": [{"id": "A", "start": 0.0, "seg_s": 3.0, "trim_in": 0.0, "trim_out": 3.0},
                                     {"id": "B", "start": 3.0, "seg_s": 4.0, "trim_in": 0.0, "trim_out": 4.0}]}
    plan = {"vo": {"lines": []}, "sfx": [], "music": None, "protected": [], "timeline_ref": ref,
            "bed_dips": [{"start": 5.0, "end": 5.5, "gain_db": -8.0, "ramp_s": 0.15, "note": ""}]}
    out = audio_mix.remap_plan_to_edit(plan, cur)
    assert out["bed_dips"][0]["start"] == pytest.approx(4.0) and out["bed_dips"][0]["end"] == pytest.approx(4.5)
