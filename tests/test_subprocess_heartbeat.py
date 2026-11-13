"""Heartbeats while a child process or a provider call runs; no child outlives a cancelled attempt.

Production (main e1c5181, 4 vCPU / 8 GB droplet): the delivery's mix_and_deliver failed all 3 attempts
with "activity Heartbeat timeout" after ~6 min each. The activity heartbeat only between its steps, one
ffmpeg encode of the 136 s 1080x1920 cut outlasted the 2 min heartbeat timeout, and the timed-out
attempt went on encoding next to its retry. Here a shim (tests.slow_bin) holds one child longer than
the attempt's heartbeat timeout: the longest stretch without a heartbeat must stay inside the timeout,
and a cancelled attempt must leave no child running."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import httpx
import pytest
import respx

from hfvg import audio_mix, interp_rife, subproc
from hfvg.activities import audio as act
from hfvg.activities import media
from hfvg.activities.media import assemble_mute_edit
from hfvg.activities.qc import check_assembly_qc, check_motion_qc
from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
from tests.lint_helpers import lint_meta
from tests.slow_bin import TimedHeartbeats, alive, slow, wait_dead  # noqa: F401
from tests.test_audio_delivery import episode
from tests.test_audio_round4_activities import API, MP3, _init_db, live  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, make_clip, make_color_clip, no_counter  # noqa: F401
from tests.test_r12_gaps import fake_rife  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")

HEARTBEAT_S = 3.0  # the attempt's heartbeat timeout here (production: 1-2 min)
HOLD_S = 4.5       # one held child, longer than the heartbeat timeout


def _silence_ok(hb: TimedHeartbeats, what: str) -> None:
    silence, ended_by = hb.longest_silence()
    assert silence < hb.timeout, (
        f"{what}: {silence:.1f}s without a heartbeat, past the {hb.timeout:.0f}s heartbeat timeout "
        f"(Temporal would time this attempt out); the silence ended with {ended_by!r}")


async def _until(pred, timeout: float, what: str) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, f"{what} not reached"
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ hfvg.subproc

async def test_run_heartbeats_while_the_child_runs():
    hb = TimedHeartbeats(2.0)

    async def body():
        return await subproc.run(["sh", "-c", "sleep 2.5; echo done"], "slow child")

    rc, out, err = await hb.run(body)
    assert (rc, out) == (0, b"done\n"), err
    silence, _ = hb.longest_silence()
    assert silence < 1.0, f"{silence:.2f}s without a heartbeat (one every heartbeat timeout / 4 = 0.5 s)"
    beats = [d for _, d in hb.beats]
    assert len(beats) >= 4, beats
    assert all(d["waiting"] == "slow child" and "stage" not in d for d in beats), beats


async def test_outside_an_activity_run_just_runs():
    assert subproc.beat_interval() is None
    rc, out, _ = await subproc.run(["sh", "-c", 'printf %s "$(cat)"; exit 3'], "stdin", stdin=b"abc")
    assert (rc, out) == (3, b"abc")
    rc, out, _ = await subproc.run(["sh", "-c", "cat"], "no stdin")
    assert (rc, out) == (0, b""), "a child without stdin bytes must read /dev/null, not the worker's stdin"


async def test_cancel_kills_the_child_and_its_process_group(tmp_path):
    pids = tmp_path / "pids"
    task = asyncio.ensure_future(subproc.run(["sh", "-c", f"sleep 30 & echo $$ $! > {pids}; wait"], "group"))
    await _until(lambda: pids.exists() and len(pids.read_text().split()) == 2, 10, "child started")
    child, grandchild = (int(p) for p in pids.read_text().split())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not alive(child), "the cancelled child is still running (it was not reaped before the error)"
    assert wait_dead([grandchild], 2.0) == [], "the child's own child (same process group) is still running"


async def test_timeout_kills_the_child(tmp_path):
    pid = tmp_path / "pid"
    with pytest.raises(TimeoutError):
        await subproc.run(["sh", "-c", f"echo $$ > {pid}; exec sleep 30"], "hung", timeout=0.5)
    assert not alive(int(pid.read_text())), "a timed-out child is still running"


async def test_keep_alive_heartbeats_and_cancels_what_it_waits_for():
    hb = TimedHeartbeats(2.0)

    async def provider_call(seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "ok"

    async def body():
        return await subproc.keep_alive(provider_call(2.5), "provider call")

    assert await hb.run(body) == "ok"
    assert hb.longest_silence()[0] < 1.0, hb.beats

    cancelled = asyncio.Event()

    async def hung_call():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def hung():
        return await subproc.keep_alive(hung_call(), "provider call")

    hb = TimedHeartbeats(2.0)
    task = asyncio.ensure_future(hb.run(hung))
    await _until(lambda: hb.beats, 5, "first heartbeat")
    hb.env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert cancelled.is_set(), "cancelling the activity did not cancel the awaited call"


# ------------------------------------------------------------------ audio + delivery activities

@needs_ffmpeg
@pytest.mark.timeout(180)
async def test_mix_and_deliver_heartbeats_through_an_encode_longer_than_the_heartbeat_timeout(tmp_path, slow):  # noqa: F811
    """The production failure, scaled down: the phone encode's first pass is held past the attempt's
    heartbeat timeout (main: no heartbeat from "phone_encode" until both passes and the picture sync
    decode were done)."""
    mute, plan = episode(tmp_path, "ep-hb1")
    slow.hold("ffmpeg", "-pass 1", HOLD_S)
    hb = TimedHeartbeats(HEARTBEAT_S)
    manifest = await hb.run(act.mix_and_deliver, "ep-hb1", str(mute), plan, None)
    assert len(slow.held()) == 1, f"the phone encode was not held once: {slow.held()}"
    assert Path(manifest["outputs"]["phone"]["path"]).is_file() and manifest["phone"]["within_limit"]
    assert "phone_encode" in hb.stages() and hb.stages()[-1] == "done", hb.stages()
    _silence_ok(hb, "mix_and_deliver")


@needs_ffmpeg
@pytest.mark.timeout(180)
async def test_cancelled_mix_attempt_kills_its_encode_and_leaves_no_work_dir(tmp_path, slow):  # noqa: F811
    """A cancelled attempt (heartbeat timeout seen by the worker, workflow cancel, worker shutdown) must
    not keep encoding next to the retry, and its temp dir is removed after the child is dead."""
    mute, plan = episode(tmp_path, "ep-hb2")
    slow.hold("ffmpeg", "-pass 1", 60)
    hb = TimedHeartbeats(HEARTBEAT_S)
    task = asyncio.ensure_future(hb.run(act.mix_and_deliver, "ep-hb2", str(mute), plan, None))
    await _until(slow.held, 120, "phone encode")
    hb.env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 15)
    assert wait_dead(slow.held(), 3.0) == [], "the cancelled attempt's encode is still running"
    left = list(audio_mix.delivery_dir("ep-hb2").glob(".work-*"))
    assert not left, f"the cancelled attempt left its work dir: {left}"


@needs_ffmpeg
@pytest.mark.timeout(180)
async def test_delivery_qc_heartbeats_through_a_slow_measurement(tmp_path, slow):  # noqa: F811
    mute, plan = episode(tmp_path, "ep-hb3")
    manifest = await audio_mix.mix_and_deliver("ep-hb3", str(mute), plan)
    slow.hold("ffmpeg", "_phone.mp4 -vn", HOLD_S)  # the phone file's loudness measurement
    hb = TimedHeartbeats(HEARTBEAT_S)
    qc = await hb.run(act.check_delivery_qc, "ep-hb3", manifest, plan, None)
    assert qc["verdict"] == "PASS", qc["failed"]
    assert len(slow.held()) == 1, slow.held()
    _silence_ok(hb, "check_delivery_qc")


async def test_generate_voiceover_heartbeats_through_a_slow_provider_call(live):  # noqa: F811
    """A TTS call slower than the heartbeat timeout (main: one heartbeat before the call): Temporal
    timed the attempt out and the retry paid for the same line again."""
    await _init_db()

    async def slow_tts(request):
        await asyncio.sleep(HOLD_S)
        return httpx.Response(200, content=MP3)

    hb = TimedHeartbeats(HEARTBEAT_S)
    with respx.mock() as mock:
        route = mock.post(f"{API}/v1/text-to-speech/voice-1").mock(side_effect=slow_tts)
        res = await hb.run(act.generate_voiceover, "ep01", "A slow line.", "voice-1", "eleven_v3", "L01", None)
    assert route.call_count == 1 and res["source"] == "generated", res
    _silence_ok(hb, "generate_voiceover")


# ------------------------------------------------------------------ picture activities

@needs_ffmpeg
@pytest.mark.timeout(180)
async def test_mute_edit_heartbeats_through_a_slow_concat(tmp_path, monkeypatch, slow):  # noqa: F811
    monkeypatch.setenv("DRY_RUN", "false")
    a = make_color_clip(tmp_path / "a.mp4", [("red", 1.0)])
    b = make_color_clip(tmp_path / "b.mp4", [("blue", 1.0)])
    slow.hold("ffmpeg", "-f concat", HOLD_S)
    hb = TimedHeartbeats(HEARTBEAT_S)
    res = await hb.run(assemble_mute_edit, "ep-hb4",
                       [{"shot_id": "A01", "duration": 1.0}, {"shot_id": "A02", "duration": 1.0}],
                       {"A01": str(a), "A02": str(b)}, None)
    assert Path(res["assembly_path"]).is_file()
    assert len(slow.held()) == 1, slow.held()
    _silence_ok(hb, "assemble_mute_edit")


@needs_ffmpeg
@pytest.mark.timeout(180)
async def test_assembly_qc_heartbeats_through_a_slow_motion_decode(tmp_path, monkeypatch, no_counter, slow):  # noqa: F811
    monkeypatch.setenv("DRY_RUN", "false")
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    clip = make_clip(tmp_path / "a.mp4", 2.0)
    edit = await TimedHeartbeats(HEARTBEAT_S).run(
        assemble_mute_edit, "ep-hb5", [{"shot_id": "A01", "duration": 2.0}], {"A01": str(clip)}, None)
    slow.hold("ffmpeg", "fps=24,scale=", HOLD_S)  # the stillgate decode of the edit
    hb = TimedHeartbeats(HEARTBEAT_S)
    qc = await hb.run(check_assembly_qc, edit["assembly_path"], edit["manifest"], "ep-hb5", {"A01": lint_meta(1)})
    assert qc["verdict"] == "PASS", qc
    assert len(slow.held()) == 1, slow.held()
    _silence_ok(hb, "check_assembly_qc")


@needs_ffmpeg
@pytest.mark.timeout(120)
async def test_clip_motion_qc_heartbeats_and_keeps_the_event_loop_free(tmp_path, slow):  # noqa: F811
    """review_clip's ffprobe / blackdetect / freezedetect ran as blocking calls on the worker's event
    loop: no heartbeat from any activity of the worker (a concurrent delivery included) until they returned."""
    clip = make_clip(tmp_path / "c.mp4", 2.0)
    slow.hold("ffmpeg", "blackdetect", HOLD_S)
    lags: list[float] = []

    async def ticker():
        loop = asyncio.get_running_loop()
        while True:
            t = loop.time()
            await asyncio.sleep(0.05)
            lags.append(loop.time() - t - 0.05)

    tick = asyncio.ensure_future(ticker())
    await asyncio.sleep(0.1)  # the ticker is waiting in its sleep when the activity starts
    hb = TimedHeartbeats(HEARTBEAT_S)
    try:
        result = await hb.run(check_motion_qc, str(clip), 2.0, "360x640", "9:16")
        await asyncio.sleep(0.1)  # a tick that spanned a blocked loop is recorded when it resumes
    finally:
        tick.cancel()
    assert result["passed"], result["issues"]
    assert len(slow.held()) == 1, slow.held()
    assert max(lags) < 1.0, f"the worker's event loop was blocked for {max(lags):.1f}s"
    _silence_ok(hb, "check_motion_qc")


async def test_cancelled_rife_render_does_not_outlive_the_attempt(tmp_path):
    """interp_rife heartbeats around its render; a cancelled attempt must also stop the render
    (main: the render task was left running, so media._run's GAP-70 kill never fired)."""
    pid = tmp_path / "pid"
    beats: list = []
    task = asyncio.ensure_future(interp_rife._run_with_heartbeat(
        media._run, ["sh", "-c", f"echo $$ > {pid}; exec sleep 30"], "rife render", beats.append, {"segment": 0}))
    await _until(lambda: pid.exists() and pid.read_text().strip(), 10, "render started")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wait_dead([int(pid.read_text())], 3.0) == [], "the RIFE render outlived its cancelled attempt"


@needs_ffmpeg
@pytest.mark.timeout(120)
async def test_rife_retime_heartbeats_through_a_slow_frame_check(tmp_path, fake_rife, monkeypatch):  # noqa: F811
    """GAP-82 moved the GAP-81 frame check (hundreds of full-size PNGs) to a worker thread: the event
    loop stays free, but the attempt sent no heartbeat until the check returned."""
    import hashlib
    import time
    real = interp_rife.check_interp_frames

    def slow_check(*a, **k):
        time.sleep(HOLD_S)
        return real(*a, **k)
    monkeypatch.setattr(interp_rife, "check_interp_frames", slow_check)
    clip = make_clip(tmp_path / "a.mp4", 2.0, MOVING, size="72x128", rate=24)
    sha = hashlib.sha256(clip.read_bytes()).hexdigest()
    hb = TimedHeartbeats(HEARTBEAT_S)
    r = await hb.run(interp_rife.rife_retime, str(clip), sha, [[0.0, 1.5]], 60, 24, media._run, media._beat)
    assert not r["cached"]
    _silence_ok(hb, "rife_retime")
