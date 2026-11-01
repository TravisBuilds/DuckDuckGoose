"""GAP-51 (ep05v2 r9, Studio: free edit fixes). Edit-plan per-shot picture ops: a multi-segment cut
(drop a bad window from a clip) retimed to the UNCHANGED slot length (picture lock + VO sync stay put)
and a 9:16 punch-in crop upscaled with the edit's lanczos scale. Ops are bound to the clip sha256 and
validated fail-closed (no silent wrong-clip crop, no >2x upscale, no letterbox, bounded speed)."""
from __future__ import annotations

import hashlib
import json

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.media import EDIT_SCALE_FLAGS, assemble_mute_edit, edit_normalize_vf, render_shot_preview
from hfvg.edit_plan import (EditPlanError, parse_shot_ops, shot_ops_filter, shot_ops_speed,
                            validate_shot_ops)
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe

SHA = "a" * 64


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_validate_rejects_bad_ops():
    ok = {"sha256": SHA, "reason": "cut tail window", "segments": [[0, 0.4], [2.2, 4.0]]}
    assert validate_shot_ops("V11", ok)["interp"] == "blend"
    bad = [
        {**ok, "bogus": 1},                                   # unknown key
        {k: v for k, v in ok.items() if k != "sha256"},       # unbound ops
        {**ok, "sha256": "xyz"},
        {**ok, "reason": " "},                                # reason required
        {**ok, "segments": [[2.2, 4.0], [0, 0.4]]},           # out of order
        {**ok, "segments": [[0, 1.0], [0.5, 2.0]]},           # overlap
        {**ok, "segments": [[0, 0.1]]},                       # too short
        {**ok, "segments": []},
        {**ok, "interp": "magic"},
        {"sha256": SHA, "reason": "r", "interp": "mci"},      # interp without segments
        {"sha256": SHA, "reason": "r"},                       # nothing to do
        {"sha256": SHA, "reason": "r", "crop": [0, 0, 540, 900]},    # not 9:16
        {"sha256": SHA, "reason": "r", "crop": [0, 0, 450, 800]},    # 2.4x upscale
        {"sha256": SHA, "reason": "r", "crop": [0, 0, 541, 962]},    # odd
        {"sha256": SHA, "reason": "r", "crop": [-2, 0, 540, 960]},
        {"sha256": SHA, "reason": "r", "crop": [0.0, 0, 540, 960]},  # not int
    ]
    for ops in bad:
        with pytest.raises(EditPlanError):
            validate_shot_ops("V11", ops)
    assert validate_shot_ops("V05", {"sha256": SHA, "reason": "r", "crop": [200, 190, 540, 960]})["crop"] == [200, 190, 540, 960]


def test_speed_is_bounded_and_keeps_slot_length():
    ops = validate_shot_ops("V11", {"sha256": SHA, "reason": "r", "segments": [[0, 0.4], [2.2, 4.0417]]})
    assert shot_ops_speed(ops, 4.0) == pytest.approx(2.2417 / 4.0)
    with pytest.raises(EditPlanError, match="speed"):
        shot_ops_speed(validate_shot_ops("V11", {"sha256": SHA, "reason": "r", "segments": [[2.2, 4.0417]]}), 4.0)
    with pytest.raises(EditPlanError, match="speed"):
        shot_ops_speed(validate_shot_ops("X", {"sha256": SHA, "reason": "r", "segments": [[0, 9.0]]}), 4.0)
    assert shot_ops_speed({"crop": [0, 0, 540, 960]}, 4.0) == 1.0


def test_parse_shot_ops_requires_ids_in_order():
    plan = {"order": ["V11"], "shot_ops": {"V12": {"sha256": SHA, "reason": "r", "crop": [0, 0, 540, 960]}}}
    with pytest.raises(EditPlanError, match="not in the edit order"):
        parse_shot_ops(plan)
    assert parse_shot_ops({"order": ["V11"]}) == {}


def test_filter_chain_order_and_lanczos():
    ops = validate_shot_ops("V11", {"sha256": SHA, "reason": "r", "segments": [[0, 0.4], [2.2, 4.0]],
                                    "interp": "mci", "crop": [100, 200, 540, 960]})
    f = shot_ops_filter(ops, shot_ops_speed(ops, 4.0), 24, edit_normalize_vf(24))
    assert "trim=start=0.000000:end=0.400000" in f and "trim=start=2.200000:end=4.000000" in f
    assert "concat=n=2:v=1:a=0" in f and "minterpolate=fps=24" in f
    assert f.index("minterpolate") < f.index("crop=540:960:100:200") < f.index(f"flags={EDIT_SCALE_FLAGS}")
    assert f.endswith("[out]")


