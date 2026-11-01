"""GAP-74 (ep05v2 r12 v10b): restore-outcome must back out to what the LATEST approved recompose replaced.

GAP-49 saved the replaced outcome only when it was completed and otherwise kept an OLDER save, so on V21
(rc4 replaced a completed clip_v1003; rc4's take clip_v2007 stayed needs_review and the edit used the
edit_plan reuse pin; rc5 failed QC) the restore jumped back two compositions to clip_v1003 and the edit
refused (EditPlanMismatch). After the cutover the restore uses the latest replaced outcome (any status)
and can correct such a stale restore once; before the cutover the recorded GAP-49 behaviour replays as is.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import hfvg.workflows.episode_v2 as mod
from tests.test_r8_reuse_route_grant import _land_recompose, _wf

A = {"status": "completed", "clip_url": "u-a", "clip_path": "data/V03/clip_a.mp4"}        # accepted
C = {"status": "needs_review", "clip_url": "u-c", "clip_path": "data/V03/clip_c.mp4"}     # rc1 take, unaccepted
D = {"status": "needs_review", "still_url": "s-d", "clip_url": "u-d", "clip_path": "data/V03/clip_d.mp4"}


def _clock(monkeypatch, when):
    monkeypatch.setattr(mod.workflow, "in_workflow", lambda: True)
    monkeypatch.setattr(mod.workflow, "now", lambda: when)


BEFORE = datetime(2026, 10, 28, 20, 45, 41, tzinfo=timezone.utc)
AFTER = datetime(2026, 10, 28, 21, 0, 0, tzinfo=timezone.utc)


async def _two_recomposes(monkeypatch):
    wf = _wf()
    wf.shot_outcomes = {"V03": dict(A)}
    wf.clip_review_acceptances = {"V03": {"reason": "A accepted"}}
    wf.assembly_sig = []
    await _land_recompose(wf, monkeypatch, "V03", {"status": "needs_review", "still_url": "s-b",
                                                    "clip_url": "u-b", "clip_path": "data/V03/clip_b.mp4"})
    wf.shot_outcomes["V03"] = dict(C)           # rc1's clip retry landed, never accepted
    wf.clip_takes["V03"] = {1: {"clip_url": "u-c", "clip_path": "data/V03/clip_c.mp4"}}
    await _land_recompose(wf, monkeypatch, "V03", dict(D))
    return wf


async def test_restore_returns_what_the_latest_recompose_replaced(monkeypatch):
    wf = await _two_recomposes(monkeypatch)
    assert wf.pre_recompose_outcomes["V03"]["outcome"] == A       # GAP-49 save unchanged (stale)
    assert wf._restore_outcome_problem("V03", "rc2 clip FAIL") is None
    out = await wf.restore_pre_recompose_outcome("V03", "rc2 clip FAIL")
    assert wf.shot_outcomes["V03"] == C and out["restored_clip"] == "data/V03/clip_c.mp4"
    assert out["restored_status"] == "needs_review" and "corrects_stale_restore" not in out
    assert wf.clip_takes["V03"] == {1: {"clip_url": "u-c", "clip_path": "data/V03/clip_c.mp4"}}
    _, paths, _, excluded = wf._assembly_plan()
    assert "V03" not in paths and {"shot_id": "V03", "status": "needs_review"} in excluded  # reuse pin applies
    assert "no accepted pre-recompose" in wf._restore_outcome_problem("V03", "again")
    assert wf.get_state()["recompose"]["replaced"]["V03"]["restored"] is True


async def test_stale_gap49_restore_replays_then_is_corrected_once(monkeypatch):
    wf = await _two_recomposes(monkeypatch)
    _clock(monkeypatch, BEFORE)                  # the recorded restore: GAP-49 behaviour, replay-identical
    old = await wf.restore_pre_recompose_outcome("V03", "rc2 clip FAIL")
    assert old["restored_clip"] == "data/V03/clip_a.mp4" and wf.shot_outcomes["V03"] == A
    assert "restored_status" not in old and wf.clip_review_acceptances["V03"] == {"reason": "A accepted"}
    _clock(monkeypatch, AFTER)
    assert wf._restore_outcome_problem("V03", "correct stale restore") is None   # completed, but stale
    out = await wf.restore_pre_recompose_outcome("V03", "correct stale restore")
    assert wf.shot_outcomes["V03"] == C and out["corrects_stale_restore"] == old["replaced_by"]
    assert "V03" not in wf.clip_review_acceptances
    assert "V03" not in wf._assembly_plan()[1]
    assert "no accepted pre-recompose" in wf._restore_outcome_problem("V03", "again")


async def test_completed_outcome_is_still_refused_unless_stale(monkeypatch):
    wf = await _two_recomposes(monkeypatch)
    wf.shot_outcomes["V03"] = {**D, "status": "completed"}        # rc2's clip accepted
    assert "current outcome is accepted" in wf._restore_outcome_problem("V03", "r")
    # a stale restore whose clip was since replaced (e.g. another take accepted) is not corrected
    wf2 = await _two_recomposes(monkeypatch)
    _clock(monkeypatch, BEFORE)
    await wf2.restore_pre_recompose_outcome("V03", "rc2 clip FAIL")
    _clock(monkeypatch, AFTER)
    wf2.shot_outcomes["V03"] = {"status": "completed", "clip_url": "u-x", "clip_path": "data/V03/clip_x.mp4"}
    assert "current outcome is accepted" in wf2._restore_outcome_problem("V03", "r")


async def test_pre_cutover_refusals_unchanged(monkeypatch):
    wf = await _two_recomposes(monkeypatch)
    _clock(monkeypatch, BEFORE)
    wf.pre_recompose_outcomes.clear()
    assert "no accepted pre-recompose" in wf._restore_outcome_problem("V03", "r")   # GAP-49 view only
    _clock(monkeypatch, AFTER)
    assert wf._restore_outcome_problem("V03", "r") is None


async def test_unapproved_or_first_recompose_records(monkeypatch):
    wf = _wf()
    await _land_recompose(wf, monkeypatch, "V03", dict(D))      # no previous outcome: nothing to restore
    assert "V03" not in wf.recompose_replaced
    assert "no accepted pre-recompose" in wf._restore_outcome_problem("V03", "r")
    wf2 = _wf()
    wf2.shot_outcomes = {"V03": dict(C)}
    await _land_recompose(wf2, monkeypatch, "V03", dict(D))
    assert wf2.recompose_replaced["V03"]["outcome"] == C and "V03" not in wf2.pre_recompose_outcomes
    assert wf2.recompose_replaced["V03"]["replaced_by"]


def test_cutover_is_after_the_recorded_v21_restore():
    # the stale V21 restore ran 2026-10-28 21:45:41 CET (20:45:41Z): it must replay with GAP-49 behaviour
    assert mod.EpisodeWorkflowV2.GAP74_CUTOVER > BEFORE
