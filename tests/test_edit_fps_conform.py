"""GAP-33 (ep05v2 G3 round 2, 2026-10-27, Studio item 8: judder). The mute edit was always normalized to
30 fps, so the 24 fps generated clips got every 4th frame repeated (24->30), visible as judder on slow
moves (V02, V08, V20). An episode edit plan may now set "fps": 24 to conform the edit to the sources:
no repeated frames, exact 24 fps frame counts, and the input key changes so the edit is rebuilt."""
from __future__ import annotations

import json
import subprocess

import pytest
from temporalio.exceptions import ApplicationError

from hfvg.activities.media import assemble_mute_edit, edit_fps
from tests.test_picture_assembly_round3 import HB, MOVING, make_clip, needs_ffmpeg, probe


def _frame_diffs(path):
    """Mean absolute difference between consecutive decoded frames (64x114 gray). A repeated frame
    is ~0 even after lossy encoding; testsrc2 moves every frame."""
    import numpy as np
    w, h = 64, 114
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", f"scale={w}:{h},format=gray",
                          "-f", "rawvideo", "-"], capture_output=True, timeout=120, check=True).stdout
    f = np.frombuffer(raw, np.uint8).reshape(-1, h, w).astype(np.float32)
    return np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))


def _repeats(path):
    return int((_frame_diffs(path) < 0.5).sum())


def test_edit_fps_values():
    assert edit_fps(None) == 30 and edit_fps({"order": []}) == 30 and edit_fps({"fps": None}) == 30
    assert edit_fps({"fps": 24}) == 24 and edit_fps({"fps": 25}) == 25
    for bad in (23, 60, "24", 24.0, True):
        with pytest.raises(ApplicationError):
            edit_fps({"fps": bad})


@needs_ffmpeg
@pytest.mark.asyncio
async def test_edit_plan_fps_24_conforms_without_repeated_frames(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    a = make_clip(tmp_path / "a.mp4", 2.0, MOVING, rate=24)
    b = make_clip(tmp_path / "b.mp4", 1.5, MOVING, rate=24)
    shots = [{"shot_id": "A01", "duration": 2.0}, {"shot_id": "A02", "duration": 1.5}]
    clips = {"A01": str(a), "A02": str(b)}
    ep = tmp_path / "episodes" / "ep33"
    ep.mkdir(parents=True)
    plan = {"version": 1, "episode_id": "ep33", "order": ["A01", "A02"], "reuse": {}, "dissolves": {}}
    (ep / "edit_plan.json").write_text(json.dumps(plan))

    # default (no fps): 30 fps with 24->30 frame repeats -- the judder Studio flagged
    r30 = await HB().run(assemble_mute_edit, "ep33", shots, clips, str(tmp_path / "out30"))
    v30 = [s for s in probe(r30["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert v30["r_frame_rate"] == "30/1"
    assert _repeats(r30["assembly_path"]) > 0

    plan["fps"] = 24
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    r24 = await HB().run(assemble_mute_edit, "ep33", shots, clips, str(tmp_path / "out24"))
    v24 = [s for s in probe(r24["assembly_path"])["streams"] if s["codec_type"] == "video"][0]
    assert v24["r_frame_rate"] == "24/1", v24
    assert int(v24["nb_read_frames"]) == 48 + 36
    assert [c["frames"] for c in r24["manifest"]["clips"]] == [48, 36]
    assert r24["manifest"]["format"]["fps"] == 24
    assert r24["duration"] == pytest.approx(3.5, abs=1e-6)
    assert _repeats(r24["assembly_path"]) == 0
    assert r24["input_key"] != r30["input_key"]  # an existing 30 fps edit is never reused as 24

    plan["fps"] = 23
    (ep / "edit_plan.json").write_text(json.dumps(plan))
    with pytest.raises(ApplicationError, match="fps"):
        await HB().run(assemble_mute_edit, "ep33", shots, clips, str(tmp_path / "out23"))
