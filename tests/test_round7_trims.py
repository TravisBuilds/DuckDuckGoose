"""Round 7 trims: per-shot trim_head_s / trim_tail_s are validated against the clip duration,
mapped onto the existing trim_in / trim_out window (one mechanism), applied by the real assembly,
and the resulting picture length is reported."""

import subprocess
from pathlib import Path

import pytest
import temporalio.activity

from hfvg.shot_data import ShotDataError, parse_shots, picture_seconds
from tests.test_picture_assembly_round3 import make_clip, needs_ffmpeg

BASE = {"count": 1, "characters": ["D"], "wet": False, "model": "kling", "duration": 6,
        "end_frame": False, "prompt": "Duck walks", "ref_keys": []}


def _plans(*extra):
    return parse_shots({"shots": [{**BASE, "id": f"A0{i + 1}", **e} for i, e in enumerate(extra)]})


def test_head_and_tail_map_onto_the_trim_window():
    plans = _plans({"trim_head_s": 0.5}, {"trim_tail_s": 1.25}, {"trim_head_s": 1, "trim_tail_s": 2}, {})
    assert plans["A01"]["trim"] == {"in": 0.5, "out": 6.0}
    assert plans["A02"]["trim"] == {"in": 0.0, "out": 4.75}
    assert plans["A03"]["trim"] == {"in": 1.0, "out": 4.0}
    assert plans["A04"]["trim"] == {"in": 0.0, "out": 6.0}
    assert picture_seconds([{"shot_id": k, **v} for k, v in plans.items()]) == 5.5 + 4.75 + 3.0 + 6.0


@pytest.mark.parametrize("extra, needle", [
    ({"trim_head_s": 3, "trim_tail_s": 3}, "must be shorter than"),
    ({"trim_head_s": 6.5}, "must be shorter than"),
    ({"trim_tail_s": -0.5}, ">= 0"),
    ({"trim_head_s": "half"}, "trim_head_s"),
    ({"trim_head_s": True}, "trim_head_s"),
    ({"trim_head_s": 0.5, "trim_in": 0.5}, "not both"),
    ({"trim_out": 7}, "trim_out"),
])
def test_invalid_trims_fail_loudly(extra, needle):
    with pytest.raises(ShotDataError, match=needle):
        _plans(extra)


def _probe(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_assembly_applies_head_and_tail_trims(tmp_path, monkeypatch):
    from hfvg.activities.media import assemble_mute_edit
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(temporalio.activity, "heartbeat", lambda *a, **k: None)
    plans = _plans({"trim_head_s": 0.5}, {"trim_tail_s": 1.0})
    clips = {sid: str(make_clip(tmp_path / f"{sid}.mp4", 6.0)) for sid in plans}
    shots = [{"shot_id": sid, "duration": 6.0, "trim_in": p["trim"]["in"], "trim_out": p["trim"]["out"]}
             for sid, p in plans.items()]
    out = await assemble_mute_edit("ep90", shots, clips, str(tmp_path / "edit"))
    segs = {c["id"]: c for c in out["manifest"]["clips"]}
    assert segs["A01"]["trim_in"] == 0.5 and abs(segs["A01"]["seg_s"] - 5.5) < 0.05
    assert segs["A02"]["trim_in"] == 0.0 and abs(segs["A02"]["seg_s"] - 5.0) < 0.05
    assert abs(_probe(out["assembly_path"]) - 10.5) < 0.1
    assert abs(out["duration"] - picture_seconds([{"shot_id": k, **v} for k, v in plans.items()])) < 0.1


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_assembly_refuses_a_trim_past_the_real_clip(tmp_path, monkeypatch):
    """The planned duration says 6 s but the delivered clip is 4 s: a 6 s window is refused."""
    from temporalio.exceptions import ApplicationError
    from hfvg.activities.media import assemble_mute_edit
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setattr(temporalio.activity, "heartbeat", lambda *a, **k: None)
    plans = _plans({"trim_head_s": 0.5})
    clip = str(make_clip(tmp_path / "A01.mp4", 4.0))
    shots = [{"shot_id": "A01", "duration": 6.0, "trim_in": 0.5, "trim_out": plans["A01"]["trim"]["out"]}]
    with pytest.raises(ApplicationError, match="exceeds the clip"):
        await assemble_mute_edit("ep91", shots, {"A01": clip}, str(tmp_path / "edit"))
