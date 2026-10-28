"""Ep05 G3 free fixes (2026-10-26): harness gaps found while applying them.

GAP-5  subtitle line breaks written in the plan ("\\n") were collapsed and re-balanced; an
       explicit break (<= 2 lines) is now honoured in the .ass and .srt.
GAP-6  the logo was always vertically centred; plan.logo.y_frac places its centre (0.5 default).
GAP-7  an audio-plan edit could not rebuild the delivery for an unchanged picture:
       request_delivery_rebuild(reason) signal + POST /api/episodes/{id}/delivery/rebuild, and
       G_FINAL_CUT approval is refused while a delivery rebuild is due or running.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError, build_cues, mix_and_deliver, normalize_plan, wrap_two_lines
from tests.test_audio_delivery import FAST_ENCODE, make_noise, make_tone


def test_explicit_break_is_honoured():
    assert wrap_two_lines("Today's guests are…\na family of char_c.", 24) == \
        ["Today's guests are…", "a family of char_c."]
    # without a break: unchanged balanced wrap (char_c is shorter so wraps differently)
    assert wrap_two_lines("Today's guests are… a family of char_c.", 24) == \
        ["Today's guests are…", "a family of char_c."]
    assert wrap_two_lines("Some tea… quietly.", 24) == ["Some tea… quietly."]
    # more than two explicit lines: falls back to the balanced two-line wrap
    assert len(wrap_two_lines("a b\nc d\ne f", 4)) == 2


def test_break_reaches_ass_and_srt(tmp_path):
    cues = build_cues([{"id": "L01", "at": 0.6, "dur": 4.0, "subtitle": "Today's guests are…\na family of char_c."}], 24)
    plan = normalize_plan({}, tmp_path)
    audio_mix.write_ass(cues, str(tmp_path / "s.ass"), plan["subtitles"])
    audio_mix.write_srt(cues, str(tmp_path / "s.srt"))
    assert "Today's guests are…\\Na family of char_c." in (tmp_path / "s.ass").read_text()
    assert "Today's guests are…\na family of char_c.\n" in (tmp_path / "s.srt").read_text()


def test_logo_y_frac_validation(tmp_path):
    assert normalize_plan({"logo": {"path": "l.png", "y_frac": 0.16}}, tmp_path)["logo"]["y_frac"] == 0.16
    with pytest.raises(AudioInputError):
        normalize_plan({"logo": {"path": "l.png", "y_frac": 1.5}}, tmp_path)


def _luma(path: str, t: float, y: int) -> int:
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", path, "-frames:v", "1",
                          "-vf", f"crop=100:20:(iw-100)/2:{y},scale=1:1,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return out[0]


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_logo_placed_high_and_phone_size_target(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_mix, "episodes_root", lambda: tmp_path / "eps")
    pic = tmp_path / "noise.mp4"  # noisy picture so the phone size target actually bites
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "nullsrc=s=540x960:r=30:d=6",
                    "-vf", "geq=random(1)*255:128:128", "-c:v", "libx264", "-preset", "ultrafast",
                    "-qp", "10", "-pix_fmt", "yuv420p", str(pic)], check=True)
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=400x100",
                    "-frames:v", "1", str(logo)], check=True)
    make_tone(tmp_path / "L01.wav", 1.5, 440, -6)
    make_noise(tmp_path / "bed.wav", 8.0, -30)
    raw = {"vo": {"lines": [{"id": "L01", "at": 0.5, "path": str(tmp_path / "L01.wav"), "subtitle": "One"}]},
           "music": {"path": str(tmp_path / "bed.wav")},
           "logo": {"path": str(logo), "visible_s": 2.0, "fade_in_s": 0.5, "y_frac": 0.15},
           "master": {"preset": "ultrafast"},
           "phone": {"preset": "ultrafast", "max_mb": 0.6, "audio_kbps": 96, "min_video_kbps": 50}}
    plan = normalize_plan(raw, tmp_path / "eps" / "ep93")
    m = await mix_and_deliver("ep93", str(pic), plan, root=tmp_path / "eps")
    nosubs = m["outputs"]["nosubs"]["path"]
    # logo (white, ~70 px tall at 540 wide) centred at 15 % of 960 = 144 px; the frame centre is not covered
    assert _luma(nosubs, 5.8, 134) > 230
    assert _luma(nosubs, 5.8, 470) < 200
    assert m["phone"]["size_bytes"] <= 600_000 and m["phone"]["within_limit"], m["phone"]


# ───────────────────────────── GAP-7: delivery rebuild after a plan edit ─────────────────────────────

import asyncio  # noqa: E402
import json  # noqa: E402

from temporalio.testing import WorkflowEnvironment  # noqa: E402

from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter  # noqa: E402
from tests.lint_helpers import lint_ok  # noqa: E402
from tests.test_audio_delivery_workflow import PROMPT, _delivered, write_audio  # noqa: E402
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: E402,F401
from tests.test_picture_assembly_round3 import (  # noqa: E402,F401
    MOVING, ClipFactory, _to_final_cut, _worker, needs_ffmpeg, no_counter,
)
from api.main import final_cut_blockers  # noqa: E402


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_delivery_rebuild_after_plan_edit(dry_env, tmp_path, no_counter):  # noqa: F811
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    root = write_audio(tmp_path, "ep94")
    h = Harness({})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
            _, handle, state = await _to_final_cut(env, h, tmp_path, "ep94", lint_ok(PROMPT))
            state = await _wait(handle, _delivered, timeout=180, what="delivery 1")
            assert state["delivery"]["delivery_version"] == 1 and state["delivery"]["qc"]["verdict"] == "PASS"
            plan_path = root / "audio" / "audio_plan.json"
            plan = json.loads(plan_path.read_text())
            plan["vo"]["lines"][0]["subtitle"] = "First\nline."
            plan_path.write_text(json.dumps(plan))
            await handle.signal("request_delivery_rebuild", "  ")  # ignored: no reason
            await asyncio.sleep(1.0)
            assert (await handle.query("get_state"))["delivery"]["delivery_version"] == 1
            await handle.signal("request_delivery_rebuild", "Operator: sub wrap fix")
            state = await _wait(handle, lambda s: s["delivery"]["delivery_version"] == 2
                                and s["delivery"]["phase"] == "done", timeout=180, what="delivery 2")
            assert state["delivery"]["qc"]["verdict"] == "PASS", state["delivery"]["qc"]
            assert state["delivery_rebuilds"][0]["reason"] == "Operator: sub wrap fix"
            assert "First\nline." in Path(state["delivery"]["paths"]["srt"]).read_text()
            assert state["delivery"]["due"] is False
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=60)


def test_final_cut_refused_while_delivery_rebuild_pending():
    base = {"current_gate": "G_FINAL_CUT", "assembly": {}, "delivery": {"due": False, "phase": "done"}}
    assert final_cut_blockers(base) == []
    assert final_cut_blockers({**base, "delivery": {"due": True, "phase": "done"}})
    assert final_cut_blockers({**base, "delivery": {"due": False, "phase": "final_mix"}})
