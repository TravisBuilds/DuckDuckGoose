"""Child processes and long waits inside Temporal activities: keep heartbeating, never orphan a child.

The heartbeat timeout of an activity (1-2 min in the episode workflow) is there to notice a dead
worker, not a slow step. An activity that heartbeats only between its steps is timed out by any single
step that outlasts it - one ffmpeg encode of a long 1080x1920 cut on a small droplet, a RIFE render, a
slow provider call. The SDK then cancels the attempt on the worker, and a child that is not killed
with it keeps running next to the retry (both slower, so every retry timed out the same way).

- ``run``: a child process that heartbeats every ``beat_interval()`` while it runs (inside an activity)
  and, when the awaiting task is cancelled or fails, is killed with its whole process group and reaped
  before the error propagates, so the caller's temp dir cleanup never races a live writer (GAP-70).
- ``keep_alive``: the same heartbeat around any other awaitable (a provider call, a worker thread);
  cancelling the caller cancels the awaitable and waits for its cleanup.

Outside an activity (CLI, tests) nothing heartbeats. The heartbeat details name what is being waited
for, ``{"waiting": what, "elapsed_s": ...}``, and never carry a ``"stage"``: the activities' own stage
heartbeats are unchanged.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any, Awaitable, TypeVar

from temporalio import activity

T = TypeVar("T")

BEAT_EVERY_S = 20.0
MIN_BEAT_EVERY_S = 0.05
REAP_TIMEOUT_S = 10.0


def beat_interval() -> float | None:
    """Seconds between keep-alive heartbeats in the current activity attempt (None outside one).

    A quarter of the attempt's heartbeat timeout, at most BEAT_EVERY_S. The SDK forwards at most one
    heartbeat per 0.8 x heartbeat timeout (60 s cap) to the server, so the server sees one well inside
    the timeout for as long as the child runs."""
    if not activity.in_activity():
        return None
    every = BEAT_EVERY_S
    timeout = activity.info().heartbeat_timeout
    if timeout:
        every = min(every, timeout.total_seconds() / 4)
    return max(MIN_BEAT_EVERY_S, every)


def _beat(details: dict[str, Any]) -> None:
    try:
        activity.heartbeat(details)
    except RuntimeError:  # not inside an activity
        pass


async def keep_alive(aw: Awaitable[T], what: str) -> T:
    """Await ``aw``, heartbeating when it starts and every beat_interval() until it is done (inside an
    activity; a plain await outside one). If the caller is cancelled (a heartbeat-delivered activity
    cancellation, a worker shutdown) ``aw`` is cancelled and finishes its own cleanup first."""
    every = beat_interval()
    if every is None:
        return await aw
    task = asyncio.ensure_future(aw)
    loop = asyncio.get_running_loop()
    start = loop.time()
    _beat({"waiting": what, "elapsed_s": 0.0})
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every)
            if done:
                return task.result()
            _beat({"waiting": what, "elapsed_s": round(loop.time() - start, 1)})
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task})
        if not task.cancelled():
            task.exception()  # retrieved: raised above, or superseded by the caller's own error


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the child's process group (the child leads its own session) and reap the child."""
    if proc.returncode is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(asyncio.shield(proc.wait()), REAP_TIMEOUT_S)
    except asyncio.TimeoutError:
        pass


async def _start(cmd: list[str], cwd: str | None, stdin: int) -> asyncio.subprocess.Process:
    """create_subprocess_exec that a cancellation does not interrupt halfway.

    Cancelled while the child's pipes are being connected, asyncio kills that child alone and (3.11,
    3.12) then waits until every pipe closes, i.e. for as long as a child of the child holds them. So
    the start is finished and the whole process group killed before the cancellation propagates."""
    start = asyncio.ensure_future(asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, stdin=stdin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True))
    try:
        return await asyncio.shield(start)
    except asyncio.CancelledError:
        while not start.done():  # milliseconds; a repeated cancel must not orphan the child
            try:
                await asyncio.wait({start})
            except asyncio.CancelledError:
                pass
        if not start.cancelled() and start.exception() is None:
            await _kill(start.result())
        raise


async def run(cmd: list[str], what: str, *, cwd: str | None = None, stdin: bytes | None = None,
              timeout: float | None = None) -> tuple[int, bytes, bytes]:
    """Run ``cmd`` to completion without blocking the event loop: (returncode, stdout, stderr).

    Inside an activity it heartbeats while the child runs (keep_alive). Cancellation (also while the
    child is starting), ``timeout`` (seconds; raises TimeoutError) or any other error while waiting
    kills the child's process group and reaps the child before the exception propagates. stdin is
    /dev/null unless bytes are given."""
    proc = await _start(cmd, cwd, asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL)
    try:
        comm = proc.communicate(stdin)
        if timeout is not None:
            comm = asyncio.wait_for(comm, timeout)
        out, err = await keep_alive(comm, what)
    except BaseException:
        await _kill(proc)
        raise
    return proc.returncode, out, err
