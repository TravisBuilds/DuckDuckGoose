"""GAP-23 (ep05full 2026-10-27, Studio G3 review): the audio plan's VO / SFX / music / protected
times are absolute seconds on the edit they were authored against. Trimming F14 or cutting F06 at
G_FINAL_CUT shortened the picture and every later cue would have landed on the wrong frame (and the
music bed would have been cut before its ending). With ``timeline_ref`` in the plan, the delivery
re-places every time on the CURRENT edit (load_audio_plan reads the edit manifest)."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from hfvg import audio_mix as am
from hfvg.audio_mix import PlacementError


def _manifest(clips, dissolves=None, total=None):
    return {"clips": [{"id": i, "seg_s": seg, "trim_in": tin, "trim_out": tin + seg} for i, seg, tin in clips],
            "dissolves": dissolves or {}, "total_duration": total}


REF = _manifest([("S1", 6.0, 0.0), ("S2", 6.0, 0.0), ("S3", 4.0, 0.0), ("S4", 5.0, 0.0), ("S5", 9.0, 0.0)],
                total=30.0)


def _plan(tmp_path):
    raw = {
        "vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 1.0},
                         {"id": "L2", "path": "a.wav", "at": 12.5},     # S3 + 0.5
                         {"id": "L3", "path": "a.wav", "at": 19.4},     # S4 + 3.4 (trimmed away below)
                         {"id": "L4", "path": "a.wav", "at": 27.0}]},   # S5
        "music": {"path": "m.wav", "offset_s": 13.0, "end_align": True},
        "sfx": [{"id": "BED", "path": "s.wav", "at": 6.0, "dur": 10.0, "loop": True},   # S2..S3
                {"id": "P_S2", "path": "s.wav", "at": 8.0, "dur": 1.0},                # in dropped S2
                {"id": "P_S4_early", "path": "s.wav", "at": 18.5, "dur": 1.0},          # S4 + 2.5 kept
                {"id": "P_S4_late", "path": "s.wav", "at": 20.0, "dur": 1.0},           # S4 + 4.0 trimmed
                {"id": "TAIL", "path": "s.wav", "at": 21.0, "dur": 9.0, "loop": True}],
        "protected": [[25.0, 28.0]],
        "timeline_ref": am.timeline_from_manifest(REF),
    }
    return am.normalize_plan(raw, tmp_path)


def _edited():
    # S2 dropped, S4 trimmed to its first 3 s -> 30 - 6 - 2 = 22 s
    m = copy.deepcopy(REF)
    m["clips"] = [c for c in m["clips"] if c["id"] != "S2"]
    for c in m["clips"]:
        if c["id"] == "S4":
            c["seg_s"], c["trim_out"] = 3.0, 3.0
    m["total_duration"] = 22.0
    return am.timeline_from_manifest(m)


def test_timeline_from_manifest_handles_dissolves():
    tl = am.timeline_from_manifest(_manifest([("A", 4.0, 0.0), ("B", 4.0, 1.0), ("C", 2.0, 0.0)],
                                             dissolves={"A": 1.0}))
    assert [(c["id"], c["start"]) for c in tl["clips"]] == [("A", 0.0), ("B", 3.0), ("C", 7.0)]
    assert tl["total_s"] == 9.0


def test_plan_without_timeline_ref_is_unchanged(tmp_path):
    plan = am.normalize_plan({"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 1.0}]}}, tmp_path)
    assert "timeline_ref" not in plan
    assert am.remap_plan_to_edit(plan, _edited()) is plan


def test_remap_follows_dropped_and_trimmed_shots(tmp_path):
    out = am.remap_plan_to_edit(_plan(tmp_path), _edited())
    vo = {ln["id"]: ln["at"] for ln in out["vo"]["lines"]}
    assert vo["L1"] == 1.0                       # before the change
    assert vo["L2"] == pytest.approx(6.5)        # S3 now starts at 6.0
    assert vo["L3"] == pytest.approx(13.4)       # 0.4 s past S4's new end (10 + 3) keeps its distance
    assert vo["L4"] == pytest.approx(19.0)       # S5 now starts at 13.0
    sfx = {c["id"]: c for c in out["sfx"]}
    assert "P_S2" not in sfx and "P_S4_late" not in sfx            # their moments left the picture
    assert {r["id"] for r in out["timeline"]["removed_sfx"]} == {"P_S2", "P_S4_late"}
    assert sfx["P_S4_early"]["at"] == pytest.approx(12.5)
    assert sfx["BED"]["at"] == pytest.approx(6.0) and sfx["BED"]["dur"] == pytest.approx(4.0)  # S3 part only
    assert sfx["TAIL"]["at"] == pytest.approx(13.0) and sfx["TAIL"]["dur"] == pytest.approx(9.0)  # to the end
    assert out["music"]["offset_s"] == pytest.approx(7.0)            # S3 + 1.0
    assert out["protected"] == [[pytest.approx(17.0), pytest.approx(20.0)]]
    assert out["timeline"]["dropped_shots"] == ["S2"]
    assert out["timeline"]["cur_total_s"] == 22.0


def test_vo_in_a_dropped_shot_needs_a_human(tmp_path):
    plan = _plan(tmp_path)
    plan["vo"]["lines"][0]["at"] = 7.0  # in S2
    with pytest.raises(PlacementError, match="dropped shot S2"):
        am.remap_plan_to_edit(plan, _edited())


def test_music_end_align():
    m = {"offset_s": 7.0, "src_start_s": 0.0, "end_align": True}
    assert am.music_src_start(m, 22.0, 20.0) == pytest.approx(5.0)   # 15 s of bed: its last 15 s
    assert am.music_src_start(m, 22.0, 10.0) == 0.0
    assert am.music_src_start({"offset_s": 7.0, "src_start_s": 1.5}, 22.0, None) == 1.5


def test_load_audio_plan_applies_the_current_edit(tmp_path, monkeypatch):
    from hfvg.activities import audio as audio_act
    ep = tmp_path / "ep77"
    (ep / "audio").mkdir(parents=True)
    (ep / "edit").mkdir()
    raw = {"vo": {"lines": [{"id": "L1", "path": "a.wav", "at": 12.5}]},
           "timeline_ref": am.timeline_from_manifest(REF)}
    (ep / "audio" / "audio_plan.json").write_text(json.dumps(raw))
    m = copy.deepcopy(REF)
    m["clips"] = [c for c in m["clips"] if c["id"] != "S2"]
    m["total_duration"] = 24.0
    (ep / "edit" / "assembly_manifest.json").write_text(json.dumps(m))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "false")
    plan = asyncio.run(audio_act.load_audio_plan("ep77", None))
    assert plan["vo"]["lines"][0]["at"] == pytest.approx(6.5)
    assert plan["timeline"]["dropped_shots"] == ["S2"]
    (ep / "edit" / "assembly_manifest.json").unlink()
    with pytest.raises(Exception, match="no edit manifest"):
        asyncio.run(audio_act.load_audio_plan("ep77", None))
