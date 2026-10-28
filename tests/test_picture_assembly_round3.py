"""Round 3: mute picture edit, stillgate QC (assembly + per clip), animal checks, clip prompts.

Activities run inside a real activity context (temporalio.testing.ActivityEnvironment): they
heartbeat, and calling them as plain coroutines raises "Not in activity context" (the CI failure of
the first Round 3 push). Workflow tests run the real EpisodeWorkflowV2 / ShotWorkflow on a local
Temporal server with real ffmpeg clips; nothing calls a provider.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import aiosqlite
import httpx
import pytest
import respx
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities.media import assemble_mute_edit, edit_dir
from hfvg.activities.qc import check_assembly_qc, clip_stillgate, review_clip
from hfvg.qc import animal_count
from hfvg.qc.animal_count import (
    DuckPresenceCheck,
    FakeAnimalCounter,
    check_shot_animals,
    create_duck_check,
    set_animal_counter,
)
from hfvg.qc.motion import MotionCheckResult, _run_stillgate_analysis, check_motion
from hfvg.shot_data import ShotDataError, merge_into_shots, parse_shots
from tests.lint_helpers import lint_meta, lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _until, _wait, dry_env  # noqa: F401

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed"
)
TASK_QUEUE = "picture-assembly-tasks"

# lavfi sources measured with the stillgate thresholds: testsrc2 moves everywhere (PASS), a flat
# colour never changes (FAIL), weak temporal noise is low motion (WATCH)
MOVING = "testsrc2"
FROZEN = "color=c=gray"


def make_clip(path: Path, seconds: float, source: str = MOVING, size: str = "360x640", rate: int = 30) -> Path:
    src = f"{source}:size={size}:rate={rate}" if source.startswith("color") else f"{source}=size={size}:rate={rate}"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, "-t", str(seconds),
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return path


def make_color_clip(path: Path, colors: list[tuple[str, float]], size: str = "360x640", rate: int = 30) -> Path:
    """Clip made of solid colour segments, e.g. [("red", 1), ("green", 2)]."""
    inputs, labels = [], []
    for i, (c, secs) in enumerate(colors):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:size={size}:rate={rate}:duration={secs}"]
        labels.append(f"[{i}:v]")
    graph = "".join(labels) + f"concat=n={len(colors)}:v=1:a=0[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", graph, "-map", "[v]",
           "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", str(path)]
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return path


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-count_frames", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return json.loads(out)


def rgb_at(path: str | Path, t: float) -> tuple[int, int, int]:
    """Mean RGB of the centre of the frame at time t."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=100:100:(iw-100)/2:(ih-100)/2,scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return raw[0], raw[1], raw[2]


def dominant(rgb: tuple[int, int, int]) -> str:
    return ["red", "green", "blue"][max(range(3), key=lambda i: rgb[i])]


class HB:
    """ActivityEnvironment with recorded heartbeats."""

    def __init__(self):
        self.env = ActivityEnvironment()
        self.beats: list = []
        self.env.on_heartbeat = lambda *details: self.beats.append(details[0] if details else None)

    async def run(self, fn, *args):
        return await self.env.run(fn, *args)

    def stages(self) -> list[str]:
        return [b.get("stage") for b in self.beats if isinstance(b, dict)]


@pytest.fixture
def no_counter():
    set_animal_counter(None)
    yield
    set_animal_counter(None)


# ─────────────────────────────────────────────────────────────────────────────
# Mute picture edit (assemble_mute_edit)
# ─────────────────────────────────────────────────────────────────────────────

