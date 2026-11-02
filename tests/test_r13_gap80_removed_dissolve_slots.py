"""GAP-80 (ep05v2 r13 v11, Studio: straight cut at V23->V24). Replacing a cross-dissolve with a
straight cut lengthens the cut by the dissolve overlap, and GAP-58 slot moves are zero-sum, so the
harness had no way to keep the 120.000 s lock. plan["removed_dissolves"] = {"A->B": s} declares the
removed dissolve; the slot moves must then net exactly -overlap frames (no more, no less), the pair
must be consecutive and not still active. Without the key the zero-sum rule is unchanged."""
from __future__ import annotations

import hashlib
import json

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.media import assemble_mute_edit
from hfvg.edit_plan import EditPlanError, apply_slots, removed_dissolve_frames, validate_slots
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe

ORDER = ["A01", "A02", "A03"]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def _plan(ep, order, **extra):
    ep.mkdir(parents=True, exist_ok=True)
    plan = {"version": 1, "episode_id": ep.name, "order": order, "reuse": {}, "dissolves": {}, "fps": 24, **extra}
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    return plan


def test_removed_dissolve_frames_validation():
    assert removed_dissolve_frames({"order": ORDER}, 24) == 0
    assert removed_dissolve_frames({"order": ORDER, "removed_dissolves": {"A02->A03": 0.5}}, 24) == 12
    assert removed_dissolve_frames({"order": ORDER, "removed_dissolves": {"A01->A02": 0.5, "A02->A03": 1.0}}, 24) == 36
    bad = [
        {"A01->A03": 0.5},   # not consecutive
        {"A03->A02": 0.5},   # wrong direction
        {"A09->A01": 0.5},   # not in order
        {"A01A02": 0.5},     # bad key
        {"A01->A02": 0},
        {"A01->A02": True},
        {"A01->A02": "0.5"},
        {"A01->A02": 0.51},  # not a whole frame
    ]
    for rd in bad:
        with pytest.raises(EditPlanError):
            removed_dissolve_frames({"order": ORDER, "removed_dissolves": rd}, 24)
    with pytest.raises(EditPlanError, match="still listed"):
        removed_dissolve_frames({"order": ORDER, "dissolves": {"A01->A02": 0.5}, "removed_dissolves": {"A01->A02": 0.5}}, 24)
    with pytest.raises(EditPlanError):
        removed_dissolve_frames({"order": ORDER, "removed_dissolves": [["A01->A02", 0.5]]}, 24)


def test_apply_slots_net_must_equal_removed_overlap_exactly():
    def entries():
        return [{"shot_id": s, "clip_path": f"{s}.mp4", "trim_in": 0.0, "trim_out": 2.0} for s in ORDER]
    order = {"order": ORDER}
    # -12 frames requested: exactly -12 passes
    e = entries()
    apply_slots(e, validate_slots({**order, "slots": {"A01": {"screen_s": 1.5, "reason": "r"}}}, 24), 24, net_frames=-12)
    assert [x["trim_out"] for x in e] == [1.5, 2.0, 2.0]
    # zero-sum refused when a dissolve was removed (cut would grow by the overlap)
    e = entries()
    with pytest.raises(EditPlanError, match="expected net -12"):
        apply_slots(e, validate_slots({**order, "slots": {"A01": {"screen_s": 2.5, "reason": "r"},
                                                          "A02": {"screen_s": 1.5, "reason": "r"}}}, 24), 24, net_frames=-12)
    assert [x["trim_out"] for x in e] == [2.0, 2.0, 2.0]
    # too much shortening refused
    with pytest.raises(EditPlanError, match="cut length"):
        apply_slots(entries(), validate_slots({**order, "slots": {"A01": {"screen_s": 1.0, "reason": "r"}}}, 24), 24, net_frames=-12)
    # removed dissolve without any slot move refused
    with pytest.raises(EditPlanError, match="removed_dissolves"):
        apply_slots(entries(), {}, 24, net_frames=-12)
    # default stays strictly zero-sum
    with pytest.raises(EditPlanError, match="net -12 frames"):
        apply_slots(entries(), validate_slots({**order, "slots": {"A01": {"screen_s": 1.5, "reason": "r"}}}, 24), 24)


async def _clips(tmp_path):
    out = {}
    for s in ORDER:
        out[s] = str(make_clip(tmp_path / f"{s}.mp4", 2.0, MOVING, size="720x1280", rate=24))
    return [{"shot_id": s, "duration": 2.0} for s in ORDER], out


@needs_ffmpeg
@pytest.mark.asyncio
async def test_straight_cut_keeps_cut_length(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    shots, clips = await _clips(tmp_path)
    ep = tmp_path / "episodes" / "ep80"
    run = lambda o: HB().run(assemble_mute_edit, "ep80", shots, clips, str(tmp_path / o))
    _plan(ep, ORDER, dissolves={"A02->A03": 0.5})
    base = await run("o0")
    assert base["duration"] == pytest.approx(5.5)
    # straight cut + A02 gives up the 12 overlap frames: same length, no dissolve
    _plan(ep, ORDER, removed_dissolves={"A02->A03": 0.5},
          slots={"A02": {"screen_s": 1.5, "reason": "straight cut A02->A03"}})
    r = await run("o1")
    m = r["manifest"]
    assert r["duration"] == pytest.approx(base["duration"])
    assert m["dissolves"] == {} and [c["frames"] for c in m["clips"]] == [48, 36, 48]
    v = [s for s in probe(r["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 132
    assert m["edit_plan"]["removed_dissolves"] == {"A02->A03": 0.5}
    # without the slot move the removal is refused (cut would grow 0.5 s)
    _plan(ep, ORDER, removed_dissolves={"A02->A03": 0.5})
    with pytest.raises(ApplicationError, match="removed_dissolves"):
        await run("o2")
    # zero-sum moves with a removed dissolve: refused
    _plan(ep, ORDER, removed_dissolves={"A02->A03": 0.5},
          slots={"A01": {"screen_s": 2.5, "reason": "r"}, "A02": {"screen_s": 1.5, "reason": "r"}},
          shot_ops={"A01": {"sha256": _sha(clips["A01"]), "reason": "r", "segments": [[0, 2.0]], "interp": "blend"}})
    with pytest.raises(ApplicationError, match="cut length"):
        await run("o3")
