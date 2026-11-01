"""GAP-73 (mirrors GAP-46): an owner grant lifted V21's recompose allowance to 5 and the recompose still
landed, but approving it was refused by the G3_CLIPS pre-check: the shot had used its 3 paid clip
retries (c1-c3) and recompose_decision passed no limit (ep05v2 r12 v10b, 21:08 CET). A recompose that
used a GRANTED allowance slot now gets exactly ONE extra G3_CLIPS slot for its single clip, one lifted
clip per grant. Clip retries, ungranted recomposes and other shots keep the setting. No network."""
from __future__ import annotations

import pytest
from temporalio.exceptions import ApplicationError

import hfvg.workflows.episode_v2 as mod
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


async def _ledger(tmp_path, monkeypatch, used: dict[str, int]):
    """ep99 ledger with MAX_PAID_RETRIES_PER_SHOT_PER_ROUND=3 and `used` committed G3_CLIPS retries."""
    from tests.test_recompose_round import _episode
    ledger, _ = await _episode(tmp_path, monkeypatch, limit="3")
    monkeypatch.setattr(mod.config, "MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", 3)
    for shot, k in used.items():
        for i in range(1, k + 1):
            await ledger.authorize_round_spend("ep99", "G3_CLIPS", shot, f"clip-ep99-{shot}-v1-c{i}",
                                               100_000, retry_key=f"c{i}")
    return ledger


def _activity(monkeypatch, calls):
    from hfvg.activities import studio_generation as sg

    async def execute_activity(name, args=None, **kw):
        if name != "check_round_budget_activity":   # e.g. a run-lock release from another test's workflow
            return None
        calls.append((name, list(args)))
        return await sg.check_round_budget_activity(*args)
    monkeypatch.setattr(mod.workflow, "execute_activity", execute_activity)
    import logging
    monkeypatch.setattr(mod.workflow, "logger", logging.getLogger("gap73-test"))


class _Handle:
    def __init__(self):
        self.signals = []

    async def signal(self, name, *args):
        self.signals.append((name, args))


def _park(wf, shot, n, version=8):
    wf.pending_recompose[shot] = {"workflow_id": f"ep99-shot-{shot}-recompose{n}", "n": n, "version": version,
                                  "still_ready": True, "decision": None, "reason": "r"}
    wf.recompose_handles[shot] = _Handle()
    wf.recompose_plans[shot] = {"params": {"model": "kling", "duration": 5}}
    wf.recompose_counts[shot] = n


def _wf(grants):
    wf = EpisodeWorkflowV2()
    wf.state.episode_id = "ep99"
    wf.recompose_grants = dict(grants)
    return wf


async def test_granted_recompose_single_clip_passes(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, {"A01": 3})
    calls = []
    _activity(monkeypatch, calls)
    wf = _wf({"A01": 1})
    _park(wf, "A01", 3)                       # rc3 used the granted slot (MAX_RECOMPOSES_PER_SHOT = 2)
    r = await wf.recompose_decision("A01", True, "Studio grant")
    assert r["decision"] == "approved"
    assert calls[-1][1][-1] == 4 and len(calls[-1][1]) == 9       # setting 3 + ONE
    assert wf.g3_clip_lifts == {"A01": [3]}
    assert wf.recompose_handles["A01"].signals == [("stills_approved", ())]
    wf.recompose_grants["A01"] = 2            # even with a 2nd grant, rc3's ONE clip never lifts again
    assert wf.g3_clip_lift_limit("A01", 3) is None
    assert wf.g3_clip_lift_limit("A01", 4) == 4


async def test_second_clip_on_the_same_grant_is_refused(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, {"A01": 3})
    calls = []
    _activity(monkeypatch, calls)
    wf = _wf({"A01": 1})
    _park(wf, "A01", 3)
    await wf.recompose_decision("A01", True, "Studio grant")
    with pytest.raises(ApplicationError, match="already decided"):      # the same recompose again
        await wf.recompose_decision("A01", True, "again")
    _park(wf, "A01", 4, version=9)            # another granted-slot recompose, but the 1 grant is spent
    assert wf.g3_clip_lift_limit("A01", 4) is None
    with pytest.raises(ApplicationError) as e:
        await wf.recompose_decision("A01", True, "second clip on grant 1")
    assert e.value.type == "RoundBudgetError" and "max 3 per shot" in str(e.value)
    assert len(calls[-1][1]) == 8                                       # no lift passed
    assert wf.g3_clip_lifts == {"A01": [3]}
    assert wf.g3_clip_lift_limit("A01", 3) is None                     # a lifted rc never lifts twice


async def test_ungranted_shot_at_three_retries_is_refused(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, {"A01": 3})
    calls = []
    _activity(monkeypatch, calls)
    wf = _wf({})
    _park(wf, "A01", 2)
    with pytest.raises(ApplicationError) as e:
        await wf.recompose_decision("A01", True, "no grant")
    assert e.value.type == "RoundBudgetError"
    assert len(calls[-1][1]) == 8 and wf.g3_clip_lifts == {}
    wf2 = _wf({"A01": 1})                     # granted shot, but an UNGRANTED slot (n <= 2): no lift
    _park(wf2, "A01", 2)
    with pytest.raises(ApplicationError):
        await wf2.recompose_decision("A01", True, "in-allowance slot")
    assert wf2.g3_clip_lifts == {}


async def test_other_shots_are_unaffected(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, {"A01": 3, "B02": 3, "C03": 1})
    calls = []
    _activity(monkeypatch, calls)
    wf = _wf({"A01": 1})
    _park(wf, "A01", 3)
    await wf.recompose_decision("A01", True, "Studio grant")
    _park(wf, "B02", 3)                       # B02 has no grant: still refused at 3
    with pytest.raises(ApplicationError) as e:
        await wf.recompose_decision("B02", True, "other shot")
    assert e.value.type == "RoundBudgetError" and len(calls[-1][1]) == 8
    _park(wf, "C03", 1)                       # under the limit: passes, old activity args unchanged
    await wf.recompose_decision("C03", True, "fine")
    assert len(calls[-1][1]) == 8 and wf.g3_clip_lifts == {"A01": [3]}


async def test_precheck_activity_retry_limit_lifts_never_lowers(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import check_round_budget_activity
    await _ledger(tmp_path, monkeypatch, {"A01": 3, "C03": 1})
    with pytest.raises(ApplicationError):
        await check_round_budget_activity("ep99", "G3_CLIPS", "A01", "clip-ep99-A01-v8", "kling", 5.0, "rc3")
    r = await check_round_budget_activity("ep99", "G3_CLIPS", "A01", "clip-ep99-A01-v8", "kling", 5.0, "rc3",
                                          None, 4)
    assert r["authorized"] and r["estimate_usd_micros"] == 560_000
    r = await check_round_budget_activity("ep99", "G3_CLIPS", "C03", "clip-ep99-C03-v2", "kling", 5.0, "rc1",
                                          None, 1)            # a lower limit does not lower 3
    assert r["authorized"]