def _plan(ep, order, **extra):
    ep.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "episode_id": ep.name, "order": order, "reuse": {}, "dissolves": {}, "fps": 24, **extra}
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    return plan


@needs_ffmpeg
@pytest.mark.asyncio
async def test_segments_retime_to_slot_and_crop_in_edit(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    a = make_clip(tmp_path / "a.mp4", 4.0, MOVING, size="720x1280", rate=24)
    b = make_clip(tmp_path / "b.mp4", 2.0, MOVING, size="1080x1920", rate=24)
    shots = [{"shot_id": "A01", "duration": 4.0}, {"shot_id": "A02", "duration": 2.0}]
    clips = {"A01": str(a), "A02": str(b)}
    ep = tmp_path / "episodes" / "ep51"
    _plan(ep, ["A01", "A02"])
    base = await HB().run(assemble_mute_edit, "ep51", shots, clips, str(tmp_path / "o0"))

    ops = {"A01": {"sha256": _sha(a), "reason": "drop 0.6-2.2 s", "segments": [[0, 0.6], [2.2, 4.0]], "interp": "dup"},
           "A02": {"sha256": _sha(b), "reason": "punch in", "crop": [270, 480, 540, 960]}}
    _plan(ep, ["A01", "A02"], shot_ops=ops)
    r = await HB().run(assemble_mute_edit, "ep51", shots, clips, str(tmp_path / "o1"))
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (1080, 1920)
    # picture lock unchanged: same frames per slot, same total
    assert [c["frames"] for c in r["manifest"]["clips"]] == [c["frames"] for c in base["manifest"]["clips"]] == [96, 48]
    assert int(v["nb_read_frames"]) == 144 and r["duration"] == base["duration"] == pytest.approx(6.0)
    c0, c1 = r["manifest"]["clips"]
    assert c0["shot_ops"]["segments"] == [[0.0, 0.6], [2.2, 4.0]] and c0["shot_ops"]["speed"] == pytest.approx(2.4 / 4)
    assert c1["shot_ops"]["crop"] == [270, 480, 540, 960]
    assert r["input_key"] != base["input_key"]
    # no ops -> the same key as before GAP-51 (cached edits of other episodes stay valid)
    _plan(ep, ["A01", "A02"])
    again = await HB().run(assemble_mute_edit, "ep51", shots, clips, str(tmp_path / "o0"))
    assert again["reused"] and again["input_key"] == base["input_key"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_ops_bound_to_clip_sha_and_ranges(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    a = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="720x1280", rate=24)
    shots, clips = [{"shot_id": "A01", "duration": 2.0}], {"A01": str(a)}
    ep = tmp_path / "episodes" / "ep51b"
    _plan(ep, ["A01"], shot_ops={"A01": {"sha256": "b" * 64, "reason": "r", "crop": [0, 0, 540, 960]}})
    with pytest.raises(ApplicationError, match="made for clip"):
        await HB().run(assemble_mute_edit, "ep51b", shots, clips, str(tmp_path / "o"))
    _plan(ep, ["A01"], shot_ops={"A01": {"sha256": _sha(a), "reason": "r", "crop": [400, 0, 540, 960]}})
    with pytest.raises(ApplicationError, match="outside"):
        await HB().run(assemble_mute_edit, "ep51b", shots, clips, str(tmp_path / "o"))
    _plan(ep, ["A01"], shot_ops={"A01": {"sha256": _sha(a), "reason": "r", "segments": [[0, 0.5], [1.0, 3.0]]}})
    with pytest.raises(ApplicationError, match="exceeds the clip"):
        await HB().run(assemble_mute_edit, "ep51b", shots, clips, str(tmp_path / "o"))
    _plan(ep, ["A01"], shot_ops={"A01": {"sha256": _sha(a), "reason": "r", "segments": [[0, 0.5]]}})
    with pytest.raises(ApplicationError, match="speed"):
        await HB().run(assemble_mute_edit, "ep51b", shots, clips, str(tmp_path / "o"))


@needs_ffmpeg
@pytest.mark.asyncio
async def test_preview_matches_edit_normalize(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 3.0, MOVING, size="1080x1920", rate=24)
    r = await render_shot_preview(str(a), 2.0, str(tmp_path / "p.mp4"), 24,
                                  {"segments": [[0, 0.5], [1.5, 3.0]], "interp": "blend", "crop": [0, 0, 540, 960]})
    v = [s for s in probe(r["path"])["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"], int(v["nb_read_frames"])) == (1080, 1920, 48)
    assert r["ops"]["sha256"] == _sha(a) and r["ops"]["speed"] == pytest.approx(1.0)