@needs_ffmpeg
class TestAssembleMuteEdit:
    @pytest.mark.asyncio
    async def test_assemble_mute_edit_basic(self, tmp_path, monkeypatch):
        """Three clips of different size / fps / aspect -> one 1080x1920 30 fps yuv420p mute edit,
        clips concatenated in shot order with exact frame counts, deterministic path."""
        monkeypatch.setenv("DRY_RUN", "false")
        a = make_color_clip(tmp_path / "a.mp4", [("red", 2.0)], size="640x360", rate=24)
        b = make_color_clip(tmp_path / "b.mp4", [("green", 1.0)], size="360x640", rate=25)
        c = make_color_clip(tmp_path / "c.mp4", [("blue", 1.5)], size="720x1280", rate=30)
        shots = [{"shot_id": "A01", "duration": 2.0}, {"shot_id": "A02", "duration": 1.0},
                 {"shot_id": "A03", "duration": 1.5}]
        hb = HB()
        result = await hb.run(assemble_mute_edit, "ep31", shots,
                              {"A01": str(a), "A02": str(b), "A03": str(c)}, None)

        expected_path = edit_dir("ep31") / "mute_assembly.mp4"
        assert result["assembly_path"] == str(expected_path)
        assert Path(result["assembly_path"]).is_file()
        assert result["dry_run"] is False and result["reused"] is False
        info = probe(result["assembly_path"])
        video = [s for s in info["streams"] if s["codec_type"] == "video"]
        assert len(video) == 1 and not [s for s in info["streams"] if s["codec_type"] == "audio"], info["streams"]
        v = video[0]
        assert (v["width"], v["height"], v["pix_fmt"], v["r_frame_rate"]) == (1080, 1920, "yuv420p", "30/1"), v
        assert int(v["nb_read_frames"]) == 60 + 30 + 45
        assert [cl["id"] for cl in result["manifest"]["clips"]] == ["A01", "A02", "A03"]
        assert [cl["frames"] for cl in result["manifest"]["clips"]] == [60, 30, 45]
        assert result["duration"] == pytest.approx(4.5, abs=1e-6)
        # shot order is the picture order
        assert [dominant(rgb_at(result["assembly_path"], t)) for t in (1.0, 2.5, 3.7)] == ["red", "green", "blue"]
        stages = hb.stages()
        assert stages.count("normalizing_clip") == 3 and "concatenating" in stages, stages
        on_disk = json.loads((edit_dir("ep31") / "assembly_manifest.json").read_text())
        assert on_disk["input_key"] == result["input_key"]

    @pytest.mark.asyncio
    async def test_assemble_mute_edit_with_trimming(self, tmp_path, monkeypatch):
        """trim_in / trim_out from the shot plan select exactly that part of the clip."""
        monkeypatch.setenv("DRY_RUN", "false")
        clip = make_color_clip(tmp_path / "rgb.mp4", [("red", 1.0), ("green", 2.0), ("blue", 1.0)])
        result = await HB().run(assemble_mute_edit, "ep32",
                                [{"shot_id": "A01", "duration": 4.0, "trim_in": 1.0, "trim_out": 3.0}],
                                {"A01": str(clip)}, str(tmp_path / "edit"))
        assert result["manifest"]["clips"][0]["frames"] == 60
        assert result["manifest"]["clips"][0]["trim_in"] == 1.0
        assert result["manifest"]["clips"][0]["trim_out"] == pytest.approx(3.0)
        assert result["duration"] == pytest.approx(2.0)
        assert int(probe(result["assembly_path"])["streams"][0]["nb_read_frames"]) == 60
        colours = {dominant(rgb_at(result["assembly_path"], t)) for t in (0.05, 1.0, 1.9)}
        assert colours == {"green"}, colours

    @pytest.mark.asyncio
    async def test_assemble_mute_edit_idempotent(self, tmp_path, monkeypatch):
        """Same inputs -> the existing edit is reused untouched; a changed clip or trim rebuilds it."""
        monkeypatch.setenv("DRY_RUN", "false")
        clip = make_color_clip(tmp_path / "a.mp4", [("red", 2.0)])
        shots = [{"shot_id": "A01", "duration": 2.0}]
        out = str(tmp_path / "edit")
        first = await HB().run(assemble_mute_edit, "ep33", shots, {"A01": str(clip)}, out)
        mtime = Path(first["assembly_path"]).stat().st_mtime_ns
        second = await HB().run(assemble_mute_edit, "ep33", shots, {"A01": str(clip)}, out)
        assert second["reused"] is True and second["input_key"] == first["input_key"]
        assert Path(second["assembly_path"]).stat().st_mtime_ns == mtime

        make_color_clip(clip, [("blue", 2.0)])  # same path, new content (e.g. a clip retry)
        third = await HB().run(assemble_mute_edit, "ep33", shots, {"A01": str(clip)}, out)
        assert third["reused"] is False and third["input_key"] != first["input_key"]
        assert dominant(rgb_at(third["assembly_path"], 1.0)) == "blue"

        trimmed = [{"shot_id": "A01", "duration": 2.0, "trim_in": 0.5, "trim_out": 1.5}]
        fourth = await HB().run(assemble_mute_edit, "ep33", trimmed, {"A01": str(clip)}, out)
        assert fourth["reused"] is False and fourth["duration"] == pytest.approx(1.0)

    @pytest.mark.asyncio
    async def test_missing_clip_live_refuses_dry_returns_placeholder(self, tmp_path, monkeypatch):
        shots = [{"shot_id": "A01", "duration": 2.0}]
        monkeypatch.setenv("DRY_RUN", "false")
        with pytest.raises(ApplicationError) as exc:
            await HB().run(assemble_mute_edit, "ep34", shots, {"A01": str(tmp_path / "nope.mp4")}, None)
        assert exc.value.non_retryable and "A01" in str(exc.value)
        with pytest.raises(ApplicationError):
            await HB().run(assemble_mute_edit, "ep34", shots, {}, None)

        monkeypatch.setenv("DRY_RUN", "true")
        dry = await HB().run(assemble_mute_edit, "ep34", shots, {"A01": str(tmp_path / "nope.mp4")}, None)
        assert dry["dry_run"] is True and dry["manifest"]["dry_run"] is True
        assert not Path(dry["assembly_path"]).exists()

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_dry_stand_in_clip_is_a_dry_run_live_short_clip_refuses(self, tmp_path, monkeypatch):
        """Dry-run provider stand-ins (1s) cannot honour a 6s plan: reported as a dry run, nothing
        rendered. The same short clip live is a hard TrimOutOfRange, never silently stretched."""
        clip = make_clip(tmp_path / "standin.mp4", 1.0)
        shots = [{"shot_id": "A01", "duration": 6.0}]
        monkeypatch.setenv("DRY_RUN", "true")
        dry = await HB().run(assemble_mute_edit, "ep35", shots, {"A01": str(clip)}, None)
        assert dry["dry_run"] is True and not Path(dry["assembly_path"]).exists()
        monkeypatch.setenv("DRY_RUN", "false")
        with pytest.raises(ApplicationError) as exc:
            await HB().run(assemble_mute_edit, "ep35", shots, {"A01": str(clip)}, None)
        assert exc.value.type == "TrimOutOfRange"

    @pytest.mark.asyncio
    async def test_trim_beyond_clip_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        clip = make_clip(tmp_path / "a.mp4", 2.0)
        with pytest.raises(ApplicationError) as exc:
            await HB().run(assemble_mute_edit, "ep35",
                           [{"shot_id": "A01", "duration": 5.0, "trim_in": 0.0, "trim_out": 4.0}],
                           {"A01": str(clip)}, str(tmp_path / "edit"))
        assert exc.value.type == "TrimOutOfRange"
        with pytest.raises(ApplicationError):
            await HB().run(assemble_mute_edit, "ep35",
                           [{"shot_id": "A01", "duration": 2.0, "trim_in": 1.5, "trim_out": 1.0}],
                           {"A01": str(clip)}, str(tmp_path / "edit"))

    @pytest.mark.asyncio
    async def test_activity_requires_activity_context(self, tmp_path, monkeypatch):
        """The activity heartbeats: it must run in an activity context (worker or ActivityEnvironment)."""
        monkeypatch.setenv("DRY_RUN", "true")
        with pytest.raises(RuntimeError, match="Not in activity context"):
            await assemble_mute_edit("ep36", [{"shot_id": "A01", "duration": 1.0}], {"A01": "x.mp4"}, None)


