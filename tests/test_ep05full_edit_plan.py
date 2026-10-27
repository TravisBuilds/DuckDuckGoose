"""Ep05 FULL: reused LOCKED clips + A03->F11 cross-dissolve spliced by the harness (edit plan).

Studio's shots.json lists 4 locked teaser clips (``reuse`` block: clip + sha256, trims) between
the new shots and a top-level ``dissolves`` {"A03->F11": 1.0}. The harness stages the spec into a
generation-only shots.json + edit_plan.json, and assemble_mute_edit splices the locked clips in
edit order (checksum verified) and renders the dissolve. No hand splicing.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.media import assemble_mute_edit
from hfvg.edit_plan import EditPlanError, edit_plan_from_spec, generation_spec, stage_episode
from hfvg.qc.motion import check_motion  # noqa: F401  (manifest format contract)
from tests.test_picture_assembly_round3 import HB, dominant, make_color_clip, needs_ffmpeg, probe, rgb_at


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def spec_for(tmp_path, locked):
    return {
        "episode_id": "epfull",
        "dissolves": {"L2->G2": 1.0},
        "shots": [
            {"id": "G1", "duration": 2, "screen_s": 2, "count": 1, "duck_present": False, "prompt": "p"},
            {"id": "L1", "duration": 3, "screen_s": 2.0, "trim_head_s": 0.5, "count": 3, "duck_present": True,
             "reuse": {"clip": str(locked["L1"]), "clip_sha256": sha(locked["L1"]), "source": "teaser"}},
            {"id": "L2", "duration": 2, "screen_s": 2.0, "count": 3, "duck_present": False,
             "reuse": {"clip": str(locked["L2"]), "clip_sha256": sha(locked["L2"])}},
            {"id": "G2", "duration": 2, "screen_s": 2, "count": 3, "duck_present": False, "prompt": "p"},
        ],
    }


def test_generation_spec_drops_reuse_shots_like_readme_jq(tmp_path):
    spec = {"episode_id": "e", "dissolves": {}, "shots": [{"id": "A"}, {"id": "B", "reuse": {"clip": "x", "clip_sha256": "y"}}]}
    assert [s["id"] for s in generation_spec(spec)["shots"]] == ["A"]
    assert [s["id"] for s in spec["shots"]] == ["A", "B"]  # input untouched


def test_edit_plan_rejects_non_consecutive_dissolve():
    spec = {"shots": [{"id": "A"}, {"id": "B"}, {"id": "C"}], "dissolves": {"A->C": 1.0}}
    with pytest.raises(EditPlanError):
        edit_plan_from_spec(spec)


@needs_ffmpeg
def test_stage_verifies_checksums(tmp_path):
    locked = {"L1": make_color_clip(tmp_path / "l1.mp4", [("green", 3.0)]),
              "L2": make_color_clip(tmp_path / "l2.mp4", [("blue", 2.0)])}
    spec = spec_for(tmp_path, locked)
    (tmp_path / "full.json").write_text(json.dumps(spec))
    plan = stage_episode(tmp_path / "full.json", tmp_path / "ep")
    assert plan["order"] == ["G1", "L1", "L2", "G2"]
    assert plan["reuse"]["L1"]["trim_in"] == 0.5 and plan["reuse"]["L1"]["trim_out"] == 2.5
    assert [s["id"] for s in json.loads((tmp_path / "ep/shots.json").read_text())["shots"]] == ["G1", "G2"]
    spec["shots"][1]["reuse"]["clip_sha256"] = "0" * 64
    (tmp_path / "bad.json").write_text(json.dumps(spec))
    with pytest.raises(EditPlanError, match="checksum"):
        stage_episode(tmp_path / "bad.json", tmp_path / "ep2")


@needs_ffmpeg
@pytest.mark.asyncio
async def test_assembly_splices_reused_clips_and_dissolve(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    locked = {"L1": make_color_clip(tmp_path / "l1.mp4", [("red", 0.5), ("green", 2.5)]),
              "L2": make_color_clip(tmp_path / "l2.mp4", [("blue", 2.0)])}
    g1 = make_color_clip(tmp_path / "g1.mp4", [("red", 2.0)])
    g2 = make_color_clip(tmp_path / "g2.mp4", [("green", 2.0)])
    (tmp_path / "full.json").write_text(json.dumps(spec_for(tmp_path, locked)))
    stage_episode(tmp_path / "full.json", tmp_path / "episodes" / "epfull")

    # The workflow only knows the generated shots
    shots = [{"shot_id": "G1", "duration": 2.0}, {"shot_id": "G2", "duration": 2.0}]
    res = await HB().run(assemble_mute_edit, "epfull", shots, {"G1": str(g1), "G2": str(g2)}, None)
    m = res["manifest"]
    assert [c["id"] for c in m["clips"]] == ["G1", "L1", "L2", "G2"]
    assert [c.get("reused", False) for c in m["clips"]] == [False, True, True, False]
    assert m["clips"][1]["trim_in"] == 0.5 and m["clips"][1]["shot_meta"]["count"] == 3
    assert m["dissolves"] == {"L2": 1.0} and m["dissolve_pairs"] == {"L2->G2": 1.0}
    assert res["duration"] == pytest.approx(2 + 2 + 2 + 2 - 1.0, abs=1e-6)
    info = probe(res["assembly_path"])
    v = [s for s in info["streams"] if s["codec_type"] == "video"][0]
    assert int(v["nb_read_frames"]) == 7 * 30
    p = res["assembly_path"]
    assert dominant(rgb_at(p, 1.0)) == "red"     # G1
    assert dominant(rgb_at(p, 2.3)) == "green"   # L1 after its 0.5 s red head was trimmed
    assert dominant(rgb_at(p, 4.3)) == "blue"    # L2 before the dissolve
    mid = rgb_at(p, 5.5)                         # dissolve midpoint: blue and green mixed
    assert mid[1] > 60 and mid[2] > 60
    assert dominant(rgb_at(p, 6.6)) == "green"   # G2 after the dissolve


@needs_ffmpeg
@pytest.mark.asyncio
async def test_assembly_refuses_tampered_locked_clip(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    locked = {"L1": make_color_clip(tmp_path / "l1.mp4", [("green", 3.0)]),
              "L2": make_color_clip(tmp_path / "l2.mp4", [("blue", 2.0)])}
    g1 = make_color_clip(tmp_path / "g1.mp4", [("red", 2.0)])
    g2 = make_color_clip(tmp_path / "g2.mp4", [("green", 2.0)])
    (tmp_path / "full.json").write_text(json.dumps(spec_for(tmp_path, locked)))
    stage_episode(tmp_path / "full.json", tmp_path / "episodes" / "epfull")
    make_color_clip(locked["L2"], [("red", 2.0)])  # locked clip replaced after staging
    shots = [{"shot_id": "G1", "duration": 2.0}, {"shot_id": "G2", "duration": 2.0}]
    with pytest.raises(Exception) as ei:
        await HB().run(assemble_mute_edit, "epfull", shots, {"G1": str(g1), "G2": str(g2)}, None)
    assert "checksum" in str(ei.value)


@needs_ffmpeg
@pytest.mark.asyncio
async def test_no_edit_plan_keeps_hard_cuts(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    g1 = make_color_clip(tmp_path / "g1.mp4", [("red", 1.0)])
    g2 = make_color_clip(tmp_path / "g2.mp4", [("green", 1.0)])
    shots = [{"shot_id": "G1", "duration": 1.0}, {"shot_id": "G2", "duration": 1.0}]
    res = await HB().run(assemble_mute_edit, "plain", shots, {"G1": str(g1), "G2": str(g2)}, None)
    assert res["manifest"]["dissolves"] == {} and res["manifest"]["edit_plan"] is None
    assert res["duration"] == pytest.approx(2.0)


def test_ep05full_spec_stages_cleanly(tmp_path):
    """Task 9: Use in-repo fixture instead of hardcoded /workspace path."""
    # Create a minimal spec with reuse and dissolves
    spec = {
        "shots": [
            {"id": "A01", "prompt": "shot 1", "count": 1, "trim_head_s": 0.5, "reuse": {"clip": "/tmp/a01.mp4", "sha256": "abc123"}, "screen_s": 4.5},
            {"id": "A02", "prompt": "shot 2", "count": 1, "reuse": {"clip": "/tmp/a02.mp4", "sha256": "def456"}, "screen_s": 3.0},
            {"id": "A03", "prompt": "shot 3", "count": 1, "reuse": {"clip": "/tmp/a03.mp4", "sha256": "ghi789"}, "screen_s": 4.0},
            {"id": "F11", "prompt": "shot F11", "count": 1, "model": "kling", "duration": 5, "screen_s": 5.0},
            {"id": "A04", "prompt": "shot 4", "count": 1, "reuse": {"clip": "/tmp/a04.mp4", "sha256": "jkl012"}, "screen_s": 10.0},
        ],
        "dissolves": {
            "A03->F11": 1.0
        }
    }
    plan = edit_plan_from_spec(spec)
    assert sorted(plan["reuse"]) == ["A01", "A02", "A03", "A04"]
    assert plan["dissolves"] == {"A03->F11": 1.0}
    assert plan["reuse"]["A01"]["trim_in"] == 0.5 and plan["reuse"]["A04"]["trim_out"] == 10.0
    assert len(generation_spec(spec)["shots"]) == 1  # Only F11 needs generation
    screen = sum(s["screen_s"] for s in spec["shots"]) - 1.0
    assert screen == pytest.approx(25.5)  # 4.5 + 3.0 + 4.0 + 5.0 + 10.0 - 1.0 (dissolve)


# ── BEATMAP: Studio table with an extra "N" column and "kling" model names ──
STUDIO_TABLE = """# x
| # | ID | Screen (gen) [⇥] | Room | TOD | Chars | N | Action | Purpose | In | Out | Model | Duck state |
|---|----|---|---|---|---|---|---|---|---|---|---|---|
| 1 | F01 | 6 (6) | path | GOLD | R H M | 3 | walk | est | 0.0 | 6.0 | kling | absent |
| 2 | A01 | 5.5 (6.04 → trim head 0.5) REUSE | pool | GOLD | R H M | 3 | doze | t | 6 | 11.5 | kling (locked) | absent |
| 3 | F14 | 5 (5) ⇥ | boardwalk | OVER | D M | 1 | nudge | fix | 11.5 | 16.5 | kling | planks |
"""


def test_header_driven_beatmap_parses_studio_table(tmp_path):
    from hfvg.episode_parser import parse_beatmap
    p = tmp_path / "BEATMAP.md"
    p.write_text(STUDIO_TABLE)
    shots = parse_beatmap(p)
    assert [s["shot_id"] for s in shots] == ["F01", "F14"]  # REUSE row is not generated
    assert shots[0]["model"] == "K" and shots[0]["gen_time"] == 6.0 and shots[0]["characters"] == ["R", "H", "M"]
    assert shots[1]["has_end_frame"] is True and shots[1]["duck_role"] == "host"


def test_header_driven_beatmap_refuses_unparseable_row(tmp_path):
    from hfvg.episode_parser import parse_beatmap
    p = tmp_path / "BEATMAP.md"
    p.write_text(STUDIO_TABLE.replace("| 6 (6) |", "| six |"))
    with pytest.raises(ValueError, match="F01"):
        parse_beatmap(p)
