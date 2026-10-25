"""Round 7 B7: in DRY_RUN the audio round is measured, not short-circuited.

VO stand-ins are as long as the line's text estimate, the music bed as long as the planned picture,
and a black stand-in picture of the planned length lets placement, ducking, subtitles and the
loudness / true-peak / duration checks run for real (results stay flagged dry_run)."""

import json
import shutil
import subprocess

import pytest

from hfvg.activities import audio as audio_act
from tests.test_audio_delivery import FAST_ENCODE

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _probe(path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def test_dry_vo_seconds_follows_the_text():
    assert audio_act.dry_vo_seconds("x" * 45) == 3.0
    assert audio_act.dry_vo_seconds("") == 0.8
    assert audio_act.dry_vo_seconds("x" * 1000) == 12.0


@pytest.fixture
def dry_episode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    root = tmp_path / "data" / "episodes" / "ep77"
    (root / "audio").mkdir(parents=True)
    plan = {"min_vo_gap_s": 0.4,
            "vo": {"voice_id": "v", "lines": [
                {"id": "L01", "at": 1.0, "text": "A short first line for the guests."},
                {"id": "L02", "at": 6.0, "text": "And the second line, a little longer than the first."}]},
            "music": {"prompt": "soft bed"}, **FAST_ENCODE}
    (root / "audio" / "audio_plan.json").write_text(json.dumps(plan))
    return root


async def _resolve(ep: str, plan: dict, picture_s: float) -> dict:
    plan = dict(plan)
    lines = []
    for ln in plan["vo"]["lines"]:
        res = await audio_act.generate_voiceover(ep, ln["text"], "v", "eleven_v3", ln["id"], None)
        assert res["dry_run"] is True
        lines.append({**ln, "path": res["path"]})
    plan["vo"] = {**plan["vo"], "lines": lines}
    music = await audio_act.generate_music(ep, "soft bed", picture_s, "music_v1", "music", None)
    plan["music"] = {**plan["music"], "path": music["path"]}
    plan["dry_inputs"] = ["vo:L01", "vo:L02", "music"]
    return plan


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(240)
async def test_dry_delivery_is_measured_on_a_planned_length_stand_in(dry_episode):
    plan = await audio_act.load_audio_plan("ep77", None)
    plan = await _resolve("ep77", plan, 12.0)
    for ln in plan["vo"]["lines"]:
        assert abs(_probe(ln["path"]) - audio_act.dry_vo_seconds(ln["text"])) < 0.05, ln["id"]
    assert abs(_probe(plan["music"]["path"]) - 12.0) < 0.05, "music bed follows the picture length"

    manifest = await audio_act.mix_and_deliver("ep77", str(dry_episode / "missing_mute.mp4"), plan, 12.0)
    assert manifest["dry_run"] is True and "picture" in manifest["dry_reason"]
    assert manifest.get("outputs"), "the mix must really run on the stand-in picture"
    placed = {ln["id"]: ln for ln in manifest["vo_lines"]}
    assert placed["L01"]["at"] == 1.0 and placed["L02"]["at"] == 6.0
    assert abs(placed["L02"]["dur"] - audio_act.dry_vo_seconds(plan["vo"]["lines"][1]["text"])) < 0.05

    qc = await audio_act.check_delivery_qc("ep77", manifest, plan, 12.0)
    names = {c["name"]: c for c in qc["checks"]}
    for needed in ("nosubs_duration", "nosubs_loudness", "nosubs_true_peak", "vo_min_gap"):
        assert needed in names, (needed, sorted(names))
    assert abs(names["nosubs_duration"]["value"] - 12.0) < 0.2, "measured against the planned picture"
    assert names["vo_min_gap"]["ok"] is True


@needs_ffmpeg
@pytest.mark.asyncio
async def test_dry_without_a_planned_length_still_short_circuits(dry_episode):
    plan = await audio_act.load_audio_plan("ep77", None)
    plan = await _resolve("ep77", plan, 5.0)
    out = await audio_act.mix_and_deliver("ep77", str(dry_episode / "missing.mp4"), plan, None)
    assert out["dry_run"] is True and not out.get("outputs")


@pytest.mark.asyncio
async def test_live_missing_picture_still_refuses(dry_episode, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    from temporalio.exceptions import ApplicationError
    with pytest.raises(ApplicationError, match="Mute picture not found"):
        await audio_act.mix_and_deliver("ep77", str(dry_episode / "missing.mp4"), {"vo": {"lines": []}}, 12.0)