# ─────────────────────────────────────────────────────────────────────────────
# Stillgate (assembly + per clip)
# ─────────────────────────────────────────────────────────────────────────────

@needs_ffmpeg
class TestStillgate:
    def test_motion_check_stillgate_direct(self, tmp_path):
        moving = make_clip(tmp_path / "m.mp4", 3.0, MOVING)
        frozen = make_clip(tmp_path / "f.mp4", 3.0, FROZEN)
        manifest = {"clips": [{"id": "A01", "seg_s": 3.0}], "dissolves": {}}
        assert check_motion(str(moving), manifest)[0].verdict == "PASS"
        r = check_motion(str(frozen), manifest)[0]
        assert r.verdict == "FAIL" and r.peak_fd < 3.0 and r.mean_fd < 0.15, r

    @pytest.mark.asyncio
    async def test_stillgate_windows_use_analysis_fps_on_30fps_edit(self, tmp_path, monkeypatch):
        """The edit is 30 fps; stillgate indexes windows at 24 fps. Decoding natively put B's window
        on A's moving frames (B 'passed'); decoding at 24 fps finds the frozen shot B."""
        monkeypatch.setenv("DRY_RUN", "false")
        a = make_clip(tmp_path / "a.mp4", 3.0, MOVING)
        b = make_clip(tmp_path / "b.mp4", 1.0, FROZEN)
        c = make_clip(tmp_path / "c.mp4", 1.0, MOVING)
        res = await HB().run(assemble_mute_edit, "ep37",
                             [{"shot_id": "A", "duration": 3.0}, {"shot_id": "B", "duration": 1.0},
                              {"shot_id": "C", "duration": 1.0}],
                             {"A": str(a), "B": str(b), "C": str(c)}, str(tmp_path / "edit"))
        verdicts = {r.shot_id: r.verdict for r in check_motion(res["assembly_path"], res["manifest"])}
        assert verdicts == {"A": "PASS", "B": "FAIL", "C": "PASS"}, verdicts

    def test_stillgate_analysis_empty_manifest(self, tmp_path):
        video = make_clip(tmp_path / "t.mp4", 1.0)
        assert _run_stillgate_analysis(str(video), {"clips": [], "dissolves": {}}) == []

    def test_clip_stillgate_single_clip(self, tmp_path):
        frozen = make_clip(tmp_path / "f.mp4", 2.0, FROZEN, size="1080x1920", rate=24)
        assert clip_stillgate(str(frozen), "A01", 2.0)["verdict"] == "FAIL"
        moving = make_clip(tmp_path / "m.mp4", 2.0, MOVING, size="1080x1920", rate=24)
        assert clip_stillgate(str(moving), "A01", 2.0)["verdict"] == "PASS"

    @pytest.mark.asyncio
    async def test_live_review_clip_runs_stillgate_per_clip(self, tmp_path, monkeypatch):
        """A frozen 9:16 clip passes ffprobe/blackdetect/freezedetect motion QC but is still-only:
        review_clip fails it per clip (reroll path, never a pass); a moving clip passes."""
        from hfvg.qc.clip_expect import expected_clip_dims
        from hfvg.studio_db import create_episode, init_studio_db

        db_path = str(tmp_path / "qc.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep38")
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = 'ep38'")
            await db.commit()
        monkeypatch.setenv("DATABASE_PATH", db_path)
        monkeypatch.setenv("DRY_RUN", "false")
        dims = expected_clip_dims({"model": "kling", "aspect_ratio": "9:16"})
        rubric = {"duration": 2.0, **dims}
        frozen = make_clip(tmp_path / "f.mp4", 2.0, FROZEN, size=dims["resolution"], rate=24)
        moving = make_clip(tmp_path / "m.mp4", 2.0, MOVING, size=dims["resolution"], rate=24)
        with respx.mock(assert_all_called=False) as router:
            router.get("https://cdn.example.com/f.mp4").mock(return_value=httpx.Response(200, content=frozen.read_bytes()))
            router.get("https://cdn.example.com/m.mp4").mock(return_value=httpx.Response(200, content=moving.read_bytes()))
            bad = await HB().run(review_clip, "https://cdn.example.com/f.mp4", rubric, "ep38", "A01")
            good = await HB().run(review_clip, "https://cdn.example.com/m.mp4", rubric, "ep38", "A02")
        assert bad["passed"] is False and bad["motion"]["verdict"] == "FAIL", bad
        assert "still-only" in bad["issues"][0]
        assert good["passed"] is True and good["motion"]["verdict"] == "PASS", good

    def test_motion_check_result_verdicts(self):
        assert MotionCheckResult("A", 0, 1, 1.5, 10.0, "PASS").is_pass()
        assert MotionCheckResult("A", 0, 1, 0.2, 5.0, "WATCH").is_watch()
        assert MotionCheckResult("A", 0, 1, 0.1, 2.0, "FAIL").is_fail()


