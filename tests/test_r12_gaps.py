"""ep05v2 r12 (Reviewer 2026-10-28: "jerky movements always don't flow and transitions don't work").

GAP-61  interp "rife": real motion interpolation for slowed shots (per segment, never across a join,
        exact frame count, cached, heartbeating, fails CLOSED without the binary).
GAP-62  timeline-neutral transitions: plan["transitions"] {"A->B": {kind, frames, reason}} blends A's
        tail (rendered past its slot) over B's head; shot starts / runtime / VO do not move.
GAP-63  speed-floor warning: retimes below 0.7x, and slowed shots without motion interpolation, are
        warned in the manifest (edit_plan.speed_warnings); the 0.5x hard floor is unchanged.
GAP-64  clip length override: retry_clip(..., duration_s) and recompose clip_route.duration_s, bounded
        per model, estimate/submit use it, default None keeps the plan duration (replay-safe)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from hfvg import audio_mix, interp_rife
from hfvg.activities.media import _shot_ops_input_args, assemble_mute_edit, render_shot_preview
from hfvg.edit_plan import (EditPlanError, SHOT_OPS_WARN_SPEED, parse_transitions, shot_ops_filter,
                            speed_warnings, validate_shot_ops)
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as EW
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe

FAKE = str(Path(__file__).with_name("fake_rife.py"))
ORDER = ["A01", "A02", "A03"]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _plan(ep, order, **extra):
    ep.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "episode_id": ep.name, "order": order, "reuse": {}, "dissolves": {}, "fps": 24, **extra}
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    return plan


@pytest.fixture
def fake_rife(tmp_path, monkeypatch):
    model = tmp_path / "rife-v4.6"
    model.mkdir()
    (model / "flownet.param").write_text("x")
    monkeypatch.setenv("RIFE_NCNN_BIN", FAKE)
    monkeypatch.setenv("RIFE_MODEL_DIR", str(model))
    monkeypatch.setenv("RIFE_CACHE_DIR", str(tmp_path / "rifecache"))
    monkeypatch.setenv("FAKE_RIFE_LOG", str(tmp_path / "rife.log"))
    return tmp_path / "rife.log"


# ---------------------------------------------------------------- GAP-61 pure parts
def test_allocate_frames_is_exact_and_proportional():
    assert interp_rife.allocate_frames([[0, 1.0]], 48) == [48]
    assert interp_rife.allocate_frames([[0, 1.0], [2.0, 3.0]], 48) == [24, 24]
    a = interp_rife.allocate_frames([[0, 1.21], [3.083, 4.04]], 130)
    assert sum(a) == 130 and a[0] > a[1] and abs(a[0] / 130 - 1.21 / (1.21 + 0.957)) < 0.01
    assert sum(interp_rife.allocate_frames([[0, 0.3], [2.2, 4.04]], 96)) == 96
    assert interp_rife.allocate_frames([[0, 0.01], [0.5, 3.0]], 3)[0] >= 1
    with pytest.raises(ValueError):
        interp_rife.allocate_frames([[0, 1], [2, 3]], 1)


def test_rife_request_step_matches_wanted_speed():
    # 72 source frames over 120 outputs (0.6x): feed 73 (one past the end), ask ~122, take 120
    k, n = interp_rife.rife_request(72, 120, True)
    assert k == 73 and n == 122 and abs(k / n - 72 / 120) < 0.002
    assert (119 * k / n) < 72          # the last kept output is still inside the segment
    k, n = interp_rife.rife_request(72, 120, False)
    # GAP-71: no frame past a clip-end segment -> the last kept output must not pass the last input
    assert k == 72 and n == 121 and (119 * k / n) <= 71
    assert interp_rife.rife_request(24, 24, True) == (25, 25)   # 1x: straight copy
    with pytest.raises(ValueError):
        interp_rife.rife_request(0, 10, True)


def test_cache_key_binds_clip_segments_frames_model():
    k = interp_rife.cache_key("a" * 64, [[0, 1]], 48, 24, "/x/rife-v4.6")
    assert k == interp_rife.cache_key("a" * 64, [[0.0, 1.0]], 48, 24, "/y/rife-v4.6")
    assert len({k, interp_rife.cache_key("b" * 64, [[0, 1]], 48, 24, "m/rife-v4.6"),
                interp_rife.cache_key("a" * 64, [[0, 1.1]], 48, 24, "m/rife-v4.6"),
                interp_rife.cache_key("a" * 64, [[0, 1]], 49, 24, "m/rife-v4.6"),
                interp_rife.cache_key("a" * 64, [[0, 1]], 48, 24, "m/rife-v4")}) == 5


def test_rife_fails_closed_without_binary(tmp_path, monkeypatch):
    monkeypatch.delenv("RIFE_NCNN_BIN", raising=False)
    with pytest.raises(interp_rife.RifeUnavailable):
        interp_rife.rife_config()
    monkeypatch.setenv("RIFE_NCNN_BIN", str(tmp_path / "nope"))
    with pytest.raises(interp_rife.RifeUnavailable):
        interp_rife.rife_config()
    monkeypatch.setenv("RIFE_NCNN_BIN", FAKE)
    monkeypatch.setenv("RIFE_MODEL_DIR", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    with pytest.raises(interp_rife.RifeUnavailable, match="model"):
        interp_rife.rife_config()


def test_validate_and_filter_for_rife():
    ops = validate_shot_ops("V04", {"sha256": "a" * 64, "reason": "r", "segments": [[0, 3]], "interp": "rife"})
    assert ops["interp"] == "rife"
    f = shot_ops_filter(ops, 0.75, 24, "fps=24,TAIL", pre_retimed=True)
    assert "setpts" not in f and "minterpolate" not in f and "trim=" not in f and f.endswith("TAIL[out]")
    f2 = shot_ops_filter({**ops, "crop": [0, 0, 540, 960]}, 0.75, 24, "TAIL", pre_retimed=True)
    assert "crop=540:960:0:0" in f2
    # the non-rife path is untouched
    assert "minterpolate" in shot_ops_filter({**ops, "interp": "mci"}, 0.75, 24, "TAIL")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_rife_missing_binary_refuses_the_shot(tmp_path, monkeypatch):
    monkeypatch.delenv("RIFE_NCNN_BIN", raising=False)
    clip = make_clip(tmp_path / "a.mp4", 1.0, MOVING, size="72x128", rate=24)
    ops = {"sha256": _sha(clip), "reason": "r", "segments": [[0, 1.0]], "interp": "rife", "speed": 0.5}
    with pytest.raises(ApplicationError, match="RIFE_NCNN_BIN") as e:
        await _shot_ops_input_args("A01", {"shot_id": "A01", "clip_path": str(clip)}, ops, 1.0, 0.0, 24, "fps=24", 48)
    assert e.value.type == "RifeUnavailable"


# ---------------------------------------------------------------- GAP-61 retime with the fake binary
@needs_ffmpeg
@pytest.mark.asyncio
async def test_rife_retime_per_segment_exact_and_cached(tmp_path, fake_rife):
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)   # 48 source frames
    beats = []
    from hfvg.activities.media import _run
    segs = [[0.0, 0.5], [1.5, 2.0]]   # 12 + 12 source frames, a cut-out window between
    r = await interp_rife.rife_retime(str(clip), _sha(clip), segs, 40, 24, _run, beats.append)
    assert not r["cached"]
    v = [s for s in probe(r["path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 40
    ps = r["per_segment"]
    assert [p["frames"] for p in ps] == [20, 20]
    assert ps[0]["src_frames"] == [12 * 0, 12] and ps[1]["src_frames"] == [36, 48]
    # segment 1 is fed one frame past its end (interpolates up to the boundary), segment 2 (clip end) not
    assert ps[0]["inputs"] == 13 and ps[1]["inputs"] == 12
    # one rife call per segment: never interpolated ACROSS the join
    calls = [tuple(map(int, l.split())) for l in fake_rife.read_text().split("\n") if l]
    assert calls == [(13, 22), (12, 21)]   # GAP-71: clip-end segment asks 21 so output 19 stays <= input 11
    again = await interp_rife.rife_retime(str(clip), _sha(clip), segs, 40, 24, _run, beats.append)
    assert again["cached"] and again["path"] == r["path"]
    assert len(fake_rife.read_text().split("\n")) == 3       # no new rife call
    other = await interp_rife.rife_retime(str(clip), _sha(clip), segs, 44, 24, _run, None)
    assert not other["cached"] and other["path"] != r["path"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_rife_retime_heartbeats_while_running(tmp_path, fake_rife, monkeypatch):
    clip = make_clip(tmp_path / "a.mp4", 1.0, MOVING, size="72x128", rate=24)
    monkeypatch.setattr(interp_rife, "HEARTBEAT_EVERY_S", 0.01)
    from hfvg.activities.media import _run

    async def slow_run(cmd, what):
        if cmd[0] == FAKE:
            await asyncio.sleep(0.2)
        return await _run(cmd, what)
    beats = []
    await interp_rife.rife_retime(str(clip), _sha(clip), [[0, 1.0]], 36, 24, slow_run, beats.append)
    assert sum(1 for b in beats if b.get("stage") == "rife" and "segment" in b) >= 3


@needs_ffmpeg
@pytest.mark.asyncio
async def test_assembly_with_rife_keeps_frames_and_records(tmp_path, monkeypatch, fake_rife):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    clips = {s: str(make_clip(tmp_path / f"{s}.mp4", 2.0, MOVING, size="360x640", rate=24)) for s in ORDER}
    shots = [{"shot_id": s, "duration": 2.0} for s in ORDER]
    ep = tmp_path / "episodes" / "ep61"
    _plan(ep, ORDER, shot_ops={"A02": {"sha256": _sha(clips["A02"]), "reason": "slow", "interp": "rife",
                                       "segments": [[0, 0.75], [1.0, 1.6]]}})
    r = await HB().run(assemble_mute_edit, "ep61", shots, clips, str(tmp_path / "o"))
    m = r["manifest"]
    assert [c["frames"] for c in m["clips"]] == [48, 48, 48]
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 144
    assert m["clips"][1]["rife"]["cached"] is False and m["clips"][1]["shot_ops"]["interp"] == "rife"
    assert [p["frames"] for p in m["clips"][1]["rife"]["per_segment"]] == [27, 21]
    # 1.35 s source in a 2 s slot = 0.675x -> GAP-63 warns (slow), but rife is a smooth interp
    w = m["edit_plan"]["speed_warnings"]
    assert [(x["id"], x["kind"]) for x in w] == [("A02", "slow")]
    # preview with the same ops pre-warms (shares the cache) and reports the warning too
    p = await render_shot_preview(clips["A02"], 2.0, str(tmp_path / "pv.mp4"), 24,
                                  {"sha256": _sha(clips["A02"]), "interp": "rife", "segments": [[0, 0.75], [1.0, 1.6]]})
    assert p["rife"]["cached"] is True and p["warnings"][0]["kind"] == "slow"


# ---------------------------------------------------------------- GAP-63
def test_speed_warnings():
    seg = {"segments": [[0, 1]], "interp": "rife"}
    assert speed_warnings("X", seg, 0.75) == []
    assert speed_warnings("X", seg, 1.0) == []
    assert speed_warnings("X", None, 0.5) == []
    assert speed_warnings("X", {"crop": [0, 0, 2, 4]}, 1.0) == []
    w = speed_warnings("X", seg, 0.69)
    assert [x["kind"] for x in w] == ["slow"] and "0.7" in w[0]["msg"]
    assert SHOT_OPS_WARN_SPEED == 0.7
    assert [x["kind"] for x in speed_warnings("X", {**seg, "interp": "blend"}, 0.6)] == ["slow", "interp"]
    assert [x["kind"] for x in speed_warnings("X", {**seg, "interp": "dup"}, 0.8)] == ["interp"]
    assert speed_warnings("X", {**seg, "interp": "mci"}, 0.8) == []
    assert [x["kind"] for x in speed_warnings("X", {"segments": [[0, 1]]}, 0.8)] == ["interp"]  # default blend


# ---------------------------------------------------------------- GAP-62
def test_parse_transitions_validation():
    plan = {"order": ORDER, "transitions": {"A01->A02": {"kind": "dissolve", "frames": 6, "reason": "match"}}}
    assert parse_transitions(plan) == {("A01", "A02"): {"kind": "dissolve", "frames": 6, "reason": "match"}}
    assert parse_transitions({"order": ORDER}) == {}
    bad = [
        {"A01->A03": {"frames": 6, "reason": "r"}},                  # not consecutive
        {"A02->A01": {"frames": 6, "reason": "r"}},
        {"A01A02": {"frames": 6, "reason": "r"}},
        {"A01->A02": {"frames": 1, "reason": "r"}},                  # too short
        {"A01->A02": {"frames": 25, "reason": "r"}},                 # too long
        {"A01->A02": {"frames": 6.0, "reason": "r"}},
        {"A01->A02": {"frames": True, "reason": "r"}},
        {"A01->A02": {"frames": 6}},                                 # reason
        {"A01->A02": {"frames": 6, "reason": "r", "kind": "wipe"}},
        {"A01->A02": {"frames": 6, "reason": "r", "offset": 2}},
        {"A01->A02": 6},
    ]
    for t in bad:
        with pytest.raises(EditPlanError):
            parse_transitions({"order": ORDER, "transitions": t})
    with pytest.raises(EditPlanError, match="also listed in dissolves"):
        parse_transitions({"order": ORDER, "dissolves": {"A01->A02": 0.5},
                           "transitions": {"A01->A02": {"frames": 6, "reason": "r"}}})


@needs_ffmpeg
@pytest.mark.asyncio
async def test_transition_keeps_starts_runtime_and_vo(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    # A01: 3 s clip used for 2 s (1 s handle) ; A02 retimed (needs no handle) ; A03 plain
    a = make_clip(tmp_path / "a.mp4", 3.0, "color=c=red", size="360x640", rate=24)
    b = make_clip(tmp_path / "b.mp4", 2.0, "color=c=blue", size="360x640", rate=24)
    c = make_clip(tmp_path / "c.mp4", 2.0, "color=c=green", size="360x640", rate=24)
    clips = {"A01": str(a), "A02": str(b), "A03": str(c)}
    shots = [{"shot_id": s, "duration": 2.0} for s in ORDER]
    ep = tmp_path / "episodes" / "ep62"
    _plan(ep, ORDER)
    base = await HB().run(assemble_mute_edit, "ep62", shots, clips, str(tmp_path / "o0"))
    tr = {"A01->A02": {"kind": "dissolve", "frames": 6, "reason": "action carries"},
          "A02->A03": {"kind": "dissolve", "frames": 4, "reason": "soften"}}
    _plan(ep, ORDER, transitions=tr,
          shot_ops={"A02": {"sha256": _sha(b), "reason": "slow a bit", "segments": [[0, 1.8]], "interp": "blend"}})
    r = await HB().run(assemble_mute_edit, "ep62", shots, clips, str(tmp_path / "o1"))
    m = r["manifest"]
    assert [x["frames"] for x in m["clips"]] == [48, 48, 48] and r["duration"] == base["duration"] == 6.0
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 144
    assert m["clips"][0]["tail_frames"] == 6 and m["clips"][1]["tail_frames"] == 4 and "tail_frames" not in m["clips"][2]
    assert m["transitions"]["A01->A02"]["frames"] == 6 and m["dissolves"] == {}
    assert r["input_key"] != base["input_key"]
    # the timeline (and so every VO line) is unchanged
    t0, t1 = audio_mix.timeline_from_manifest(base["manifest"]), audio_mix.timeline_from_manifest(m)
    assert [(c["id"], c["start"], c["seg_s"]) for c in t1["clips"]] == [(c["id"], c["start"], c["seg_s"]) for c in t0["clips"]]
    assert t1["total_s"] == t0["total_s"] == 6.0
    plan = {"timeline_ref": audio_mix.validate_timeline(t0, "ref"), "vo": {"lines": [{"id": "L1", "at": 2.4}, {"id": "L2", "at": 4.3}]},
            "sfx": [], "protected": [], "bed_dips": []}
    assert [ln["at"] for ln in audio_mix.remap_plan_to_edit(plan, t1)["vo"]["lines"]] == [2.4, 4.3]
    # picture: frame 47 is pure A01 red, 48 starts the blend (mostly red), 53 mostly blue, 54 pure blue
    import subprocess

    def px(n):
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", r["assembly_path"], "-vf",
                              f"select=eq(n\\,{n}),scale=1:1", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout
        return tuple(out[:3])
    r47, r48, r53, r54 = px(47), px(48), px(53), px(54)
    assert r47[0] > 200 and r47[2] < 40
    assert r48[0] > r48[2] and r53[2] > r53[0]
    assert r54[2] > 200 and r54[0] < 40
    assert px(95)[2] > 150 and px(96)[2] > px(96)[1]          # A02->A03 blend starts at 96 (still blue-ish)
    assert px(100)[1] > 100 and px(100)[2] < 60             # pure green after 4 frames


@needs_ffmpeg
@pytest.mark.asyncio
async def test_transition_refuses_without_handle(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    clips = {s: str(make_clip(tmp_path / f"{s}.mp4", 2.0, MOVING, size="360x640", rate=24)) for s in ORDER}
    shots = [{"shot_id": s, "duration": 2.0} for s in ORDER]
    ep = tmp_path / "episodes" / "ep62b"
    _plan(ep, ORDER, transitions={"A01->A02": {"frames": 6, "reason": "r"}})
    with pytest.raises(ApplicationError, match="never a frozen hold") as e:
        await HB().run(assemble_mute_edit, "ep62b", shots, clips, str(tmp_path / "o"))
    assert e.value.type == "TransitionOutOfRange"


# ---------------------------------------------------------------- GAP-64
def test_clip_duration_and_route_validation():
    assert EW.clip_duration_problem(None, "kling") is None
    assert EW.clip_duration_problem(8, "kling") is None
    assert EW.clip_duration_problem(3, "kling") is None and EW.clip_duration_problem(15, "kling") is None
    for bad in (2, 16, 7.5, "8", True):
        assert EW.clip_duration_problem(bad, "kling")
    assert EW.clip_duration_problem(3, "seedance") and EW.clip_duration_problem(4, "seedance") is None
    assert EW.clip_duration_problem(5, "veo")
    assert EW.clip_route_problem({"duration_s": 8}, {"model": "kling"}) is None
    assert "duration_s" in EW.clip_route_problem({"duration_s": 20}, {"model": "kling"})
    assert EW.clip_route_problem({"model": "seedance", "duration_s": 3}, {"model": "kling"})
    p = EW.routed_params({"model": "kling", "duration": 4}, {"duration_s": 8})
    assert p["duration"] == 8 and p["model"] == "kling"
    assert EW.routed_params({"model": "kling", "duration": 4}, {"model": "kling"})["duration"] == 4


def test_api_retry_clip_keeps_old_arg_shape():
    from api.main import RetryClipRequest
    assert RetryClipRequest(reason="r").duration_s is None
    assert RetryClipRequest(reason="r", duration_s=8).duration_s == 8
    with pytest.raises(Exception):
        RetryClipRequest(reason="r", duration_s=2)
    src = Path(__file__).resolve().parents[1].joinpath("api", "main.py").read_text()
    assert "+ ([request.duration_s] if request.duration_s is not None else [])" in src
