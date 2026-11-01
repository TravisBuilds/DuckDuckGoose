"""GAP-69 / GAP-70 (ep05v2 r12, 28 Oct 2026).

GAP-69: GAP-68's 180-min assembly cap sat behind workflow.patched(); the Python SDK memoizes patched()
per run, so in ep05v2 (which had replayed an earlier assembly schedule without a marker) it stayed False
and activity 548 was scheduled with 1800 s. The cap must not depend on patched().

GAP-70: a cancelled activity attempt left its rife-ncnn child running (CPU contention with the retry,
TemporaryDirectory cleanup "Directory not empty"). media._run must kill its child on cancel."""

from __future__ import annotations

import asyncio
import inspect
import os
import time

from hfvg.activities import media
from hfvg.workflows import episode_v2 as ev2


def test_assembly_cap_is_not_gated_by_patched():
    src = inspect.getsource(ev2.EpisodeWorkflowV2._run_picture_assembly)
    assert 'patched("gap68-rife-assembly-timeout-v1")' not in src
    assert "assembly_cap = ASSEMBLY_RIFE_CAP_MIN" in src
    assert ev2.ASSEMBLY_RIFE_CAP_MIN >= 120


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie still answers kill(0); treat it as gone
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split()[2] != "Z"
    except FileNotFoundError:
        return False


def test_cancelled_run_kills_its_child(tmp_path):
    pidfile = tmp_path / "pid"

    async def main():
        t = asyncio.ensure_future(media._run(["sh", "-c", f"echo $$ > {pidfile}; exec sleep 30"], "sleep"))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if pidfile.exists() and pidfile.read_text().strip():
                break
        pid = int(pidfile.read_text())
        assert _alive(pid)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        return pid

    pid = asyncio.run(main())
    deadline = time.time() + 3
    while time.time() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)


def test_run_still_returns_output_and_raises_on_failure():
    assert asyncio.run(media._run(["sh", "-c", "echo ok"], "echo")).strip() == "ok"
    try:
        asyncio.run(media._run(["sh", "-c", "exit 3"], "fail"))
    except RuntimeError as e:
        assert "fail failed (3)" in str(e)
    else:
        raise AssertionError("non-zero exit must raise")