# ─────────────────────────────────────────────────────────────────────────────
# Animal checks + assembly QC verdict routing
# ─────────────────────────────────────────────────────────────────────────────

class TestAnimalChecks:
    @pytest.mark.asyncio
    async def test_no_checker_escalates_never_passes(self, tmp_path, no_counter):
        media = tmp_path / "x.mp4"
        media.write_bytes(b"x")
        res = await check_shot_animals(None, media, {"count": 2, "duck": True})
        assert res["status"] == "NEEDS_REVIEW", res
        assert all(c["escalate"] for c in res["checks"]) and len(res["checks"]) == 2
        duck = await create_duck_check().check_clip(media, 1)
        assert duck["passed"] is False and duck["escalate"] is True and duck["status"] == "NEEDS_REVIEW"
        assert (await DuckPresenceCheck(None).check_still(media, 1))["escalate"] is True

    @pytest.mark.asyncio
    async def test_deterministic_counter(self, tmp_path):
        media = tmp_path / "x.mp4"
        media.write_bytes(b"x")
        ok = await check_shot_animals(FakeAnimalCounter({"animal": 2, "duck": 1}), media, {"count": 2, "duck": True})
        assert ok["status"] == "PASS", ok
        wrong_count = await check_shot_animals(FakeAnimalCounter({"animal": 3, "duck": 1}), media, {"count": 2})
        assert wrong_count["status"] == "FAIL" and "expected 2, got 3" in wrong_count["reason"]
        no_duck = await check_shot_animals(FakeAnimalCounter({"animal": 2, "duck": 0}), media, {"count": 2, "duck": True})
        assert no_duck["status"] == "FAIL"
        unsure = await check_shot_animals(FakeAnimalCounter({"animal": 2}, confidence=0.3), media, {"count": 2})
        assert unsure["status"] == "NEEDS_REVIEW"
        per_file = FakeAnimalCounter({("x.mp4", "animal"): 4, "animal": 1})
        assert (await check_shot_animals(per_file, media, {"count": 4}))["status"] == "PASS"
        assert (await DuckPresenceCheck(FakeAnimalCounter({"duck": 1})).check_still(media, 1))["passed"] is True


@needs_ffmpeg
class TestAssemblyQC:
    async def _edit(self, tmp_path, sources: dict[str, str]):
        clips = {sid: str(make_clip(tmp_path / f"{sid}.mp4", 2.0, src)) for sid, src in sources.items()}
        shots = [{"shot_id": sid, "duration": 2.0} for sid in sources]
        return await HB().run(assemble_mute_edit, "ep40", shots, clips, str(tmp_path / "edit"))

    @pytest.mark.asyncio
    async def test_motion_pass_without_checker_needs_review(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        edit = await self._edit(tmp_path, {"A01": MOVING, "A02": MOVING})
        qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep40",
                            {"A01": lint_meta(1), "A02": lint_meta(2, duck=True)})
        assert [m["verdict"] for m in qc["motion_results"]] == ["PASS", "PASS"]
        assert qc["verdict"] == "NEEDS_REVIEW" and qc["blocking"] is True and qc["escalate"] is True, qc
        assert qc["review_shots"] == ["A01", "A02"]

    @pytest.mark.asyncio
    async def test_pass_with_matching_counter(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1, "duck": 1}))
        edit = await self._edit(tmp_path, {"A01": MOVING, "A02": MOVING})
        qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep40",
                            {"A01": lint_meta(1), "A02": lint_meta(1, duck=True)})
        assert qc["verdict"] == "PASS" and qc["blocking"] is False, qc
        assert len(qc["animal_results"]) == 2

    @pytest.mark.asyncio
    async def test_still_only_shot_fails(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        edit = await self._edit(tmp_path, {"A01": MOVING, "A02": FROZEN})
        qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep40",
                            {"A01": lint_meta(1), "A02": lint_meta(1)})
        assert qc["verdict"] == "FAIL" and qc["blocking"] is True, qc
        assert qc["failed_shots"] == ["A02"] and "A02 still-only" in qc["reason"]

    @pytest.mark.asyncio
    async def test_count_mismatch_fails(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 2}))
        edit = await self._edit(tmp_path, {"A01": MOVING})
        qc = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep40", {"A01": lint_meta(1)})
        assert qc["verdict"] == "FAIL" and qc["failed_shots"] == ["A01"], qc

    @pytest.mark.asyncio
    async def test_missing_edit_and_qc_error_escalate(self, tmp_path, monkeypatch, no_counter):
        monkeypatch.setenv("DRY_RUN", "false")
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        manifest = {"clips": [{"id": "A01", "seg_s": 2.0}], "dissolves": {}}
        missing = await HB().run(check_assembly_qc, str(tmp_path / "none.mp4"), manifest, "ep40", {})
        assert missing["verdict"] == "NEEDS_REVIEW" and missing["blocking"] is True
        junk = tmp_path / "junk.mp4"
        junk.write_bytes(b"not a video")
        broken = await HB().run(check_assembly_qc, str(junk), manifest, "ep40", {"A01": lint_meta(1)})
        assert broken["verdict"] == "NEEDS_REVIEW" and "motion QC error" in broken["reason"], broken

    @pytest.mark.asyncio
    async def test_dry_placeholder_is_not_a_pass(self, no_counter):
        qc = await HB().run(check_assembly_qc, "x.mp4", {"clips": [{"id": "A01", "seg_s": 1}], "dry_run": True}, "ep40", {})
        assert qc["verdict"] == "DRY_RUN" and qc["blocking"] is False


