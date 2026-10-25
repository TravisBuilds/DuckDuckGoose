"""Round 4 audio + delivery (G-AUD-1b): real ffmpeg mixes on synthetic tones and clips.

No provider calls: live paths use respx (ElevenLabs REST) or locked/library files."""

import json
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from hfvg import audio_mix
from hfvg.audio_mix import (
    AudioInputError,
    LoudnormParseError,
    PlacementError,
    delivery_qc,
    mix_and_deliver,
    normalize_plan,
    parse_loudnorm_json,
    validate_min_gap,
    validate_placement,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def make_picture(path: Path, seconds: float = 6.0, size: str = "1080x1920", rate: int = 30) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ff("-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an", str(path))
    return path


def make_tone(path: Path, seconds: float, freq: int = 440, volume_db: float = -12.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ff("-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
        "-af", f"volume={volume_db}dB", "-ac", "2", str(path))
    return path


def make_noise(path: Path, seconds: float, volume_db: float = -20.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ff("-f", "lavfi", "-i", f"anoisesrc=color=pink:sample_rate=48000:duration={seconds}:seed=7",
        "-af", f"volume={volume_db}dB", "-ac", "2", str(path))
    return path


def episode(tmp_path: Path, ep: str = "ep01", seconds: float = 6.0, vo_db=(-4.0, -12.0)) -> tuple[Path, dict]:
    root = Path(os.environ.get("EPISODES_DIR", tmp_path / "data" / "episodes")) / ep
    mute = make_picture(root / "edit" / "mute_assembly.mp4", seconds)
    make_tone(root / "audio" / "vo" / "L01.wav", 1.2, 300, vo_db[0])
    make_tone(root / "audio" / "vo" / "L02.wav", 1.0, 360, vo_db[1])
    make_noise(root / "audio" / "lib" / "bed.wav", 4.0, -18.0)
    make_noise(root / "audio" / "lib" / "hit.wav", 0.6, -10.0)
    raw = {
        "min_vo_gap_s": 0.4,
        "vo": {"voice_id": "voice-x", "lines": [
            {"id": "L01", "at": 0.5, "path": "audio/vo/L01.wav", "text": "First line here."},
            {"id": "L02", "at": 2.5, "path": "audio/vo/L02.wav", "text": "Second line."},
        ]},
        "music": {"path": "audio/lib/bed.wav", "loop": True},
        "sfx": [{"id": "S01", "at": 4.3, "dur": 0.6, "path": "audio/lib/hit.wav"}],
    }
    (root / "audio" / "audio_plan.json").write_text(json.dumps(raw))
    return mute, audio_mix.load_plan_file(root / "audio" / "audio_plan.json")


def _ok(qc: dict, name: str) -> bool:
    return next(c for c in qc["checks"] if c["name"] == name)["ok"]


# ------------------------------------------------------------------ plan + placement guards

def test_vo_gap_constant():
    assert audio_mix.MIN_VO_GAP_RANGE == (0.3, 0.4)
    assert validate_min_gap(0.35) == 0.35
    for bad in (0.2, 0.5):
        try:
            validate_min_gap(bad)
            raised = False
        except AudioInputError:
            raised = True
        assert raised, f"min gap {bad} accepted"


def test_mix_audio_vo_gap_validation():
    lines = [{"id": "A", "at": 0.0, "dur": 1.0}, {"id": "B", "at": 1.2, "dur": 1.0}]
    try:
        validate_placement(lines, 10.0, 0.4)
        err = None
    except PlacementError as e:
        err = str(e)
    assert err and "gap" in err, "a 0.2 s gap passed a 0.4 s minimum"
    ok = validate_placement([lines[0], {"id": "B", "at": 1.45, "dur": 1.0}], 10.0, 0.4)
    assert abs(ok["min_gap_s"] - 0.45) < 1e-6


def test_placement_rejects_overlap_tail_and_protected():
    for lines, prot in (
        ([{"id": "A", "at": 0.0, "dur": 2.0}, {"id": "B", "at": 1.0, "dur": 1.0}], None),
        ([{"id": "A", "at": 9.0, "dur": 0.9}], None),  # ends inside the 0.3 s tail guard
        ([{"id": "A", "at": 2.0, "dur": 1.0}], [[2.5, 4.0]]),
    ):
        try:
            validate_placement(lines, 10.0, 0.4, prot)
            raised = False
        except PlacementError:
            raised = True
        assert raised, f"placement {lines} {prot} accepted"


def test_plan_needs_input_or_generation_source():
    for raw in ({"vo": {"lines": [{"id": "A", "at": 0}]}}, {"music": {"level_db": -20}},
                {"sfx": [{"id": "S", "at": 0, "dur": 1}]}):
        try:
            normalize_plan(raw, ".")
            raised = False
        except AudioInputError:
            raised = True
        assert raised, f"plan {raw} accepted without a path or prompt"


def test_loudnorm_json_parse_is_real_and_strict():
    stderr = ('[Parsed_loudnorm_0 @ 0x1]\n{\n\t"input_i" : "-23.51",\n\t"input_tp" : "-4.20",\n'
              '\t"input_lra" : "5.10",\n\t"input_thresh" : "-33.9",\n\t"output_i" : "-12.40",\n'
              '\t"output_tp" : "-1.60",\n\t"output_lra" : "4.0",\n\t"output_thresh" : "-22.9",\n'
              '\t"normalization_type" : "linear",\n\t"target_offset" : "-0.10"\n}\n')
    got = parse_loudnorm_json(stderr)
    assert got["input_i"] == -23.51 and got["output_tp"] == -1.6
    for bad in ("no json here", stderr.replace('"-23.51"', '"-inf"'), stderr.replace('"input_tp" : "-4.20",\n', "")):
        try:
            parse_loudnorm_json(bad)
            raised = False
        except LoudnormParseError:
            raised = True
        assert raised, "an unusable loudnorm block parsed"


# ------------------------------------------------------------------ full mix on synthetic media

@pytest.mark.timeout(240)
async def test_mix_audio_final_basic(tmp_path):
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    paths = audio_mix.delivery_paths("ep01")
    for name in ("manifest", "mix_wav", "nosubs", "master_subs", "phone", "srt", "ass", "vo_stem"):
        assert Path(paths[name]).is_file(), name
    assert Path(paths["dir"]).as_posix().endswith("ep01/delivery")
    assert m["reused"] is False and m["subtitles_burned"] is True
    # two-pass loudnorm: pass-1 measurement parsed from real ffmpeg JSON, final within tolerance
    assert math.isfinite(m["loudnorm"]["pass1"]["input_i"])
    assert abs(m["mix_loudness"]["integrated_lufs"] - plan["loudness"]["target_lufs"]) <= plan["loudness"]["tolerance_lu"]
    assert m["mix_loudness"]["true_peak_dbtp"] <= plan["loudness"]["tp_ceiling_dbtp"]
    # pickup RMS match: the quiet line was raised toward the louder one
    gains = {ln["id"]: ln["gain_db"] for ln in m["vo_lines"]}
    assert gains["L02"] - gains["L01"] > 4.0, gains
    assert m["music"]["ducked_windows"] == 2
    qc = await delivery_qc(m, plan)
    assert qc["verdict"] == "PASS", qc["failed"]
    assert not qc["blocking"]


@pytest.mark.timeout(240)
async def test_music_is_ducked_under_vo(tmp_path):
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    music = await audio_mix.decode(m["paths"]["music_stem"])
    sr = audio_mix.SR
    # both windows sit after the 2 s fade-in and before the 1 s fade-out: only the duck differs
    under_vo = audio_mix.rms_db(music[int(2.7 * sr):int(3.3 * sr)])   # under L02 (2.5-3.5 s)
    clear = audio_mix.rms_db(music[int(4.0 * sr):int(4.6 * sr)])      # no VO
    assert clear - under_vo > 2.5, (clear, under_vo)
    assert abs((clear - under_vo) - abs(plan["music"]["duck_db"])) < 1.5, (clear, under_vo)


@pytest.mark.timeout(240)
async def test_mix_audio_idempotent_reuse(tmp_path):
    mute, plan = episode(tmp_path)
    first = await mix_and_deliver("ep01", str(mute), plan)
    again = await mix_and_deliver("ep01", str(mute), plan)
    assert again["reused"] is True and again["input_key"] == first["input_key"]
    # a re-rendered VO file (same path, new content) invalidates the reuse key
    make_tone(Path(plan["vo"]["lines"][0]["path"]), 1.2, 500, -4.0)
    third = await mix_and_deliver("ep01", str(mute), plan)
    assert third["reused"] is False and third["input_key"] != first["input_key"]


@pytest.mark.timeout(240)
async def test_mix_audio_with_subtitles(tmp_path):
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    found = audio_mix.parse_ass_cues(m["paths"]["ass"])
    assert len(found) == 2 and all(n == 1 for *_, n in found), found
    assert [" ".join(c["lines"]) for c in m["cues"]] == ["First line here.", "Second line."]
    assert abs(m["cues"][0]["start"] - 0.5) < 0.05
    srt = Path(m["paths"]["srt"]).read_text()
    assert "00:00:00,500 -->" in srt
    v = await audio_mix.probe(m["paths"]["master_subs"])
    assert any(s["codec_type"] == "audio" for s in v["streams"])


@pytest.mark.timeout(300)
async def test_mix_audio_phone_encode_size_cap(tmp_path):
    mute, plan = episode(tmp_path)
    plan["phone"] = {**plan["phone"], "max_mb": 0.5}
    m = await mix_and_deliver("ep01", str(mute), plan)
    assert m["phone"]["max_bytes"] == 500_000
    assert m["phone"]["size_bytes"] == Path(m["paths"]["phone"]).stat().st_size
    assert len(m["phone"]["attempts"]) <= 2
    qc = await delivery_qc(m, plan)
    assert _ok(qc, "phone_size") == m["phone"]["within_limit"]


@pytest.mark.timeout(300)
async def test_phone_over_size_gets_one_reencode(tmp_path, monkeypatch):
    """An over-size first phone encode is re-encoded once at a lower bitrate (and only once)."""
    mute, plan = episode(tmp_path)
    real = audio_mix.encode_phone
    calls = []

    async def first_too_big(mute_p, wav, ass, out, total, kbps, cfg, cwd, fonts=None):
        await real(mute_p, wav, ass, out, total, kbps, cfg, cwd, fonts)
        calls.append(kbps)
        if len(calls) == 1:  # pad the first attempt past the cap
            with open(out, "ab") as f:
                f.write(b"\0" * int(plan["phone"]["max_mb"] * 1_000_000))

    monkeypatch.setattr(audio_mix, "encode_phone", first_too_big)
    m = await mix_and_deliver("ep01", str(mute), plan)
    att = m["phone"]["attempts"]
    assert len(att) == 2 and len(calls) == 2, att
    assert att[0]["size_bytes"] > m["phone"]["max_bytes"] and att[1]["video_kbps"] < att[0]["video_kbps"]
    assert m["phone"]["within_limit"] is True
    qc = await delivery_qc(m, plan)
    assert _ok(qc, "phone_size")


@pytest.mark.timeout(240)
async def test_delivery_qc_fails_on_measured_misses(tmp_path):
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    strict = {**plan, "loudness": {**plan["loudness"], "target_lufs": -20.0, "tolerance_lu": 0.5}}
    qc = await delivery_qc(m, strict)
    assert qc["verdict"] == "FAIL" and qc["blocking"] and any("loudness" in f for f in qc["failed"]), qc["failed"]
    qc = await delivery_qc(m, plan, picture_duration=9.0)  # picture is 6 s
    assert qc["verdict"] == "FAIL" and "duration" in " ".join(qc["failed"])
    tp = {**plan, "loudness": {**plan["loudness"], "tp_ceiling_dbtp": -6.0}}
    qc = await delivery_qc(m, tp)
    assert any("true_peak" in f for f in qc["failed"]), qc["failed"]
    ass = Path(m["paths"]["ass"])
    ass.write_text(ass.read_text().replace("0:00:02.50,0:00:03.50", "0:00:02.90,0:00:03.90"))
    qc = await delivery_qc(m, plan)  # the written subtitle file drifted from the VO placement
    assert qc["verdict"] == "FAIL" and "subtitle_timing" in qc["failed"], qc["failed"]


@pytest.mark.timeout(240)
async def test_delivery_qc_requires_audio_stream(tmp_path):
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    shutil.copy(mute, m["paths"]["nosubs"])  # a deliverable without its audio stream
    qc = await delivery_qc(m, plan)
    assert qc["verdict"] == "FAIL"
    assert any(not c["ok"] and "audio" in c["name"] for c in qc["checks"]), qc["failed"]


@pytest.mark.timeout(120)
async def test_mix_rejects_missing_input_and_bad_gap(tmp_path):
    mute, plan = episode(tmp_path)
    Path(plan["sfx"][0]["path"]).unlink()
    try:
        await mix_and_deliver("ep01", str(mute), plan)
        err = None
    except AudioInputError as e:
        err = str(e)
    assert err and "missing" in err
    mute, plan = episode(tmp_path, "ep02")
    plan["vo"]["lines"][1]["at"] = 1.85  # L01 ends at 1.7 -> 0.15 s gap
    try:
        await mix_and_deliver("ep02", str(mute), plan)
        raised = False
    except PlacementError:
        raised = True
    assert raised
    assert not Path(audio_mix.delivery_paths("ep02")["manifest"]).exists()


@pytest.mark.timeout(240)
async def test_stand_in_inputs_are_never_a_pass(tmp_path, monkeypatch):
    """A measured-good mix built from dry-run stand-ins reports DRY_RUN (not PASS) and stays
    non-blocking; real measured misses still FAIL."""
    from temporalio.testing import ActivityEnvironment

    from hfvg.activities.audio import check_delivery_qc

    monkeypatch.setenv("DRY_RUN", "true")
    mute, plan = episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    dry = {**m, "dry_run": True, "dry_reason": "stand-in inputs: ['vo:L01']"}
    qc = await ActivityEnvironment().run(check_delivery_qc, "ep01", dry, plan, None)
    assert qc["verdict"] == "DRY_RUN" and qc["blocking"] is False, qc
    qc = await ActivityEnvironment().run(check_delivery_qc, "ep01", dry, plan, 9.0)
    assert qc["verdict"] == "FAIL" and qc["blocking"] is True
