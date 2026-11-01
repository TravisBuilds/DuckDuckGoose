"""GAP-68 (ep05v2 r12, 28 Oct 2026): the v10 mute edit RIFE-retimes ~800 frames at ~4-5 s/frame on
the CPU box (over an hour). assemble_mute_edit was scheduled with a 30-min start_to_close, so each
attempt was killed mid-shot. The cap is raised (behind a replay patch); heartbeats still catch hangs."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from hfvg.workflows import episode_v2 as ev2
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


class _Stop(BaseException):
    pass


class _S:
    stage = None
    assembly_acceptance = None
    output_paths: dict = {}


def _wf() -> EpisodeWorkflowV2:
    wf = object.__new__(EpisodeWorkflowV2)
    wf.state = _S()
    wf.state.output_paths = {}
    wf.assembly_version = 0
    wf._assembly_signature = lambda: ["sig"]
    wf._assembly_plan = lambda: ([{"shot_id": "V05"}], {"V05": "outputs/V05/clip.mp4"}, [], [])
    return wf


def _scheduled(monkeypatch, patched: bool) -> dict:
    seen: dict = {}

    async def fake_execute_activity(fn, *a, **kw):
        seen["fn"] = fn
        seen.update(kw)
        raise _Stop()

    monkeypatch.setattr(ev2.workflow, "execute_activity", fake_execute_activity)
    monkeypatch.setattr(ev2.workflow, "patched", lambda pid: patched and pid == "gap68-rife-assembly-timeout-v1")
    monkeypatch.setattr(ev2.workflow, "logger", type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                                              "warning": staticmethod(lambda *a, **k: None)})())
    with pytest.raises(_Stop):
        asyncio.run(_wf()._run_picture_assembly("ep05v2"))
    return seen


def test_patched_assembly_cap_covers_an_hour_plus_of_rife(monkeypatch):
    seen = _scheduled(monkeypatch, True)
    assert seen["fn"] is ev2.assemble_mute_edit
    # ~850 RIFE frames x 5 s = ~71 min must fit inside one attempt
    assert seen["start_to_close_timeout"] >= timedelta(minutes=120)
    assert seen["start_to_close_timeout"] == timedelta(minutes=ev2.ASSEMBLY_RIFE_CAP_MIN)


def test_heartbeat_timeout_still_catches_a_hung_attempt(monkeypatch):
    seen = _scheduled(monkeypatch, True)
    assert seen["heartbeat_timeout"] == timedelta(minutes=2)
    assert seen["retry_policy"].maximum_attempts == 3


def test_memoized_false_patch_no_longer_drops_the_cap(monkeypatch):
    # GAP-69: patched() memoized False (a run that replayed an earlier schedule without a marker)
    # used to give 30 min for every later rebuild; the cap no longer depends on patched().
    seen = _scheduled(monkeypatch, False)
    assert seen["start_to_close_timeout"] == timedelta(minutes=ev2.ASSEMBLY_RIFE_CAP_MIN)