# ─────────────────────────────────────────────────────────────────────────────
# shots.json: clip_prompt + trims
# ─────────────────────────────────────────────────────────────────────────────

def _shot(**kw) -> dict:
    base = {"id": "A01", "count": 1, "characters": ["D"], "wet": False, "model": "kling",
            "duration": 5.0, "end_frame": False, "prompt": "A duck.", "ref_keys": []}
    base.update(kw)
    return base


class TestShotData:
    def test_shot_data_clip_prompt_parsing(self):
        out = parse_shots({"shots": [_shot(clip_prompt="  The duck waddles left.  ")]})
        assert out["A01"]["prompt"] == "A duck." and out["A01"]["clip_prompt"] == "The duck waddles left."

    def test_shot_data_clip_prompt_optional(self):
        out = parse_shots({"shots": [_shot()]})
        assert out["A01"]["clip_prompt"] is None
        assert out["A01"]["trim"] == {"in": 0.0, "out": 5.0}

    def test_clip_prompt_and_trim_validation(self):
        for bad in ({"clip_prompt": "   "}, {"clip_prompt": 5}, {"trim_in": -1}, {"trim_out": 6.0},
                    {"trim_in": 3, "trim_out": 2}, {"trim_in": "1"}, {"trim_out": True}):
            with pytest.raises(ShotDataError):
                parse_shots({"shots": [_shot(**bad)]})
        assert parse_shots({"shots": [_shot(trim_in=0.5, trim_out=4)]})["A01"]["trim"] == {"in": 0.5, "out": 4.0}

    def test_merge_carries_clip_prompt_and_trim(self):
        plans = parse_shots({"shots": [_shot(clip_prompt="Waddles.", trim_in=1, trim_out=4)]})
        plans["A01"].update({"refs": [], "missing_refs": []})
        merged = merge_into_shots("ep41", [{"shot_id": "A01", "prompt": "beatmap"}], plans, True)
        assert merged[0].get("clip_prompt") == "Waddles." and merged[0].get("trim") == {"in": 1.0, "out": 4.0}, merged[0]
        assert merged[0]["prompt"] == "A duck."


# ─────────────────────────────────────────────────────────────────────────────
# Clip prompt in the workflow: replaces the still prompt, moderation, lint, preflight
# ─────────────────────────────────────────────────────────────────────────────

BEATMAP = """
# Picture assembly test beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.5 (6.0) | Park | Day | D | Duck walks in park | Intro | - | - | K |
"""


def _write_episode(tmp_path: Path, episode_id: str, prompt: str, clip_prompt: str | None = None, **extra) -> str:
    ep_dir = tmp_path / "data" / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "BEATMAP.md").write_text(BEATMAP)
    shot = {"id": "A01", "prompt": prompt, "count": 1, "characters": ["D"], "wet": False,
            "model": "kling", "duration": 6, "end_frame": False, "ref_keys": [], **extra}
    if clip_prompt is not None:
        shot["clip_prompt"] = clip_prompt
    (ep_dir / "shots.json").write_text(json.dumps({"shots": [shot]}))
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": []}}))
    (ep_dir / "prompt_kit.json").write_text("{}")
    return str(ep_dir / "BEATMAP.md")


class ClipFactory:
    """save_asset_locally stand-in: clips become real local files (sources consumed in order)."""

    def __init__(self, tmp_path: Path, sources: list[str]):
        self.dir = tmp_path / "clips"
        self.dir.mkdir(exist_ok=True)
        self.sources = list(sources)
        self.saved: list[str] = []

    def activity(self):
        from hfvg.activities.save_assets import save_asset_locally as real_save

        @activity.defn(name="save_asset_locally")
        async def save(episode_id, shot_id, asset_url, asset_type, attempt=1):
            if asset_type != "clip":
                return await real_save(episode_id, shot_id, asset_url, asset_type, attempt)
            src = self.sources.pop(0) if len(self.sources) > 1 else self.sources[0]
            path = self.dir / f"{shot_id}_{len(self.saved) + 1}.mp4"
            await asyncio.to_thread(make_clip, path, 6.0, src)
            self.saved.append(str(path))
            return str(path)

        return save


def _worker(env, h: Harness, clips: ClipFactory | None = None):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    acts = h.activities("kling")
    if clips is not None:
        acts = [a for a in acts if getattr(a, "__temporal_activity_definition").name != "save_asset_locally"]
        acts.append(clips.activity())
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow], activities=acts)


async def _to_final_cut(env, h, tmp_path, episode_id, prompt, clip_prompt=None, **extra):
    beatmap = _write_episode(tmp_path, episode_id, prompt, clip_prompt, **extra)
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[episode_id, beatmap, True, None, None, None],
        id=episode_id, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _until(lambda: h.still_calls, what="still submitted")
    await asyncio.sleep(0.5)
    await handle.execute_update("approve_g2_scene_stills")
    result = await _child_result(env, f"{episode_id}-shot-A01", timeout=90)
    state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT" and (s["assembly"]["qc"] or {}).get("verdict"),
                        timeout=90, what="G_FINAL_CUT with assembly QC")
    return result, handle, state


