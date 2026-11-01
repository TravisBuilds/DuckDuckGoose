"""GAP-83 (ep05v2 r15 v13, Travis via Studio: the duck points at nothing / the door closes, then a
same-angle cut). Dropping a shot (GAP-22 set_shot_edit drop) shortened the cut by its slot, and
GAP-58 slot moves are zero-sum, so the neighbours could not take the dropped time and the 120.000 s
lock broke. plan["absorbed_drops"] = {sid: {screen_s, reason}} declares it; the slot moves must then
net exactly +frames. The shot must be in the order and really absent from the edit."""
from __future__ import annotations

import hashlib
import json

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.media import assemble_mute_edit
from hfvg.edit_plan import EditPlanError, absorbed_drop_frames, apply_slots, validate_slots
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe

ORDER = ["A01", "A02", "A03"]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _plan(ep, **extra):
    ep.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "episode_id": ep.name, "order": ORDER, "reuse": {}, "dissolves": {}, "fps": 24, **extra}
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    return plan


def test_absorbed_drop_frames_validation():
    o = {"order": ORDER}
    assert absorbed_drop_frames(o, 24) == 0
    assert absorbed_drop_frames({**o, "absorbed_drops": {"A02": {"screen_s": 2.0, "reason": "r"}}}, 24) == 48
    assert absorbed_drop_frames({**o, "absorbed_drops": {"A02": {"screen_s": 2.0, "reason": "r"}}}, 24,
                                present=["A01", "A03"]) == 48
    bad = [
        {"A09": {"screen_s": 2.0, "reason": "r"}},             # not in order
        {"A02": {"screen_s": 2.0}},                             # no reason
        {"A02": {"screen_s": 2.0, "reason": " "}},
        {"A02": {"screen_s": 0, "reason": "r"}},
        {"A02": {"screen_s": True, "reason": "r"}},
        {"A02": {"screen_s": "2", "reason": "r"}},
        {"A02": {"screen_s": 2.01, "reason": "r"}},             # not a whole frame
        {"A02": {"screen_s": 2.0, "reason": "r", "x": 1}},      # unknown key
        {"A02": 2.0},
    ]
    for ad in bad:
        with pytest.raises(EditPlanError):
            absorbed_drop_frames({**o, "absorbed_drops": ad}, 24)
    with pytest.raises(EditPlanError):
        absorbed_drop_frames({**o, "absorbed_drops": [["A02", 2.0]]}, 24)
    with pytest.raises(EditPlanError, match="still in this edit"):
        absorbed_drop_frames({**o, "absorbed_drops": {"A02": {"screen_s": 2.0, "reason": "r"}}}, 24, present=ORDER)


def test_apply_slots_net_must_equal_absorbed_frames():
    def entries():
        return [{"shot_id": s, "clip_path": f"{s}.mp4", "trim_in": 0.0, "trim_out": 2.0} for s in ("A01", "A03")]
    o = {"order": ORDER}
    e = entries()
    apply_slots(e, validate_slots({**o, "slots": {"A03": {"screen_s": 4.0, "reason": "r"}}}, 24), 24, net_frames=48)
    assert [x["trim_out"] for x in e] == [2.0, 4.0]
    with pytest.raises(EditPlanError, match="expected net \\+48"):
        apply_slots(entries(), validate_slots({**o, "slots": {"A03": {"screen_s": 3.0, "reason": "r"}}}, 24), 24, net_frames=48)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_dropped_shot_time_absorbed_keeps_cut_length(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    clips = {s: str(make_clip(tmp_path / f"{s}.mp4", 2.0, MOVING, size="720x1280", rate=24)) for s in ORDER}
    ep = tmp_path / "episodes" / "ep83"
    full = [{"shot_id": s, "duration": 2.0} for s in ORDER]
    kept = [s for s in full if s["shot_id"] != "A02"]          # A02 dropped by the workflow (GAP-22)
    kept_clips = {k: v for k, v in clips.items() if k != "A02"}
    run = lambda shots, cl, o: HB().run(assemble_mute_edit, "ep83", shots, cl, str(tmp_path / o))
    _plan(ep)
    base = await run(full, clips, "o0")
    assert base["duration"] == pytest.approx(6.0)
    ops = {"A03": {"sha256": _sha(clips["A03"]), "reason": "r", "segments": [[0, 2.0]], "interp": "blend"}}
    # A02 dropped, A03 takes its 2.0 s (retimed): same cut length
    _plan(ep, absorbed_drops={"A02": {"screen_s": 2.0, "reason": "dropped, A03 absorbs"}},
          slots={"A03": {"screen_s": 4.0, "reason": "absorbs A02"}}, shot_ops=ops)
    r = await run(kept, kept_clips, "o1")
    m = r["manifest"]
    assert r["duration"] == pytest.approx(base["duration"])
    assert [c["id"] for c in m["clips"]] == ["A01", "A03"] and [c["frames"] for c in m["clips"]] == [48, 96]
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 144
    # declared but the shot is still in the edit: refused (time counted twice)
    with pytest.raises(ApplicationError, match="still in this edit"):
        await run(full, clips, "o2")
    # declared without the slot move: refused (the cut would not keep its length)
    _plan(ep, absorbed_drops={"A02": {"screen_s": 2.0, "reason": "r"}})
    with pytest.raises(ApplicationError, match="absorbed|net"):
        await run(kept, kept_clips, "o3")
    # without the declaration the zero-sum rule is unchanged: the growth is refused
    _plan(ep, slots={"A03": {"screen_s": 4.0, "reason": "r"}}, shot_ops=ops)
    with pytest.raises(ApplicationError, match="cut length"):
        await run(kept, kept_clips, "o4")
