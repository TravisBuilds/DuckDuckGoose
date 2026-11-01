"""GAP-58 (ep05v2 r11, Studio: free edit pass). Move slot time between shots in the edit plan:
plan["slots"] = {shot: {screen_s, reason}}, frame-exact and ZERO-SUM (the cut length never changes),
applied to generated AND reused shots before the GAP-51 shot_ops see the slot. A slot longer than its
clip needs shot_ops segments (retimed within the 0.5x-2x bounds); otherwise the edit refuses. VO placed
by the GAP-23 timeline_ref remap does not move when the shots it sits in keep their start."""
from __future__ import annotations

import hashlib
import json

import pytest
from temporalio.exceptions import ApplicationError

from hfvg import audio_mix
from hfvg.activities.media import assemble_mute_edit
from hfvg.edit_plan import EditPlanError, apply_slots, validate_slots
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe

ORDER = ["A01", "A02", "A03"]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _plan(ep, order, **extra):
    ep.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "episode_id": ep.name, "order": order, "reuse": {}, "dissolves": {}, "fps": 24, **extra}
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    return plan


def test_validate_slots_rejects_bad_input():
    plan = {"order": ORDER, "slots": {"A01": {"screen_s": 3.0, "reason": "gets A02's time"}}}
    assert validate_slots(plan, 24) == {"A01": {"screen_s": 3.0, "frames": 72, "reason": "gets A02's time"}}
    assert validate_slots({"order": ORDER}, 24) == {}
    bad = [
        {"A09": {"screen_s": 1.0, "reason": "r"}},              # not in the order
        {"A01": {"screen_s": 1.0}},                            # reason required
        {"A01": {"screen_s": 1.0, "reason": " "}},
        {"A01": {"screen_s": 1.0, "reason": "r", "speed": 1}},  # unknown key
        {"A01": {"screen_s": 0, "reason": "r"}},
        {"A01": {"screen_s": -1.0, "reason": "r"}},
        {"A01": {"screen_s": True, "reason": "r"}},
        {"A01": {"screen_s": "2", "reason": "r"}},
        {"A01": {"screen_s": 1.01, "reason": "r"}},             # not a whole frame at 24 fps
        {"A01": 2.0},
    ]
    for slots in bad:
        with pytest.raises(EditPlanError):
            validate_slots({"order": ORDER, "slots": slots}, 24)
    with pytest.raises(EditPlanError):
        validate_slots({"order": ORDER, "slots": [["A01", 2.0]]}, 24)
    # 1.833333 s = 44 frames at 24 fps is accepted (rounded to 6 dp)
    assert validate_slots({"order": ORDER, "slots": {"A01": {"screen_s": 1.833333, "reason": "r"}}}, 24)["A01"]["frames"] == 44


def test_apply_slots_is_zero_sum_and_records_moves():
    def entries():
        return [{"shot_id": s, "clip_path": f"{s}.mp4", "trim_in": 0.0, "trim_out": 2.0} for s in ORDER]
    plan = {"order": ORDER, "slots": {"A01": {"screen_s": 3.0, "reason": "+1 s"}, "A02": {"screen_s": 1.0, "reason": "-1 s"}}}
    e = entries()
    moves = apply_slots(e, validate_slots(plan, 24), 24)
    assert [x["trim_out"] for x in e] == [3.0, 1.0, 2.0]
    assert e[0]["slot_move"] == {"from_s": 2.0, "to_s": 3.0, "reason": "+1 s"} and "slot_move" not in e[2]
    assert [(m["id"], m["from_frames"], m["to_frames"]) for m in moves] == [("A01", 48, 72), ("A02", 48, 24)]
    # net change refused, entries untouched
    e = entries()
    with pytest.raises(EditPlanError, match="net \\+1 frames"):
        apply_slots(e, validate_slots({"order": ORDER, "slots": {"A01": {"screen_s": 2.041667, "reason": "r"}}}, 24), 24)
    assert [x["trim_out"] for x in e] == [2.0, 2.0, 2.0] and "slot_move" not in e[0]
    with pytest.raises(EditPlanError, match="not in this edit"):
        apply_slots(entries()[:2], validate_slots({"order": ORDER, "slots": {"A03": {"screen_s": 2.0, "reason": "r"}}}, 24), 24)


async def _clips(tmp_path):
    a = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="720x1280", rate=24)
    b = make_clip(tmp_path / "b.mp4", 2.0, MOVING, size="720x1280", rate=24)
    c = make_clip(tmp_path / "c.mp4", 2.0, MOVING, size="720x1280", rate=24)
    shots = [{"shot_id": s, "duration": 2.0} for s in ORDER]
    return shots, {"A01": str(a), "A02": str(b), "A03": str(c)}