class TestClipPromptInWorkflow:
    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_clip_prompt_replaces_still_prompt(self, dry_env, tmp_path):  # noqa: F811
        """The still is submitted with the still prompt; the clip with clip_prompt ALONE."""
        h = Harness({})
        still_prompt = lint_ok("A duck sits on a bench beside a red lantern in heavy snow")
        clip_prompt = lint_ok("The duck turns its head and blinks while snow drifts down")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle, _ = await _to_final_cut(env, h, tmp_path, "ep51", still_prompt, clip_prompt)
                assert result["status"] == "completed", result
                assert h.still_calls[0][2] == still_prompt
                assert len(h.clip_calls) == 1 and h.clip_calls[0][3] == clip_prompt, h.clip_calls[0][3]
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_no_clip_prompt_falls_back_to_still_prompt(self, dry_env, tmp_path):  # noqa: F811
        h = Harness({})
        still_prompt = lint_ok("A duck sits on a bench")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle, _ = await _to_final_cut(env, h, tmp_path, "ep52", still_prompt)
                assert h.clip_calls[0][3] == still_prompt
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_moderation_rewords_the_submitted_clip_prompt(self, dry_env, tmp_path):  # noqa: F811
        """A blocked clip is reworded from the clip prompt that was submitted, not the still prompt."""
        h = Harness({"clip": 1})
        still_prompt = lint_ok("A duck sits on a bench beside a lantern")
        clip_prompt = lint_ok("A wet duck waddles through dripping reeds")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle, _ = await _to_final_cut(env, h, tmp_path, "ep53", still_prompt, clip_prompt)
                assert result["status"] == "completed", result
                first, second = h.clip_calls
                assert first[3] == clip_prompt
                assert "duck waddles through" in second[3] and "wet" not in second[3].lower().split()
                assert "dripping" not in second[3].lower() and "bench" not in second[3], second[3]
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)


class TestClipPromptLimits:
    def _route(self):
        from hfvg import preflight
        return {"clip_model": preflight.clip_model_path("kling")}

    def test_long_still_prompt_with_short_clip_prompt_fits_kling(self):
        """Ep05 A02 shape: a 2197-char still prompt. The clip submits clip_prompt alone, which fits
        Kling's 2500 limit; still + clip appended (the first Round 3 push) would not."""
        from hfvg import model_caps, preflight
        from hfvg.workflows.shot import ShotWorkflow

        still = lint_ok("A duck in the snow " + "with soft lantern light " * 200)[:2197]
        assert len(still) == 2197
        clip = lint_ok("The duck turns its head; snow drifts; lantern flickers. " * 6)
        plan = {"prompt": still, "clip_prompt": clip}
        submitted = ShotWorkflow._submitted_base_prompt("clip", plan)
        assert submitted == clip
        assert ShotWorkflow._submitted_base_prompt("still", plan) == still
        assert ShotWorkflow._submitted_base_prompt("clip", {"prompt": still, "clip_prompt": None}) == still
        reg = model_caps.get_registry()
        ok = preflight.preflight_shot({"id": "A02", "clip_prompt": submitted, "duration": 5.0, "start_frame": "https://cdn.example.com/s.png"},
                                      self._route(), reg, phase="clip")
        assert not [v for v in ok if v.severity == "error"], ok
        appended = preflight.preflight_shot({"id": "A02", "clip_prompt": f"{still} {clip}", "duration": 5.0,
                                             "start_frame": "https://cdn.example.com/s.png"},
                                            self._route(), reg, phase="clip")
        assert [v for v in appended if v.severity == "error" and "prompt" in v.field], \
            "still + clip must exceed Kling's prompt limit"

    def test_episode_preflight_checks_clip_prompt(self):
        """Plan-time preflight sees the merged clip_prompt (an over-long clip prompt is caught at G1)."""
        from hfvg import model_caps, preflight

        shot = {"id": "A01", "prompt": "short", "clip_prompt": "x" * 2600, "duration": 5.0,
                "start_frame": "https://cdn.example.com/s.png"}
        errs = [v for v in preflight.preflight_shot(shot, self._route(), model_caps.get_registry(), phase="clip")
                if v.severity == "error" and "prompt" in v.field]
        assert errs, "an over-long clip_prompt must fail clip preflight"

    def test_clip_lint_rejects_frozen_scene(self):
        from hfvg.prompt_lint import PromptLintError, enforce_prompt_lint

        frozen = lint_ok("The duck sits; everything else is still")
        assert enforce_prompt_lint("ep54", frozen, lint_meta(), live=False) == []  # still prompt rules
        with pytest.raises(PromptLintError, match="freezes the scene"):
            enforce_prompt_lint("ep54", frozen, lint_meta(), live=True, kind="clip")
        assert enforce_prompt_lint("ep54", lint_ok("The duck waddles; snow drifts"), lint_meta(),
                                   live=True, kind="clip") == []

    @pytest.mark.asyncio
    async def test_submit_clip_lints_final_prompt_with_clip_rules(self, tmp_path, monkeypatch):
        """submit_clip_job_enforced runs the clip lint (motion rules) on the prompt it is given."""
        from hfvg.prompt_lint import PromptLintError
        from tests.test_moderation_recovery import _live_episode, _live_routes
        from hfvg.activities.studio_generation import submit_clip_job_enforced

        await _live_episode(tmp_path, monkeypatch)
        with respx.mock(assert_all_called=False) as router:
            submit = _live_routes(router)
            err = None
            try:
                await submit_clip_job_enforced(
                    "ep99", "A01", "https://cdn.example.com/start.png",
                    lint_ok("Duck sits, nothing moves"), 5.0, 1, shot_meta=lint_meta())
            except Exception as e:  # noqa: BLE001 - asserted below
                err = e
            assert isinstance(err, PromptLintError) and "freezes the scene" in str(err), err
            assert submit.called is False

    @pytest.mark.asyncio
    async def test_submit_clip_preflights_final_prompt(self, tmp_path, monkeypatch):
        """Live clip submit runs model_caps preflight on the prompt it actually submits: an over-long
        final clip prompt (> Kling 2500) is refused before any provider call."""
        from hfvg.preflight import PreflightError
        from tests.test_moderation_recovery import _live_episode, _live_routes
        from hfvg.activities.studio_generation import submit_clip_job_enforced

        await _live_episode(tmp_path, monkeypatch)
        long_prompt = lint_ok("The duck turns its head while snow drifts past the reeds. " * 50)
        assert len(long_prompt) > 2500
        with respx.mock(assert_all_called=False) as router:
            submit = _live_routes(router)
            err = None
            try:
                await submit_clip_job_enforced(
                    "ep99", "A01", "https://cdn.example.com/start.png",
                    long_prompt, 5.0, 1, shot_meta=lint_meta())
            except Exception as e:  # noqa: BLE001 - asserted below
                err = e
            assert isinstance(err, PreflightError), err
            assert submit.called is False


