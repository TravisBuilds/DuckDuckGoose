"""GAP-55 (r9): the delivery's logo overlay dropped 8 frames (vfr) in Ep05 v7 while the mute
assembly had 2880. Every picture encode is now CFR with the mute's exact frame count, the build
aborts on a mismatch, and delivery QC fails any deliverable whose frame count differs.

Real ffmpeg on small synthetic pictures; no provider calls."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hfvg import audio_mix
from hfvg.audio_mix import count_frames, delivery_qc, mix_and_deliver

from tests.test_audio_delivery import _ff, episode, make_picture

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

FPS, SECONDS = 24, 6.0
FRAMES = int(FPS * SECONDS)  # 144


def _logo(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ff("-f", "lavfi", "-i", "color=c=white@0.8:s=160x48,format=rgba", "-frames:v", "1", str(path))
    return path


def logo_episode(tmp_path: Path, fps: int = FPS) -> tuple[Path, dict]:
    mute, _ = episode(tmp_path, seconds=SECONDS)
    make_picture(mute, SECONDS, size="432x768", rate=fps)  # 24 fps like production
    plan_file = mute.parent.parent / "audio" / "audio_plan.json"
    raw = json.loads(plan_file.read_text())
    raw["logo"] = {"path": str(_logo(tmp_path / "logo.png")), "visible_s": 2.5, "fade_in_s": 1.0,
                   "y_frac": 0.16}
    plan_file.write_text(json.dumps(raw))
    return mute, audio_mix.load_plan_file(plan_file)


def drop_frames(src: str, dst: str, first: int = 60, last: int = 67) -> None:
    """Re-encode src without frames first..last but with their original timestamps (a vfr hole,
    the r9 v7 failure: duration unchanged, 8 frames gone)."""
    tmp = dst + ".tmp.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", src, "-map", "0:v:0", "-map", "0:a?",
                    "-vf", f"select='not(between(n\\,{first}\\,{last}))'", "-fps_mode", "passthrough",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy",
                    tmp], check=True)
    Path(tmp).replace(dst)


def _check(qc: dict, name: str) -> dict:
    return next(c for c in qc["checks"] if c["name"] == name)


@pytest.mark.timeout(240)
async def test_logo_delivery_keeps_mute_frame_count(tmp_path):
    mute, plan = logo_episode(tmp_path)
    assert await count_frames(mute) == FRAMES
    m = await mix_and_deliver("ep01", str(mute), plan)
    assert m["logo"] and m["picture_frames"] == FRAMES and m["picture_fps"] == f"{FPS}/1"
    assert m["version"] == audio_mix.DELIVERY_VERSION >= 5
    names = ["nosubs", "phone"] + (["master_subs"] if m["subtitles_burned"] else [])
    assert "master_subs" in names
    for name in names:
        assert await count_frames(m["outputs"][name]["path"]) == FRAMES, name
    qc = await delivery_qc(m, plan)
    assert qc["verdict"] == "PASS", qc["failed"]
    for name in names:
        c = _check(qc, f"{name}_frame_count")
        assert c["ok"] and c["value"] == FRAMES and c["limit"] == FRAMES


@pytest.mark.timeout(240)
async def test_dropped_frames_fail_delivery_qc_even_with_correct_duration(tmp_path):
    mute, plan = logo_episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    nosubs = m["outputs"]["nosubs"]["path"]
    drop_frames(nosubs, nosubs)
    assert await count_frames(nosubs) == FRAMES - 8
    qc = await delivery_qc(m, plan)
    assert qc["verdict"] == "FAIL" and qc["blocking"]
    assert qc["failed"] == ["nosubs_frame_count"], qc["failed"]
    c = _check(qc, "nosubs_frame_count")
    assert c["value"] == FRAMES - 8 and c["limit"] == FRAMES
    # the duration check alone would not have caught it (the v7 failure mode)
    assert _check(qc, "nosubs_duration")["ok"]
    assert _check(qc, "phone_frame_count")["ok"]


@pytest.mark.timeout(240)
async def test_frame_count_checked_without_manifest_frames(tmp_path):
    """Older manifests (no picture_frames) fall back to duration x stream rate."""
    mute, plan = logo_episode(tmp_path)
    m = await mix_and_deliver("ep01", str(mute), plan)
    old = {k: v for k, v in m.items() if k != "picture_frames"}
    qc = await delivery_qc(old, plan)
    assert qc["verdict"] == "PASS", qc["failed"]
    phone = m["outputs"]["phone"]["path"]
    drop_frames(phone, phone, 10, 12)
    qc = await delivery_qc(old, plan)
    assert "phone_frame_count" in qc["failed"] and _check(qc, "phone_frame_count")["limit"] == FRAMES


@pytest.mark.timeout(240)
async def test_overlay_frame_mismatch_aborts_the_build(tmp_path, monkeypatch):
    mute, plan = logo_episode(tmp_path)
    real = audio_mix.overlay_logo

    async def lossy(src, logo, total_s, out, master, frames=None):
        await real(src, logo, total_s, out, master, frames=frames)
        drop_frames(out, out)

    monkeypatch.setattr(audio_mix, "overlay_logo", lossy)
    with pytest.raises(RuntimeError, match="GAP-55"):
        await mix_and_deliver("ep01", str(mute), plan)
    assert not Path(audio_mix.delivery_paths("ep01")["manifest"]).exists()


async def test_overlay_logo_pins_cfr_and_frame_count(monkeypatch):
    seen = []

    async def fake_run(cmd, what, cwd=None, stdin=None):
        seen.append(cmd)
        return b"", ""

    async def fake_probe(path):
        return {"streams": [{"codec_type": "video", "width": 1080, "height": 1920, "r_frame_rate": "24/1"}]}

    monkeypatch.setattr(audio_mix, "run_cmd", fake_run)
    monkeypatch.setattr(audio_mix, "probe", fake_probe)
    logo = {"path": "/x/logo.png", "width_frac": 0.62, "fade_in_s": 2.5, "visible_s": 6.5, "y_frac": 0.16}
    await audio_mix.overlay_logo("/x/mute.mp4", logo, 120.0, "/x/out.mp4",
                                 {"preset": "fast", "crf": 18}, frames=2880)
    cmd = seen[-1]
    i = cmd.index("-fps_mode")
    assert cmd[i + 1] == "cfr"
    assert cmd[cmd.index("-r") + 1] == "24/1"
    assert cmd[cmd.index("-frames:v") + 1] == "2880"
    assert cmd.index("-fps_mode") < cmd.index("/x/out.mp4")


def test_cfr_args_and_version_bump_change_the_input_key(monkeypatch):
    assert audio_mix.cfr_args("24/1", 2880) == ["-fps_mode", "cfr", "-r", "24/1", "-frames:v", "2880"]
    plan = {"vo": {"lines": []}}
    k5 = audio_mix.input_key("a" * 64, 120.0, plan, {})
    monkeypatch.setattr(audio_mix, "DELIVERY_VERSION", 4)
    assert audio_mix.input_key("a" * 64, 120.0, plan, {}) != k5  # a v4 delivery is never reused


@pytest.mark.timeout(240)
async def test_logo_overlay_keeps_last_frame_off_the_25fps_grid(tmp_path):
    """The looped logo used to run on ffmpeg's 25 fps image grid for exactly total_s, so
    overlay shortest=1 ended one frame early on 30 fps pictures (179 of 180)."""
    mute, plan = logo_episode(tmp_path, fps=30)
    m = await mix_and_deliver("ep01", str(mute), plan)
    assert m["picture_frames"] == 180
    for name in ("nosubs", "phone", "master_subs"):
        assert await count_frames(m["outputs"][name]["path"]) == 180, name
