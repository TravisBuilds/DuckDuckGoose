"""A slow child process for heartbeat tests: a fake ``ffmpeg`` / ``ffprobe`` first on PATH holds every
call whose arguments contain a given text for N seconds (the slow CPU of a small droplet), then runs
the real binary with the same arguments. Each held call records its pid (the real binary's pid after
the exec) so a test can check whether a cancelled child is still running."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shlex
import shutil
import signal
import time
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.testing import ActivityEnvironment


class SlowBin:
    def __init__(self, tmp_path: Path, monkeypatch):
        self.dir = tmp_path / "slow-bin"
        self.dir.mkdir(exist_ok=True)
        self.pid_file = self.dir / "pids"
        self.real = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
        monkeypatch.setenv("PATH", f"{self.dir}{os.pathsep}{os.environ['PATH']}")

    def hold(self, name: str, match: str, seconds: float) -> None:
        """Hold every ``name`` call whose argument list contains ``match`` for ``seconds``."""
        shim = self.dir / name
        shim.write_text(
            "#!/bin/sh\n"
            f'case " $* " in\n'
            f"  *{shlex.quote(match)}*) echo $$ >> {shlex.quote(str(self.pid_file))}; sleep {seconds};;\n"
            "esac\n"
            f'exec {shlex.quote(self.real[name])} "$@"\n')
        shim.chmod(0o755)

    def held(self) -> list[int]:
        if not self.pid_file.exists():
            return []
        return [int(p) for p in self.pid_file.read_text().split()]

    def kill_leftovers(self) -> None:
        """Kill held calls that are still running, with their ``sleep``."""
        mine = str(self.dir.parent)
        for pid in self.held():
            if mine not in _cmdline(pid):  # gone, or the pid now belongs to someone else
                continue
            for p in [*_children(pid), pid]:
                try:
                    os.kill(p, signal.SIGKILL)
                except ProcessLookupError:
                    pass


@pytest.fixture
def slow(tmp_path, monkeypatch):
    """A SlowBin on PATH. Held calls a cancelled attempt left running are killed at teardown, so a
    failing run does not leave them behind for the rest of the session."""
    s = SlowBin(tmp_path, monkeypatch)
    yield s
    s.kill_leftovers()


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def _children(pid: int) -> list[int]:
    kids = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            ppid = stat.read_text().rsplit(")", 1)[1].split()[1]
        except (OSError, IndexError):
            continue
        if ppid == str(pid):
            kids.append(int(stat.parent.name))
    return kids


def _state(pid: int) -> str | None:
    """The state letter in /proc/<pid>/stat; None once the pid is gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError, IndexError):
        # reaped while we look: open() gives ENOENT or ESRCH, a read() after open() gives ESRCH
        return None


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # kill(0) still answers for a zombie (Z) and while its parent or init reaps it (X)
    return _state(pid) not in (None, "Z", "X")


def describe(pid: int) -> str:
    """``pid N [state] command line`` for an assertion message."""
    state = _state(pid)
    return f"pid {pid} gone" if state is None else f"pid {pid} [{state}] {_cmdline(pid).strip()!r}"


def kill_group(pgid: int, members: list[int]) -> None:
    """SIGKILL process group ``pgid`` while one of ``members`` still runs (a running member keeps the
    group id from being reused), so what a failed check left running does not outlive the test."""
    if any(alive(p) for p in members):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def wait_dead(pids: list[int], timeout: float = 5.0) -> list[int]:
    """The pids still alive after ``timeout`` seconds (blocks: only once no worker needs the loop)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and any(alive(p) for p in pids):
        time.sleep(0.05)
    return [p for p in pids if alive(p)]


async def wait_dead_async(pids: list[int], timeout: float = 5.0) -> list[int]:
    """wait_dead that keeps the event loop (and a worker running on it) going."""
    end = time.monotonic() + timeout
    while time.monotonic() < end and any(alive(p) for p in pids):
        await asyncio.sleep(0.05)
    return [p for p in pids if alive(p)]


class TimedHeartbeats:
    """ActivityEnvironment whose attempt has a heartbeat timeout, recording when each heartbeat came."""

    def __init__(self, heartbeat_timeout_s: float):
        self.timeout = heartbeat_timeout_s
        self.env = ActivityEnvironment()
        self.env.info = dataclasses.replace(self.env.info, heartbeat_timeout=timedelta(seconds=heartbeat_timeout_s),
                                            start_to_close_timeout=timedelta(minutes=10))
        self.beats: list[tuple[float, object]] = []
        self.env.on_heartbeat = lambda *d: self.beats.append((time.monotonic(), d[0] if d else None))
        self.started = self.ended = 0.0

    async def run(self, fn, *args):
        self.started = time.monotonic()
        try:
            return await self.env.run(fn, *args)
        finally:
            self.ended = time.monotonic()

    def longest_silence(self) -> tuple[float, object]:
        """Longest stretch of the attempt without a heartbeat, and the heartbeat that ended it."""
        marks = [(self.started, "start"), *self.beats, (self.ended, "end")]
        return max(((b[0] - a[0], b[1]) for a, b in zip(marks, marks[1:])), key=lambda g: g[0])

    def stages(self) -> list:
        return [d.get("stage") for _, d in self.beats if isinstance(d, dict)]