# ─────────────────────────────────────────────────────────────────────────────
# Episode workflow: assembly wired after clips, before G_FINAL_CUT; routing
# ─────────────────────────────────────────────────────────────────────────────

@needs_ffmpeg
class TestAssemblyInEpisode:
    @pytest.mark.asyncio
    @pytest.mark.timeout(240)
    async def test_no_checker_escalates_and_holds_final_cut(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """Real clip -> real trimmed edit (trims from shots.json) at a deterministic path in state;
        no animal checker -> NEEDS_REVIEW: final cut approval refused until a human accepts."""
        h = Harness({})
        clips = ClipFactory(tmp_path, [MOVING])
        prompt = lint_ok("A duck waddles through the snow")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, clips):
                result, handle, state = await _to_final_cut(env, h, tmp_path, "ep61", prompt,
                                                            trim_in=1, trim_out=4)
                assert result["clip_path"] == clips.saved[0]
                asm = state["assembly"]
                assert asm["path"] == str(edit_dir("ep61") / "mute_assembly.mp4") and Path(asm["path"]).is_file()
                clip0 = asm["manifest"]["clips"][0]
                assert (clip0["id"], clip0["trim_in"], clip0["frames"]) == ("A01", 1.0, 90), clip0
                assert state["output_paths"]["A01"]["clip"] == clips.saved[0]
                assert asm["qc"]["verdict"] == "NEEDS_REVIEW" and asm["blocking"] is True, asm["qc"]
                assert state["stage"] == "mute_edit"

                await handle.signal("approve_g_final_cut")
                state = await _wait(handle, lambda s: s["final_cut_refusals"], what="refusal")
                assert "assembly QC NEEDS_REVIEW" in state["final_cut_refusals"][-1]
                assert state["approvals"]["g_final_cut"] is False
                await handle.signal("accept_assembly_qc", "  ")  # no reason -> ignored
                state = await _wait(handle, lambda s: len(s["final_cut_refusals"]) == 2, what="2nd refusal")
                assert state["assembly"]["blocking"] is True

                await handle.signal("accept_assembly_qc", "checked count and duck by eye")
                await _wait(handle, lambda s: s["assembly"]["acceptance"], what="acceptance")
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_still_only_fail_blocks_until_clip_retry_rebuilds(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """A frozen clip -> assembly QC FAIL: an approval sent early is withdrawn and later approvals
        are refused (never silently to G_FINAL_CUT). A clip retry lands a moving clip -> the edit is
        rebuilt from it -> PASS (deterministic counter) -> final cut approval counts."""
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        h = Harness({})
        clips = ClipFactory(tmp_path, [FROZEN, MOVING])
        prompt = lint_ok("A duck waddles through the snow")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, clips):
                beatmap = _write_episode(tmp_path, "ep62", prompt)
                handle = await env.client.start_workflow(
                    "EpisodeWorkflowV2", args=["ep62", beatmap, True, None, None, None],
                    id="ep62", task_queue=TASK_QUEUE)
                await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
                await handle.execute_update("approve_g1_shot_plan")
                await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
                await _until(lambda: h.still_calls, what="still")
                await asyncio.sleep(0.5)
                await handle.signal("approve_g_final_cut")  # early approval, before the edit exists
                await handle.execute_update("approve_g2_scene_stills")
                state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                                    and (s["assembly"]["qc"] or {}).get("verdict"), timeout=90, what="QC")
                qc = state["assembly"]["qc"]
                assert qc["verdict"] == "FAIL" and qc["failed_shots"] == ["A01"], qc
                assert state["approvals"]["g_final_cut"] is False
                assert any("approval withdrawn" in r for r in state["final_cut_refusals"]), state["final_cut_refusals"]
                await asyncio.sleep(1.0)
                desc = await handle.describe()
                assert desc.status.name == "RUNNING", "FAIL must not pass G_FINAL_CUT"

                await handle.execute_update("retry_clip", args=["A01", "still-only clip", None])
                state = await _wait(handle, lambda s: s["assembly"]["edit_version"] == 2
                                    and (s["assembly"]["qc"] or {}).get("edit_version") == 2,
                                    timeout=120, what="rebuilt edit")
                assert state["assembly"]["qc"]["verdict"] == "PASS", state["assembly"]["qc"]
                assert state["assembly"]["manifest"]["clips"][0]["source"] == clips.saved[1]
                assert state["stage"] == "picture_lock"
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(200)
    async def test_completed_shot_without_local_clip_escalates(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """A completed shot whose clip never reached disk is not silently dropped from the edit."""
        h = Harness({})

        @activity.defn(name="save_asset_locally")
        async def broken_save(episode_id, shot_id, asset_url, asset_type, attempt=1):
            if asset_type == "clip":
                raise ApplicationError("disk full", non_retryable=True)
            from hfvg.activities.save_assets import save_asset_locally as real_save
            return await real_save(episode_id, shot_id, asset_url, asset_type, attempt)

        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        acts = [a for a in h.activities("kling")
                if getattr(a, "__temporal_activity_definition").name != "save_asset_locally"] + [broken_save]
        async with await WorkflowEnvironment.start_local() as env:
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                              activities=acts):
                result, handle, state = await _to_final_cut(env, h, tmp_path, "ep63", lint_ok("A duck"))
                assert result["status"] == "completed" and result["clip_path"] is None, result
                qc = state["assembly"]["qc"]
                assert qc["verdict"] == "NEEDS_REVIEW" and "A01" in qc["reason"], qc
                await handle.signal("approve_g_final_cut")
                state = await _wait(handle, lambda s: s["final_cut_refusals"], what="refusal")
                await handle.terminate("test done")


