"""Ep11 harness gaps: per-episode edit recipe (slow + crossfade + tail hold + 24 fps + post filters)
in the mute assembly, and the logo timed after a VO line with a lower-frame darken + hold check."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from hfvg import audio_mix
from hfvg.activities.media import (
    EDIT_RECIPE_NAME,
    assemble_mute_edit,
    load_edit_recipe,
    recipe_total_s,
    xfade_offsets,
)
from hfvg.audio_mix import AudioInputError, delivery_qc, logo_start_s, mix_and_deliver, normalize_plan
from hfvg.qc.motion import check_motion
from tests.test_ep05_g3_fixes import _luma, make_noise, make_tone
from tests.test_picture_assembly_round3 import make_clip, needs_ffmpeg


def _probe(path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, check=True, text=True).stdout
    return json.loads(out)


def test_recipe_absent_and_validation(tmp_path):
    assert load_edit_recipe(tmp_path / "none.json") is None
    p = tmp_path / EDIT_RECIPE_NAME
    p.write_text(json.dumps({"fps": 24, "speed": 1.095, "xfade_s": 0.55, "tail_hold_s": 1.5,
                             "scale_flags": "lanczos", "post_vf": "unsharp=5:5:0.4,noise=alls=3:allf=t"}))
    r = load_edit_recipe(p)
    assert r["fps"] == 24 and r["speed"] == 1.095 and r["xfade_s"] == 0.55 and r["tail_hold_s"] == 1.5
    for bad in ({"speed": 5}, {"xfade_s": -1}, {"post_vf": "a[x];b"}, {"scale_flags": "lanczos;rm"}, {"fps": "x"}):
        p.write_text(json.dumps(bad))
        with pytest.raises(ApplicationError):
            load_edit_recipe(p)


def test_xfade_offsets_and_total_match_format_md_timeline():
    segs = [6.57] + [5.475] * 8 + [6.57]
    offs = xfade_offsets(segs, 0.55)
    assert offs[0] == pytest.approx(6.02) and offs[1] == pytest.approx(10.945)
    # SH10 starts at 45.42 in format.md; with the 1.5 s hold the picture ends at ~53.49
    assert offs[-1] == pytest.approx(45.42, abs=0.01)
    r = {"xfade_s": 0.55, "tail_hold_s": 1.5}
    assert recipe_total_s(segs, r) == pytest.approx(53.49, abs=0.01)


@pytest.mark.asyncio
@needs_ffmpeg
@pytest.mark.timeout(300)
async def test_assembly_with_recipe_slows_crossfades_and_holds(tmp_path):
    clips = {}
    for sid, secs in (("A", 2.0), ("B", 1.0), ("C", 2.0)):
        clips[sid] = str(make_clip(tmp_path / f"{sid}.mp4", secs, size="360x640", rate=24))
    out = tmp_path / "edit"
    out.mkdir()
    (out / EDIT_RECIPE_NAME).write_text(json.dumps({"fps": 24, "speed": 1.5, "xfade_s": 0.5, "tail_hold_s": 1.0,
                                                    "scale_flags": "lanczos", "post_vf": "unsharp=5:5:0.4"}))
    shots = [{"shot_id": "A", "duration": 2.0}, {"shot_id": "B", "duration": 1.0}, {"shot_id": "C", "duration": 2.0}]
    res = await ActivityEnvironment().run(assemble_mute_edit, "ep99", shots, clips, str(out))
    m = res["manifest"]
    assert [c["seg_s"] for c in m["clips"]] == [3.0, 1.5, 3.0]
    assert m["dissolves"] == {"A": 0.5, "B": 0.5}
    assert m["total_duration"] == pytest.approx(3.0 + 1.5 + 3.0 - 1.0 + 1.0)
    assert [c["start_s"] for c in m["clips"]] == [0.0, 2.5, 3.5]
    info = _probe(res["assembly_path"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"], v["r_frame_rate"], v["profile"]) == (1080, 1920, "24/1", "High")
    assert float(info["format"]["duration"]) == pytest.approx(m["total_duration"], abs=0.05)
    assert not any(s["codec_type"] == "audio" for s in info["streams"])
    rows = check_motion(res["assembly_path"], m)  # windows follow the crossfaded timeline, tail excluded
    assert [r.shot_id for r in rows] == ["A", "B", "C"] and rows[-1].t1 == pytest.approx(6.5, abs=0.01)
    # reuse: same inputs + recipe -> reused; recipe change -> rebuilt
    again = await ActivityEnvironment().run(assemble_mute_edit, "ep99", shots, clips, str(out))
    assert again["reused"] is True
    (out / EDIT_RECIPE_NAME).write_text(json.dumps({"fps": 24, "speed": 1.0, "xfade_s": 0.0, "tail_hold_s": 0.5}))
    third = await ActivityEnvironment().run(assemble_mute_edit, "ep99", shots, clips, str(out))
    assert third["reused"] is False and third["manifest"]["total_duration"] == pytest.approx(5.5)
    assert float(_probe(third["assembly_path"])["format"]["duration"]) == pytest.approx(5.5, abs=0.05)


def test_logo_plan_fields_and_start(tmp_path):
    plan = normalize_plan({"logo": {"path": "l.png", "width_px": 576, "fade_in_s": 1.0, "after_vo_line": "vo10",
                                    "after_vo_gap_s": 0.6, "min_hold_s": 2.0,
                                    "darken_lower": {"frac": 0.45, "amount": 0.15}}}, tmp_path)
    lg = plan["logo"]
    assert lg["width_px"] == 576 and lg["after_vo_line"] == "vo10" and lg["darken_lower"]["amount"] == 0.15
    assert logo_start_s(lg, 53.5, [{"id": "vo10", "at": 46.0, "dur": 3.0}]) == pytest.approx(49.6)
    with pytest.raises(AudioInputError):
        logo_start_s(lg, 53.5, [{"id": "vo09", "at": 40.0, "dur": 3.0}])
    assert logo_start_s({"visible_s": 6.5}, 53.5) == pytest.approx(47.0)
    with pytest.raises(AudioInputError):
        normalize_plan({"logo": {"path": "l.png", "darken_lower": {"amount": 2}}}, tmp_path)


@pytest.mark.asyncio
@needs_ffmpeg
@pytest.mark.timeout(240)
async def test_logo_after_vo_line_darken_and_hold_check(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_mix, "episodes_root", lambda: tmp_path / "eps")
    pic = tmp_path / "gray.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=540x960:r=24:d=6",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(pic)], check=True)
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=white:s=400x100",
                    "-frames:v", "1", str(logo)], check=True)
    make_tone(tmp_path / "L01.wav", 1.0, 440, -6)
    make_noise(tmp_path / "bed.wav", 8.0, -30)
    raw = {"vo": {"lines": [{"id": "L01", "at": 0.5, "path": str(tmp_path / "L01.wav"), "subtitle": "One"}]},
           "music": {"path": str(tmp_path / "bed.wav")},
           "logo": {"path": str(logo), "width_px": 200, "fade_in_s": 0.5, "after_vo_line": "L01",
                    "after_vo_gap_s": 0.6, "min_hold_s": 2.0, "darken_lower": {"frac": 0.4, "amount": 0.3}},
           "subtitles": {"burn": False},
           "master": {"preset": "ultrafast"}, "phone": {"preset": "ultrafast"}}
    plan = normalize_plan(raw, tmp_path / "eps" / "ep94")
    m = await mix_and_deliver("ep94", str(pic), plan, root=tmp_path / "eps")
    assert m["logo"]["start_s"] == pytest.approx(2.1, abs=0.01)
    assert m["logo"]["hold_s"] == pytest.approx(6 - 2.1 - 0.5, abs=0.02)
    nosubs = m["outputs"]["nosubs"]["path"]
    assert _luma(nosubs, 1.0, 470) < 200          # before the fade: no logo
    assert _luma(nosubs, 5.5, 470) > 230          # centred logo after the fade
    assert _luma(nosubs, 5.5, 900) < _luma(nosubs, 1.0, 900) - 15   # lower frame darkened
    assert abs(_luma(nosubs, 5.5, 100) - _luma(nosubs, 1.0, 100)) <= 3  # upper frame untouched
    assert m["subtitles_burned"] is False and "master_subs" not in m["outputs"]
    qc = await delivery_qc(m, plan)
    hold = next(c for c in qc["checks"] if c["name"] == "logo_hold")
    assert hold["ok"]
    plan["logo"]["min_hold_s"] = 5.0
    qc = await delivery_qc(m, plan)
    assert "logo_hold" in qc["failed"]


# ───────────── run lock: a restarted episode (same stable workflow id) reclaims its own lock ─────────────

@pytest.mark.asyncio
async def test_restart_with_same_workflow_id_reclaims_lock(tmp_path, monkeypatch):
    from temporalio.testing import ActivityEnvironment as AE
    from temporalio import activity as tact

    from hfvg.activities import run_lock
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db

    db = str(tmp_path / "s.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    await init_studio_db(db)
    ledger = CreditLedger(db)
    await ledger.init_db()
    assert (await ledger.acquire_run_lock("ep11", "ep11", account_identifier="default"))["acquired"]

    env = AE()
    env.info = tact.Info(**{**env.info.__dict__, "workflow_id": "ep11"})
    res = await env.run(run_lock.acquire_run_lock, "ep11", "ep11", "default")
    assert res["acquired"] is True

    # a DIFFERENT workflow id still may not take the lock while it is held (describe fails here ->
    # time-based check -> lock is fresh -> refused)
    env2 = AE()
    env2.info = tact.Info(**{**env2.info.__dict__, "workflow_id": "ep12"})
    with pytest.raises(ValueError):
        await env2.run(run_lock.acquire_run_lock, "ep12", "ep12", "default")


# ───────────── preflight lint report: clip prompts are linted with the live kit before any spend ─────────────

def test_lint_report_flags_missing_kit_and_bad_clip_prompt(tmp_path, monkeypatch):
    import json as _json
    from hfvg.prompt_lint import lint_report

    ep = tmp_path / "ep11"
    ep.mkdir()
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    meta = {"count": 1, "characters": ["D"], "exterior": True}
    good = "Calm drift; no fast camera moves. Smooth, bedtime-calm motion. Tuft stays. Ducks in frame: 1."
    plans = {
        "SH01": {"prompt": "STILL PROMPT WITHOUT CLAUSES", "clip_prompt": good, "shot_meta": meta,
                 "pre_approved_still": {"path": "x.png"}},
        "SH02": {"prompt": good, "clip_prompt": "Fast whip pan. Ducks in frame: 2.", "shot_meta": meta,
                 "pre_approved_still": None},
    }
    # 1. no prompt_kit.json at all -> one global problem
    assert list(lint_report("ep11", plans)) == ["*"]
    # 2. prompt_kit.json without a "lint" section (the Ep11 failure) -> global problem
    (ep / "prompt_kit.json").write_text(_json.dumps({"episode_id": "ep11"}))
    assert "no 'lint' section" in lint_report("ep11", plans)["*"][0]
    # same file named by PROMPT_LINT_KIT_PATH (the Ep11 stack) -> same clear message
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(ep / "prompt_kit.json"))
    assert "no 'lint' section" in lint_report("ep11", plans)["*"][0]
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH")
    # 3. kit present: pre-approved SH01 lints only its (clean) clip prompt; SH02 fails still? no - clean still,
    #    bad clip (count mismatch, no camera clause, no calm clause)
    (ep / "prompt_kit.json").write_text(_json.dumps({"lint": {
        "count_template": "Ducks in frame: {n}.", "realtime_pattern": "bedtime-calm motion",
        "locked_camera_clause": "no fast camera moves"}}))
    rep = lint_report("ep11", plans)
    assert "SH01" not in rep
    assert all(p.startswith("clip: ") for p in rep["SH02"])
    joined = " ".join(rep["SH02"])
    assert "count clause mismatch" in joined and "locked-camera" in joined and "real-time" in joined


# ---- Ep11 run gaps: QC resolution tolerance, keeping a landed QC-failed clip, subtitle fade/blur
def test_qc_resolution_tolerance_accepts_provider_rounding():
    from hfvg.activities.qc import _dims_close
    assert _dims_close(720, 1276, 720, 1280)          # Seedance 720p rounding (0.3%)
    assert not _dims_close(720, 1200, 720, 1280)      # a real mismatch still fails
    assert not _dims_close(1280, 720, 720, 1280)


def test_accept_clip_allows_landed_qc_failed_clip():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2()
    wf.assembly_sig = []
    wf.shot_outcomes = {"SH01": {"status": "failed", "clip_url": "https://x/c.mp4"},
                        "SH02": {"status": "failed", "clip_url": None},
                        "SH03": {"status": "failed", "clip_url": "https://x/d.mp4"}}
    wf.state.failed_shots = [
        {"shot_id": "SH01", "status": "failed", "reason": "QC failed: Resolution 720x1276"},
        {"shot_id": "SH02", "status": "failed", "reason": "QC failed: Resolution 720x1276"},
        {"shot_id": "SH03", "status": "failed", "reason": "Generation failed: timeout"}]
    assert wf._clip_review_problem("SH01", "ok by eye", "a.mp4") is None
    assert "no clip landed" in wf._clip_review_problem("SH02", "ok", "a.mp4")
    assert "not 'needs_review'" in wf._clip_review_problem("SH03", "ok", "a.mp4")
    assert "reason is required" in wf._clip_review_problem("SH01", " ", "a.mp4")


def test_subtitle_fade_and_blur_tags(tmp_path):
    from hfvg.audio_mix import write_ass
    p = tmp_path / "s.ass"
    style = "Style: EmberSubs,Nunito SemiBold,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,0,0,0,0,100,100,0,0,1,0,2,2,80,80,400,1"
    write_ass([{"start": 1.0, "end": 2.5, "lines": ["Hello there"]}], str(p),
              {"ass_style": style, "fade_ms": 150, "blur": 0.8})
    body = p.read_text()
    assert "{\\fad(150,150)\\blur0.8}" in body and "EmberSubs" in body


# ---- Ep11 delivery knobs: VO re-roll take, library SFX lowpass + transient slice
def test_vo_take_gets_its_own_key_and_take1_keeps_original():
    from hfvg.activities.audio import _key
    k1 = _key("tts", "pop", "v", "eleven_v3")
    assert k1 != _key("tts", "pop", "v", "eleven_v3", 2)


def test_plan_keeps_take_and_lowpass(tmp_path):
    from hfvg.audio_mix import normalize_plan, AudioInputError
    import pytest
    raw = {"vo": {"voice_id": "v", "lines": [{"id": "vo01", "at": 0.3, "text": "hi", "take": 2}]},
           "sfx": [{"id": "bed", "path": "x.wav", "at": 1, "dur": 2, "lowpass_hz": 2500},
                   {"id": "pop", "path": "x.wav", "at": 3, "dur": 0.12, "src_start_s": 4.2}]}
    p = normalize_plan(raw, tmp_path)
    assert p["vo"]["lines"][0]["take"] == 2
    assert p["sfx"][0]["lowpass_hz"] == 2500 and p["sfx"][1]["lowpass_hz"] is None
    assert p["sfx"][1]["src_start_s"] == 4.2
    raw["sfx"][0]["lowpass_hz"] = 5
    with pytest.raises(AudioInputError):
        normalize_plan(raw, tmp_path)


def test_decode_lowpass_softens_highs(tmp_path):
    import asyncio, subprocess
    import numpy as np
    from hfvg.audio_mix import decode
    src = tmp_path / "n.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=1:c=white:a=0.5",
                    "-ar", "48000", "-ac", "2", str(src)], check=True)
    raw = asyncio.run(decode(str(src), 48000))
    lp = asyncio.run(decode(str(src), 48000, afilter="lowpass=f=1000,lowpass=f=1000"))
    def hf(x):
        spec = np.abs(np.fft.rfft(x[:, 0]))
        return spec[len(spec) // 2:].mean()
    assert hf(lp) < hf(raw) * 0.05


def test_assembly_rebuild_request_makes_edit_stale(monkeypatch):
    import logging
    import hfvg.workflows.episode_v2 as ev2
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    monkeypatch.setattr(ev2.workflow, "logger", logging.getLogger("t"))
    wf = EpisodeWorkflowV2()
    wf.assembly_shots = [{"shot_id": "SH01"}]
    wf.shot_outcomes = {"SH01": {"status": "completed", "clip_path": "a.mp4"}}
    wf.assembly_sig = wf._assembly_signature()
    assert not wf._assembly_stale()
    wf.request_assembly_rebuild("  ")
    assert not wf._assembly_stale() and wf.final_cut_refusals
    wf.request_assembly_rebuild("tail 1.5 -> 2.0s")
    assert wf._assembly_stale()
    wf.assembly_sig = wf._assembly_signature()   # re-assembled
    assert not wf._assembly_stale()


@pytest.mark.asyncio
async def test_manifest_includes_recipe_and_fps(tmp_path):
    """Task 5: manifest.format.fps must follow recipe fps, and include recipe and tail_hold_s."""
    clips = {}
    for sid, secs in (("A", 2.0), ("B", 2.0)):
        clips[sid] = str(make_clip(tmp_path / f"{sid}.mp4", secs, size="360x640", rate=30))
    out = tmp_path / "edit"
    out.mkdir()
    
    # Recipe with non-standard fps
    (out / EDIT_RECIPE_NAME).write_text(json.dumps({
        "fps": 25,
        "speed": 1.0,
        "xfade_s": 0.0,
        "tail_hold_s": 2.5,
        "scale_flags": "lanczos"
    }))
    
    shots = [{"shot_id": "A", "duration": 2.0}, {"shot_id": "B", "duration": 2.0}]
    res = await ActivityEnvironment().run(assemble_mute_edit, "ep99", shots, clips, str(out))
    m = res["manifest"]
    
    # Task 5: format.fps must match recipe fps (not default EDIT_FPS)
    assert m["format"]["fps"] == 25, "manifest.format.fps must follow recipe fps"
    
    # Task 5: recipe must be in manifest
    assert "recipe" in m, "manifest must include recipe"
    assert m["recipe"]["fps"] == 25
    assert m["recipe"]["tail_hold_s"] == 2.5
    
    # Task 5: tail_hold_s must be in manifest
    assert "tail_hold_s" in m, "manifest must include tail_hold_s"
    assert m["tail_hold_s"] == 2.5


@pytest.mark.asyncio
async def test_dry_run_honors_recipe(tmp_path, monkeypatch):
    """Task 5: dry-run return must honor the recipe (speed and tail_hold)."""
    monkeypatch.setenv("DRY_RUN", "true")
    
    # Use placeholder clips that don't exist (dry-run mode)
    clips = {"A": str(tmp_path / "A.mp4"), "B": str(tmp_path / "B.mp4")}
    out = tmp_path / "edit"
    out.mkdir()
    
    # Recipe with speed 2.0 and tail hold 1.5s
    (out / EDIT_RECIPE_NAME).write_text(json.dumps({
        "fps": 24,
        "speed": 2.0,
        "xfade_s": 0.0,
        "tail_hold_s": 1.5,
        "scale_flags": "lanczos"
    }))
    
    shots = [
        {"shot_id": "A", "duration": 4.0, "trim_in": 0.0, "trim_out": 4.0},
        {"shot_id": "B", "duration": 2.0, "trim_in": 0.0, "trim_out": 2.0}
    ]
    res = await ActivityEnvironment().run(assemble_mute_edit, "ep99", shots, clips, str(out))
    
    assert res["dry_run"] is True
    
    # Without recipe: (4.0 + 2.0) = 6.0s
    # With recipe: (4.0 + 2.0) / 2.0 + 1.5 = 3.0 + 1.5 = 4.5s
    expected_duration = (4.0 + 2.0) / 2.0 + 1.5
    assert res["duration"] == pytest.approx(expected_duration, abs=0.01), \
        "dry-run duration must honor recipe speed and tail_hold"
    
    # Task 5: dry-run manifest must include recipe
    assert "recipe" in res["manifest"]
    assert res["manifest"]["tail_hold_s"] == 1.5
