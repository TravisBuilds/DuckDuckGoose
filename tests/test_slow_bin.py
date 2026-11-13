"""tests.slow_bin.alive: an exited process is gone, whatever /proc shows while it is being reaped.

CI (redgreen shard 2 at 2cf5bf1): the restored run of mutation 444
(test_cancel_kills_the_child_and_its_process_group) failed in a fraction of a second without an
assertion. The test's grandchild had just been killed and init was reaping it while alive() looked:
/proc/<pid>/stat then answers open() or read() with ESRCH (a ProcessLookupError alive() let escape),
or shows state X (being reaped), which alive() counted as running. A killed grandchild polled until
it is gone shows R > Z > X > ESRCH/ENOENT; looping test 444's sequence hit one of the two in 2-4% of
runs. Each case below is the kernel's answer at one point of that sequence."""

from __future__ import annotations

import errno
import io
import os
import subprocess
import time

import pytest

from tests import slow_bin
from tests.slow_bin import alive

real_open = open


def _stat(pid: int) -> str:
    with real_open(f"/proc/{pid}/stat") as f:
        return f.read()


@pytest.fixture
def zombie():
    """A child that has exited and is not reaped yet: kill(pid, 0) still answers for it."""
    p = subprocess.Popen(["true"])
    end = time.monotonic() + 5
    while _stat(p.pid).rsplit(")", 1)[1].split()[0] != "Z":
        assert time.monotonic() < end, f"pid {p.pid} never became a zombie"
        time.sleep(0.01)
    yield p
    if p.returncode is None:
        p.wait()


def _alive(pid: int) -> bool:
    try:
        return alive(pid)
    except OSError as e:
        raise AssertionError(f"alive({pid}) raised {e!r} for a process reaped while it looked") from e


def test_alive_tells_a_running_process_from_an_exited_one(zombie):
    p = subprocess.Popen(["sleep", "30"])
    try:
        assert alive(p.pid), "a running child is alive"
    finally:
        p.kill()
        p.wait()
    assert not alive(p.pid), "a reaped child is gone"
    assert not alive(zombie.pid), "a zombie has exited"


def test_alive_is_false_for_a_process_reaped_between_open_and_read(zombie, monkeypatch):
    def open_then_reap(path, *a, **k):
        f = real_open(path, *a, **k)
        if path == f"/proc/{zombie.pid}/stat":  # its reaper gets there between open() and read()
            _, status = os.waitpid(zombie.pid, 0)
            zombie.returncode = os.waitstatus_to_exitcode(status)
        return f

    monkeypatch.setattr(slow_bin, "open", open_then_reap, raising=False)
    assert _alive(zombie.pid) is False


def test_alive_is_false_when_open_races_the_reap(zombie, monkeypatch):
    def open_esrch(path, *a, **k):
        if path == f"/proc/{zombie.pid}/stat":  # the lookup found the pid, then the task went away
            raise ProcessLookupError(errno.ESRCH, os.strerror(errno.ESRCH), path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(slow_bin, "open", open_esrch, raising=False)
    assert _alive(zombie.pid) is False


def test_alive_is_false_for_a_process_being_reaped(zombie, monkeypatch):
    def stat_while_reaped(path, *a, **k):
        if path == f"/proc/{zombie.pid}/stat":
            head, tail = _stat(zombie.pid).rsplit(")", 1)
            return io.StringIO(f"{head}) X {tail.split(None, 1)[1]}")  # EXIT_DEAD
        return real_open(path, *a, **k)

    monkeypatch.setattr(slow_bin, "open", stat_while_reaped, raising=False)
    assert _alive(zombie.pid) is False