# ─────────────────────────────────────────────────────────────────────────────
# API
# ─────────────────────────────────────────────────────────────────────────────

class TestAssemblyAPI:
    @pytest.mark.asyncio
    async def test_state_exposes_assembly_and_accept_sends_signal(self, tmp_path, monkeypatch):
        from tests.test_gate_correctness_round1 import _api_client

        client, api_main, wf_handle = await _api_client(str(tmp_path / "api.db"), monkeypatch)
        assembly = {"path": "/data/episodes/ep71/edit/mute_assembly.mp4", "manifest": {"clips": []},
                    "qc": {"verdict": "FAIL", "blocking": True, "reason": "A01 still-only"},
                    "acceptance": None, "blocking": True, "stale": False, "edit_version": 1}
        wf_handle.query = AsyncMock(return_value={
            "episode_id": "ep71", "stage": "mute_edit", "approvals": {}, "shots": 1, "g1_plan": {},
            "assembly": assembly})
        wf_handle.signal = AsyncMock()
        resp = client.get("/api/episodes/ep71")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["assembly_path"] == assembly["path"]
        assert body["assembly"]["qc"]["verdict"] == "FAIL" and body["assembly"]["blocking"] is True

        resp = client.post("/api/episodes/ep71/assembly/accept", json={"reason": "count verified by eye"})
        assert resp.status_code == 200, resp.text
        wf_handle.signal.assert_awaited_once_with("accept_assembly_qc", "count verified by eye")
        assert client.post("/api/episodes/ep71/assembly/accept", json={"reason": ""}).status_code == 422


def test_worker_registers_assembly_activities():
    from hfvg import worker

    names = {getattr(a, "__temporal_activity_definition").name for a in worker.PRODUCTION_ACTIVITIES}
    assert {"assemble_mute_edit", "check_assembly_qc"} <= names


# ─────────────────────────────────────────────────────────────────────────────
# The PR #17 originals, kept by name and adapted to the real interfaces
# (activities run in an activity context; no animal checker -> NEEDS_REVIEW, never PASS)
# ─────────────────────────────────────────────────────────────────────────────

@needs_ffmpeg
def test_motion_check_frozen_video(tmp_path):
    """Frozen (still-only) video fails the stillgate."""
    video = make_clip(tmp_path / "frozen.mp4", 3.0, "color=c=black", size="180x320", rate=1)
    results = check_motion(str(video), {"clips": [{"id": "FROZEN", "seg_s": 3.0}], "dissolves": {}})
    assert len(results) == 1 and results[0].verdict == "FAIL", results


@needs_ffmpeg
@pytest.mark.asyncio
async def test_assembly_qc_integration(tmp_path, monkeypatch, no_counter):
    """Assembly QC runs the stillgate on every shot of the edit, plus the animal checks; with no
    checker registered it escalates, with a deterministic checker a moving edit passes."""
    monkeypatch.setenv("DRY_RUN", "false")
    clip = make_clip(tmp_path / "a.mp4", 6.0, MOVING)
    shots = [{"shot_id": "A01", "duration": 3.0}, {"shot_id": "A02", "duration": 3.0}]
    edit = await HB().run(assemble_mute_edit, "ep45", shots, {"A01": str(clip), "A02": str(clip)}, None)
    metas = {"A01": lint_meta(1), "A02": lint_meta(1)}
    unchecked = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep45", metas)
    assert unchecked["verdict"] == "NEEDS_REVIEW" and unchecked["escalate"] is True, unchecked
    assert len(unchecked["motion_results"]) == 2
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    result = await HB().run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep45", metas)
    assert result["verdict"] == "PASS", result
    assert [m["shot_id"] for m in result["motion_results"]] == ["A01", "A02"]


@pytest.mark.asyncio
async def test_duck_presence_check_fake_counter(tmp_path):
    """Duck presence with the deterministic counter: a match passes, a miss fails."""
    still = tmp_path / "s.jpg"
    still.write_bytes(b"\xff\xd8\xff\xe0")
    ok = await DuckPresenceCheck(FakeAnimalCounter({"duck": 1})).check_still(still, expected_count=1)
    assert ok["passed"] is True and ok["escalate"] is False and ok["details"]["count"] == 1, ok
    miss = await DuckPresenceCheck(FakeAnimalCounter({"duck": 0})).check_still(still, expected_count=1)
    assert miss["passed"] is False, miss


@needs_ffmpeg
def test_stillgate_analysis_validates_manifest(tmp_path):
    """An empty manifest does not crash the stillgate analysis."""
    video = make_clip(tmp_path / "t.mp4", 2.0, MOVING)
    results = _run_stillgate_analysis(str(video), {"clips": [], "dissolves": {}})
    assert isinstance(results, list) and len(results) == 0
