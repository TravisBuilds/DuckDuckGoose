"""Ep05 live-run harness gaps (2026-10-26):

GAP-1  a clip escalated as needs_review (qc_fail_if with no checker) could not be kept by a human
       after G2: accept_clip_review keeps it (no spend) and the edit is rebuilt with it.
GAP-2  VO atempo post step (handbook: atempo 0.90 per generated line, before placement).
GAP-3  logo overlay in the delivery (Ep01 ending template).
GAP-4  subtitle ASS style override (handbook WarmVacation).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from hfvg import audio_mix
from hfvg.audio_mix import AudioInputError, mix_and_deliver, normalize_plan, write_ass
from tests.lint_helpers import lint_ok
from tests.test_audio_delivery import FAST_ENCODE, make_noise, make_picture, make_tone
from tests.test_moderation_recovery import Harness, _child_result, _until, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)

WARM = ("Style: WarmVacation,Comfortaa,58,&H00E8F8FF,&H000000FF,&H002A3A5C,&H64000000,0,0,0,0,100,100,"
        "0.5,0,1,5,2,2,70,70,240,1")


# ───────────────────────────── GAP-1: keep a needs_review clip ─────────────────────────────

def _escalating_review():
    @activity.defn(name="review_clip")
    async def review(asset_url, rubric, episode_id=None, shot_id=None):
        return {"passed": False, "escalate": True,
                "issues": ["qc_fail_if needs review: tray vanishes: NEEDS_REVIEW (no checker)"],
                "constraints": {"status": "NEEDS_REVIEW", "declared": ["tray vanishes"],
                                "results": [{"constraint": "tray vanishes", "status": "NEEDS_REVIEW"}]}}
    return review


@needs_ffmpeg
class TestAcceptClipReview:
    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_needs_review_clip_kept_and_edit_rebuilt(self, dry_env, tmp_path, no_counter):
        h = Harness({})
        clips = ClipFactory(tmp_path, [MOVING])
        async with await WorkflowEnvironment.start_local() as env:
            from temporalio.worker import Worker
            from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
            from hfvg.workflows.shot import ShotWorkflow
            acts = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name
                    not in ("save_asset_locally", "review_clip")] + [clips.activity(), _escalating_review()]
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                              activities=acts):
                beatmap = _write_episode(tmp_path, "ep91", lint_ok("A duck waddles through the snow"))
                handle = await env.client.start_workflow(
                    "EpisodeWorkflowV2", args=["ep91", beatmap, True, None, None, None],
                    id="ep91", task_queue=TASK_QUEUE)
                await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
                await handle.execute_update("approve_g1_shot_plan")
                await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
                await _until(lambda: h.still_calls, what="still")
                await asyncio.sleep(0.5)
                await handle.execute_update("approve_g2_scene_stills")
                result = await _child_result(env, "ep91-shot-A01", timeout=90)
                # root fix: the needs_review result carries the local clip (as completed does)
                assert result["status"] == "needs_review" and result["clip_path"] == clips.saved[0], result
                child = await env.client.get_workflow_handle("ep91-shot-A01").query("get_state")
                assert child["clip_path"] == clips.saved[0]
                state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                                    and (s["assembly"]["qc"] or {}).get("verdict"), timeout=90, what="edit 1")
                assert state["assembly"]["qc"]["verdict"] == "NO_CLIPS", state["assembly"]["qc"]
                assert state["assembly"]["qc"]["excluded_shots"] == [{"shot_id": "A01", "status": "needs_review"}]

                with pytest.raises(WorkflowUpdateFailedError):  # a reason is required
                    await handle.execute_update("accept_clip_review", args=["A01", "  ", None])
                with pytest.raises(WorkflowUpdateFailedError):  # unknown shot
                    await handle.execute_update("accept_clip_review", args=["A09", "ok", None])
                out = await handle.execute_update("accept_clip_review",
                                                  args=["A01", "Operator: keep as is", None])
                assert out["clip_path"] == clips.saved[0]
                state = await _wait(handle, lambda s: s["assembly"]["edit_version"] == 2
                                    and (s["assembly"]["qc"] or {}).get("edit_version") == 2,
                                    timeout=120, what="rebuilt edit")
                assert [c["id"] for c in state["assembly"]["manifest"]["clips"]] == ["A01"]
                assert state["assembly"]["manifest"]["clips"][0]["source"] == clips.saved[0]
                assert state["shot_outcomes"]["A01"]["status"] == "completed"
                assert state["clip_review_acceptances"]["A01"]["reason"] == "Operator: keep as is"
                assert len(h.clip_calls) == 1, "keeping a clip must not spend"
                with pytest.raises(WorkflowUpdateFailedError):  # now completed: nothing to accept
                    await handle.execute_update("accept_clip_review", args=["A01", "again", None])
                await handle.terminate("test done")


# ───────────────────────────── GAP-2/3/4: audio + delivery ─────────────────────────────

def test_atempo_applies_to_generated_lines_only():
    plan = normalize_plan({"vo": {"atempo": 0.9, "lines": [
        {"id": "L01", "at": 0.5, "text": "a"},
        {"id": "L02", "at": 3.0, "path": "locked.wav"},
        {"id": "L03", "at": 5.0, "path": "locked.wav", "atempo": 0.95},
        {"id": "L04", "at": 7.0, "text": "b", "atempo": None}]}}, "/tmp")
    assert [ln["atempo"] for ln in plan["vo"]["lines"]] == [0.9, None, 0.95, None]
    assert normalize_plan({"vo": {"lines": [{"id": "L01", "at": 0, "text": "a"}]}}, "/tmp")["vo"]["lines"][0]["atempo"] is None
    with pytest.raises(AudioInputError):
        normalize_plan({"vo": {"atempo": 3.0, "lines": []}}, "/tmp")
    # re-normalizing a resolved plan (generated path filled in) keeps the decision
    again = normalize_plan({"vo": {"atempo": 0.9, "lines": [{**plan["vo"]["lines"][0], "path": "gen.mp3"}]}}, "/tmp")
    assert again["vo"]["lines"][0]["atempo"] == 0.9


def test_logo_and_ass_style_validation(tmp_path):
    plan = normalize_plan({"logo": {"path": "logo.png"}, "subtitles": {"ass_style": WARM}}, tmp_path / "ep")
    assert plan["logo"] == {"path": str(tmp_path / "ep" / "logo.png"), "width_frac": 0.62,
                            "fade_in_s": 2.5, "visible_s": 6.5, "y_frac": 0.5}
    assert normalize_plan({}, tmp_path)["logo"] is None
    with pytest.raises(AudioInputError):
        normalize_plan({"subtitles": {"ass_style": "Style: X,Arial"}}, tmp_path)
    ass = tmp_path / "s.ass"
    write_ass([{"start": 1.0, "end": 2.0, "lines": ["Hi"]}], str(ass), plan["subtitles"])
    text = ass.read_text()
    assert WARM in text and "Style: Default" not in text and ",WarmVacation,," in text


def _luma(path: str, t: float) -> float:
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", path, "-frames:v", "1",
                          "-vf", "crop=200:60:(iw-200)/2:(ih-60)/2,scale=1:1,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True).stdout
    return out[0]


@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_delivery_applies_atempo_and_logo(tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.setattr(audio_mix, "episodes_root", lambda: tmp_path / "eps")
    ep = tmp_path / "eps" / "ep92"
    pic = tmp_path / "black.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=black:s=540x960:r=30:d=8",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(pic)], check=True)
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=400x100",
                    "-frames:v", "1", str(logo)], check=True)
    make_tone(tmp_path / "L01.wav", 1.8, 440, -6)
    make_tone(tmp_path / "L02.wav", 1.0, 660, -6)
    make_noise(tmp_path / "bed.wav", 10.0, -30)
    raw = {"vo": {"atempo": 0.9, "lines": [
        {"id": "L01", "at": 0.5, "text": "one", "subtitle": "One"},
        {"id": "L02", "at": 3.5, "path": str(tmp_path / "L02.wav"), "subtitle": "Two"}]},
        "music": {"path": str(tmp_path / "bed.wav")},
        "logo": {"path": str(logo), "visible_s": 3.0, "fade_in_s": 1.0}, **FAST_ENCODE}
    plan = normalize_plan(raw, ep)
    plan["vo"]["lines"][0]["path"] = str(tmp_path / "L01.wav")  # as resolved after generation
    m = await mix_and_deliver("ep92", str(pic), plan, root=tmp_path / "eps")
    l1, l2 = m["vo_lines"]
    assert l1["atempo"] == 0.9 and abs(l1["dur"] - 1.8 / 0.9) < 0.05, l1
    assert l1["source_path"] == str(tmp_path / "L01.wav")
    assert l2["atempo"] is None and abs(l2["dur"] - 1.0) < 0.05, l2
    assert m["logo"]["start_s"] == 5.0
    nosubs = m["outputs"]["nosubs"]["path"]
    assert _luma(nosubs, 2.0) < 30, "no logo before the last visible_s"
    assert _luma(nosubs, 7.5) > 200, "logo fully in after the fade"
