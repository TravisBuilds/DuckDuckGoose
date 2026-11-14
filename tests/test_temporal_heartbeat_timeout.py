"""The production failure on a real Temporal server: local dev server, real worker, real heartbeats.

main e1c5181: mix_and_deliver heartbeat only between its steps. One phone encode outlasted the 2 min
heartbeat timeout, so the attempt timed out ("activity Heartbeat timeout"); the SDK cancelled it on
the worker, but the cancelled proc.communicate() left ffmpeg running next to the retry. A TTS call
slower than generate_voiceover's 1 min heartbeat timeout had the same fate, and its retry paid for
the line a second time. Each activity is scheduled here the way the episode
workflow schedules it (heartbeat timeout, retries), with the timeout scaled down and one child or
provider call held longer than it."""

from __future__ import annotations

import asyncio
import os
import shutil
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from temporalio import workflow
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.exceptions import TimeoutError as ActivityTimeoutError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities import audio as act
from tests.slow_bin import slow, wait_dead_async  # noqa: F401
from tests.test_audio_delivery import episode
from tests.test_audio_round4_activities import API, MP3, _init_db, live  # noqa: F401

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")

TASK_QUEUE = "heartbeat-timeout-tests"
HEARTBEAT_S = 3.0  # production: 2 min (mix_and_deliver), 1 min (generate_voiceover)
HOLD_S = 8.0


@workflow.defn(sandboxed=False)
class OneActivity:
    @workflow.run
    async def run(self, name: str, args: list, attempts: int) -> Any:
        return await workflow.execute_activity(
            name, args=args, start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=HEARTBEAT_S),
            retry_policy=RetryPolicy(maximum_attempts=attempts, initial_interval=timedelta(milliseconds=200)))


def _worker(env: WorkflowEnvironment) -> Worker:
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[OneActivity],
                  activities=[act.mix_and_deliver, act.generate_voiceover])


async def _outcome(handle: WorkflowHandle) -> str:
    try:
        await handle.result()
    except WorkflowFailureError as e:
        cause = e.cause.cause if isinstance(e.cause, ActivityError) else e.cause
        if isinstance(cause, ActivityTimeoutError) and cause.type is not None:
            return f"{cause.type.name} timeout ({cause})"
        return repr(cause)
    return "completed"


async def _until(pred, timeout: float, what: str) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        assert loop.time() < end, f"{what} not reached"
        await asyncio.sleep(0.05)


@needs_ffmpeg
@pytest.mark.timeout(120)
async def test_a_delivery_encode_longer_than_the_heartbeat_timeout_completes(tmp_path, slow):  # noqa: F811
    mute, plan = episode(tmp_path, "ep-t1")
    slow.hold("ffmpeg", "-pass 1", HOLD_S)
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            handle = await env.client.start_workflow(
                OneActivity.run, args=["mix_and_deliver", ["ep-t1", str(mute), plan, None], 1],
                id="ep-t1", task_queue=TASK_QUEUE)
            outcome = await _outcome(handle)
    assert len(slow.held()) == 1, f"the phone encode was not held once: {slow.held()}"
    assert outcome == "completed", f"mix_and_deliver did not complete: {outcome}"


@needs_ffmpeg
@pytest.mark.timeout(120)
async def test_cancelling_a_delivery_kills_its_encode(tmp_path, slow):  # noqa: F811
    """The cancel reaches the worker in the response to the next heartbeat; the attempt then kills its
    encode instead of leaving it to compete with the next one for the CPU."""
    mute, plan = episode(tmp_path, "ep-t2")
    slow.hold("ffmpeg", "-pass 1", 60)
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            handle = await env.client.start_workflow(
                OneActivity.run, args=["mix_and_deliver", ["ep-t2", str(mute), plan, None], 1],
                id="ep-t2", task_queue=TASK_QUEUE)
            await _until(slow.held, 60, "phone encode")
            await handle.cancel()
            outcome = await _outcome(handle)
            left = await wait_dead_async(slow.held(), 4 * HEARTBEAT_S)
    assert "Cancelled" in outcome, outcome
    assert left == [], f"the cancelled delivery's encode is still running: pids {left}"


@pytest.mark.timeout(120)
async def test_a_tts_call_longer_than_the_heartbeat_timeout_is_paid_for_once(live):  # noqa: F811
    await _init_db()
    received = []  # the provider bills a request it received, answered or not

    async def slow_tts(request):
        received.append(request)
        await asyncio.sleep(HOLD_S)
        return httpx.Response(200, content=MP3)

    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            with respx.mock(assert_all_called=False) as mock:
                mock.post(f"{API}/v1/text-to-speech/voice-1").mock(side_effect=slow_tts)
                handle = await env.client.start_workflow(
                    OneActivity.run,
                    args=["generate_voiceover", ["ep01", "A slow line.", "voice-1", "eleven_v3", "L01", None, 1, None], 2],
                    id="ep01-vo-L01", task_queue=TASK_QUEUE)
                outcome = await _outcome(handle)
    assert len(received) == 1, f"the VO line was requested (and billed) {len(received)} times; activity: {outcome}"
    assert outcome == "completed", outcome
