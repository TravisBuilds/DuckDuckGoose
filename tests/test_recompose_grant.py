"""GAP-39 (ep05v2 G3 round 3): Studio ordered a third V16 still after both recomposes were used, but
MAX_RECOMPOSES_PER_SHOT had no reviewer override. grant_recompose(shot, reason) lifts one shot's cap
by exactly one, only at G_FINAL_CUT, only once the allowance is used up, at most
MAX_RECOMPOSE_GRANTS_PER_SHOT times; the default cap is unchanged for every other shot."""
from __future__ import annotations

import pytest

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


def _wf(used=2):
    wf = EpisodeWorkflowV2()
    wf.approved_g2_scene_stills = True
    wf.passed_gates = ["G1_SHOT_PLAN", "G2_SCENE_STILLS"] if isinstance(wf.passed_gates, list) else {"G1_SHOT_PLAN", "G2_SCENE_STILLS"}
    wf.state.current_gate = "G_FINAL_CUT"
    wf.launched_shot_plans = {"V16": {"version": 3}, "V10": {"version": 4}}
    wf.recompose_counts = {"V16": used, "V10": 2}
    return wf


def _recompose(wf, shot="V16"):
    return wf._recompose_problem(shot, "Studio g3r3", "a new still prompt", ["/refs/duck.png"])


def test_default_cap_unchanged():
    assert EpisodeWorkflowV2.MAX_RECOMPOSES_PER_SHOT == 2
    p = _recompose(_wf())
    assert p and "max recomposes per shot (2) reached" in p and "grant_recompose" in p


def test_grant_allows_exactly_one_more_for_that_shot_only():
    wf = _wf()
    assert wf._grant_recompose_problem("V16", "Studio: lanterns on the window posts") is None
    wf.recompose_grants["V16"] = 1           # what grant_recompose records
    assert wf.recompose_allowance("V16") == 3
    assert _recompose(wf) is None             # third recompose allowed
    assert "max recomposes" in (_recompose(wf, "V10") or "")  # other shots keep the default cap
    wf.recompose_counts["V16"] = 3            # the granted one was used
    assert "max recomposes per shot (3) reached" in (_recompose(wf) or "")


def test_grant_refused_when_not_needed_or_without_reason_or_off_gate():
    wf = _wf(used=1)
    assert "no grant needed" in wf._grant_recompose_problem("V16", "why")
    wf = _wf()
    assert "reason" in wf._grant_recompose_problem("V16", "  ")
    assert "unknown shot" in wf._grant_recompose_problem("V99", "why")
    wf.state.current_gate = "G2_SCENE_STILLS"
    assert "G_FINAL_CUT" in wf._grant_recompose_problem("V16", "why")
    wf = _wf(); wf.approved_g_final_cut = True
    assert "already approved" in wf._grant_recompose_problem("V16", "why")


def test_grants_are_bounded():
    wf = _wf()
    wf.recompose_grants["V16"] = EpisodeWorkflowV2.MAX_RECOMPOSE_GRANTS_PER_SHOT
    wf.recompose_counts["V16"] = wf.recompose_allowance("V16")
    assert "max recompose grants" in wf._grant_recompose_problem("V16", "why")


async def test_grant_update_records_and_returns_the_row(monkeypatch):
    wf = _wf()
    import hfvg.workflows.episode_v2 as mod

    class _Log:
        def info(self, *a, **k):
            pass
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    row = await wf.grant_recompose("V16", "Studio g3r3: V16 window-post glows")
    assert row == {"shot_id": "V16", "reason": "Studio g3r3: V16 window-post glows", "grants": 1,
                   "allowance": 3, "used": 2}
    assert wf.recompose_grant_log == [row] and wf.recompose_grants == {"V16": 1}
    with pytest.raises(Exception):
        await wf.grant_recompose("V16", "again")   # allowance not used up yet


# GAP-43 (ep05v2 r7): Studio granted V01 a new plate after both grants were used; the cap stays, an
# explicit Studio cap_override_note (audited in the grant row) lifts it by one per call.

def test_grant_cap_override_requires_explicit_note():
    wf = _wf()
    wf.recompose_grants["V16"] = EpisodeWorkflowV2.MAX_RECOMPOSE_GRANTS_PER_SHOT
    wf.recompose_counts["V16"] = wf.recompose_allowance("V16")
    assert "cap_override_note" in wf._grant_recompose_problem("V16", "why", "")
    assert "cap_override_note" in wf._grant_recompose_problem("V16", "why", "  short  ")
    assert wf._grant_recompose_problem("V16", "why", "Studio 23:56 CET: V01 new plate") is None
    # the override never skips the other rules
    assert "reason" in wf._grant_recompose_problem("V16", " ", "Studio 23:56 CET: V01 new plate")
    wf.recompose_counts["V16"] -= 1
    assert "no grant needed" in wf._grant_recompose_problem("V16", "why", "Studio 23:56 CET: V01 new plate")


async def test_override_grant_row_records_the_note(monkeypatch):
    import hfvg.workflows.episode_v2 as mod

    class _Log:
        def info(self, *a, **k):
            pass
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    wf = _wf()
    row = await wf.grant_recompose("V16", "normal grant")
    assert "cap_override" not in row
    wf.recompose_grants["V16"] = EpisodeWorkflowV2.MAX_RECOMPOSE_GRANTS_PER_SHOT
    wf.recompose_counts["V16"] = wf.recompose_allowance("V16")
    with pytest.raises(Exception):
        await wf.grant_recompose("V16", "no note")
    row = await wf.grant_recompose("V16", "V01 new plate", "Studio 23:56 CET explicit grant")
    assert row["cap_override"] == "Studio 23:56 CET explicit grant"
    assert row["grants"] == EpisodeWorkflowV2.MAX_RECOMPOSE_GRANTS_PER_SHOT + 1
    assert row["allowance"] == EpisodeWorkflowV2.MAX_RECOMPOSES_PER_SHOT + row["grants"]


def test_grant_route_passes_note_only_when_given():
    import inspect
    import api.main as m
    src = inspect.getsource(m.grant_recompose)
    assert "([note] if note else [])" in src and "grant_recompose_override" in src