@needs_ffmpeg
@pytest.mark.asyncio
async def test_slot_move_retimes_neighbour_keeps_total_and_vo(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    shots, clips = await _clips(tmp_path)
    ep = tmp_path / "episodes" / "ep58"
    _plan(ep, ORDER)
    base = await HB().run(assemble_mute_edit, "ep58", shots, clips, str(tmp_path / "o0"))
    assert [c["frames"] for c in base["manifest"]["clips"]] == [48, 48, 48]

    # A02 keeps only its first 0.5 s slowed to 0.5x (1.0 s slot); its spare 1.0 s goes to A01 (slowed to 0.667x)
    slots = {"A01": {"screen_s": 3.0, "reason": "gets A02's time"}, "A02": {"screen_s": 1.0, "reason": "clean head only"}}
    ops = {"A01": {"sha256": _sha(clips["A01"]), "reason": "fill the longer slot", "segments": [[0, 2.0]], "interp": "blend"},
           "A02": {"sha256": _sha(clips["A02"]), "reason": "clean head", "segments": [[0, 0.5]], "interp": "blend"}}
    _plan(ep, ORDER, slots=slots, shot_ops=ops)
    r = await HB().run(assemble_mute_edit, "ep58", shots, clips, str(tmp_path / "o1"))
    m = r["manifest"]
    assert [c["frames"] for c in m["clips"]] == [72, 24, 48]
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 144 and r["duration"] == base["duration"] == pytest.approx(6.0)
    assert m["clips"][0]["shot_ops"]["speed"] == pytest.approx(2.0 / 3.0)
    assert m["clips"][1]["shot_ops"]["speed"] == pytest.approx(0.5)
    assert m["clips"][0]["slot_move"] == {"from_s": 2.0, "to_s": 3.0, "reason": "gets A02's time"}
    assert "slot_move" not in m["clips"][2]
    assert [(x["id"], x["from_frames"], x["to_frames"]) for x in m["edit_plan"]["slot_moves"]] == [("A01", 48, 72), ("A02", 48, 24)]
    assert r["input_key"] != base["input_key"]

    # GAP-23 remap: VO in the lengthened first shot and in the untouched third shot keep their absolute time
    ref = audio_mix.timeline_from_manifest(base["manifest"])
    plan = {"timeline_ref": audio_mix.validate_timeline(ref, "ref"), "vo": {"lines": [{"id": "L1", "at": 0.5}, {"id": "L2", "at": 4.5}]},
            "sfx": [], "protected": [], "bed_dips": []}
    out = audio_mix.remap_plan_to_edit(plan, audio_mix.timeline_from_manifest(m))
    assert [ln["at"] for ln in out["vo"]["lines"]] == [0.5, 4.5] and out["timeline"]["moves"] == []

    # no slots -> the pre-GAP-58 key again (cached edits stay valid)
    _plan(ep, ORDER)
    again = await HB().run(assemble_mute_edit, "ep58", shots, clips, str(tmp_path / "o0"))
    assert again["reused"] and again["input_key"] == base["input_key"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_slot_move_refusals(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    shots, clips = await _clips(tmp_path)
    ep = tmp_path / "episodes" / "ep58b"
    run = lambda: HB().run(assemble_mute_edit, "ep58b", shots, clips, str(tmp_path / "o"))
    # longer slot without a retime: refused (no silent clamp to the clip / short shot)
    _plan(ep, ORDER, slots={"A01": {"screen_s": 3.0, "reason": "r"}, "A02": {"screen_s": 1.0, "reason": "r"}})
    with pytest.raises(ApplicationError, match="needs shot_ops segments"):
        await run()
    # not zero-sum: refused
    _plan(ep, ORDER, slots={"A02": {"screen_s": 1.0, "reason": "r"}})
    with pytest.raises(ApplicationError, match="cut length"):
        await run()
    # the 0.5x floor still binds on a lengthened slot (2.0 s source in a 4.25 s slot = 0.47x)
    _plan(ep, ORDER, slots={"A01": {"screen_s": 4.25, "reason": "r"}, "A02": {"screen_s": 0.875, "reason": "r"},
                            "A03": {"screen_s": 0.875, "reason": "r"}},
          shot_ops={"A01": {"sha256": _sha(clips["A01"]), "reason": "r", "segments": [[0, 2.0]]}})
    with pytest.raises(ApplicationError, match="speed"):
        await run()
    # a retimed slot still range-checks its segments against the clip
    _plan(ep, ORDER, slots={"A01": {"screen_s": 3.0, "reason": "r"}, "A02": {"screen_s": 1.0, "reason": "r"}},
          shot_ops={"A01": {"sha256": _sha(clips["A01"]), "reason": "r", "segments": [[0, 2.5]]}})
    with pytest.raises(ApplicationError, match="exceeds the clip"):
        await run()
