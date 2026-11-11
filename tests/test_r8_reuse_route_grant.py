"""ep05v2 r8 (2026-10-28) Studio redo list v6.

GAP-44: V06 is an edit_plan reuse slot (locked clip from another episode), not a workflow shot, so
        recompose refused it ("unknown shot"). adopt_reuse_slot registers a plan for the slot (no spend);
        the edit keeps the reuse clip until a generated clip for the slot completes, then the generated
        clip wins (recorded as reuse_superseded).
GAP-45: a recompose could not change the clip route: V11 must render Seedance at 720p (the tier default is
        1080p, 2.3x the price) and V21 moves to Kling. clip_route {model, seedance_resolution} is
        validated, recorded and carried into routing, the estimate, the reservation and the clip QC dims.
GAP-46: an owner grant lifted V01's recompose allowance to 5, but the G3_RECOMPOSE round still capped
        paid retries per shot at MAX_PAID_RETRIES_PER_SHOT_PER_ROUND (3), so the granted take was refused
        inside the child. The granted allowance is now the per-shot limit for that shot's recompose
        stills (it only lifts, never lowers the setting).
GAP-47: the edit upscaled with ffmpeg's default bicubic; a 720p Seedance clip is upscaled with lanczos
        (EDIT_SCALE_FLAGS) and the edit format version bumped so cached edits rebuild.
No network, no spend."""
from __future__ import annotations

import json

import pytest

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


class _Log:
    def info(self, *a, **k):
        pass

    warning = info


def _wf():
    wf = EpisodeWorkflowV2()
    wf.approved_g2_scene_stills = True
    wf.passed_gates = ["G1_SHOT_PLAN", "G2_SCENE_STILLS"]
    wf.state.current_gate = "G_FINAL_CUT"
    wf.parsed_beatmap = {"shots": []}
    wf.launched_shot_plans = {
        "V03": {"shot_id": "V03", "version": 1, "prompt": "lobby", "refs": ["/r/lobby.png"],
                "params": {"model": "kling", "duration": 4}, "shot_meta": {"count": 3}, "workflow_id": "x"},
        "V11": {"shot_id": "V11", "version": 1, "prompt": "pool", "refs": ["/r/pool.png"],
                "params": {"model": "seedance", "duration": 4}, "shot_meta": {"count": 0}},
        "V21": {"shot_id": "V21", "version": 1, "prompt": "pool", "refs": ["/r/pool.png"],
                "params": {"model": "seedance", "duration": 5, "seedance_resolution": "1080p"},
                "shot_meta": {"count": 1}},
    }
    wf.assembly_shots = [dict(v) for v in wf.launched_shot_plans.values()]
    return wf


# ---------------------------------------------------------------- GAP-45 clip_route
def test_clip_route_validation():
    p = EpisodeWorkflowV2.clip_route_problem
    assert p(None) is None
    assert p({"seedance_resolution": "720p"}, {"model": "seedance"}) is None
    assert p({"model": "kling"}, {"model": "seedance"}) is None
    assert "non-empty" in p({})
    assert "may only set" in p({"tier": "draft"})
    assert "must be one of" in p({"model": "veo"})
    assert "must be one of" in p({"seedance_resolution": "900p"}, {"model": "seedance"})
    assert "needs a seedance clip" in p({"seedance_resolution": "720p"}, {"model": "kling"})
    assert "needs a seedance clip" in p({"model": "kling", "seedance_resolution": "720p"}, {"model": "seedance"})


def test_routed_params():
    rp = EpisodeWorkflowV2.routed_params
    assert rp({"model": "seedance", "duration": 4}, {"seedance_resolution": "720p"}) == {
        "model": "seedance", "duration": 4, "seedance_resolution": "720p"}
    assert rp({"model": "seedance", "duration": 5, "seedance_resolution": "1080p"}, {"model": "kling"}) == {
        "model": "kling", "duration": 5}
    assert rp({"model": "kling"}, None) == {"model": "kling"}


def _start_child(monkeypatch, captured):
    import hfvg.workflows.episode_v2 as mod

    class _Info:
        workflow_id = "ep05v2"
        task_queue = "q"

    class _Handle:
        def __await__(self):
            async def never():
                import asyncio
                await asyncio.sleep(3600)
            return never().__await__()

    async def start(name, args, id, task_queue):
        captured.append(args[1])
        return _Handle()
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    monkeypatch.setattr(mod.workflow, "info", lambda: _Info())
    monkeypatch.setattr(mod.workflow, "start_child_workflow", start)
    # r9 GAP-54: GAP-48 sits behind workflow.patched; a fresh execution sees the patch
    monkeypatch.setattr(mod.workflow, "patched", lambda _id: True)


async def test_recompose_carries_the_clip_route(monkeypatch):
    wf, plans = _wf(), []
    _start_child(monkeypatch, plans)
    await wf.recompose_shot("V11", "Studio r8", "new pool still", ["/r/pool.png"], ["pool"], None, None,
                            {"seedance_resolution": "720p"})
    assert plans[-1]["params"]["seedance_resolution"] == "720p" and plans[-1]["params"]["model"] == "seedance"
    assert plans[-1]["recompose"]["clip_route"] == {"seedance_resolution": "720p"}
    wf.pending_recompose.clear()
    await wf.recompose_shot("V21", "Studio r8", "new pool still", ["/r/pool.png"], ["pool"], None,
                            {"count": 1}, {"model": "kling"})
    assert plans[-1]["params"] == {"model": "kling", "duration": 5}
    assert "allowance" not in plans[-1]["recompose"]          # no grant: no limit change
    for t in wf.background_tasks:
        t.cancel()


def test_recompose_refuses_a_bad_route():
    wf = _wf()
    assert "needs a seedance clip" in wf._recompose_problem("V03", "r", "p", ["/r/a.png"], None,
                                                            {"seedance_resolution": "720p"})


def test_plan_route_resolution():
    from hfvg.routing import RoutingError, plan_route
    shot = {"model": "seedance", "tier": "final", "duration": 4, "count": 0}
    assert plan_route(shot).resolution == "1080p"
    assert plan_route(shot, resolution="720p").resolution == "720p"
    with pytest.raises(RoutingError):
        plan_route(shot, resolution="900p")
    with pytest.raises(RoutingError):
        plan_route({**shot, "model": "kling"}, resolution="720p")


def test_estimates_and_qc_dims_follow_the_resolution():
    from hfvg.estimates import clip_call_usd_micros
    from hfvg.pricing import clip_estimate_usd_micros
    from hfvg.qc.clip_expect import expected_clip_dims
    full = clip_estimate_usd_micros("seedance", "final", 4)
    r720 = clip_estimate_usd_micros("seedance", "final", 4, "720p")
    assert r720 < full
    assert clip_call_usd_micros("seedance", "final", 4, "720p") == r720
    assert clip_call_usd_micros("kling", "final", 4, "720p") == clip_estimate_usd_micros("kling", "final", 4)
    assert expected_clip_dims({"model": "seedance", "seedance_resolution": "720p"})["resolution"] == "720x1280"
    assert expected_clip_dims({"model": "seedance"})["resolution"] == "1080x1920"
    assert expected_clip_dims({"model": "kling", "seedance_resolution": "720p"})["resolution"] == \
        expected_clip_dims({"model": "kling"})["resolution"]


def test_shot_workflow_passes_the_resolution_only_for_seedance():
    import inspect

    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.workflows import shot
    assert list(inspect.signature(submit_clip_job_enforced).parameters)[-1] == "seedance_resolution"
    src = inspect.getsource(shot)
    assert 'params["seedance_resolution"]' in src and '== "seedance"' in src


# ---------------------------------------------------------------- GAP-46 granted allowance = round limit
async def test_recompose_plan_records_the_granted_allowance(monkeypatch):
    wf, plans = _wf(), []
    _start_child(monkeypatch, plans)
    wf.recompose_grants = {"V03": 3}
    wf.recompose_counts = {"V03": 4}
    await wf.recompose_shot("V03", "Studio r8", "empty plate", ["/r/a.png"], ["aerial"])
    assert plans[-1]["recompose"]["allowance"] == 5
    for t in wf.background_tasks:
        t.cancel()


async def test_ledger_retry_limit_lifts_never_lowers(tmp_path, monkeypatch):
    from hfvg.budget import RoundBudgetError
    from tests.test_recompose_round import _episode
    ledger, _ = await _episode(tmp_path, monkeypatch, limit="2")   # the G2 setup holds 2 retries
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "1")
    await ledger.authorize_round_spend("ep99", "G3_RECOMPOSE", "A01", "s-rc1", 60_000, retry_key="rc1")
    with pytest.raises(RoundBudgetError):
        await ledger.authorize_round_spend("ep99", "G3_RECOMPOSE", "A01", "s-rc2", 60_000, retry_key="rc2")
    r = await ledger.authorize_round_spend("ep99", "G3_RECOMPOSE", "A01", "s-rc2", 60_000, retry_key="rc2",
                                           retry_limit=2)
    assert r["authorized"]
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "5")
    r = await ledger.authorize_round_spend("ep99", "G3_RECOMPOSE", "A01", "s-rc3", 60_000, retry_key="rc3",
                                           retry_limit=1)           # a lower limit does not lower 5
    assert r["authorized"]


async def test_still_submit_uses_the_review_round_limit(tmp_path, monkeypatch):
    import respx
    from temporalio.exceptions import ApplicationError

    from tests.test_recompose_round import _episode, _routes, _submit
    ledger, ref = await _episode(tmp_path, monkeypatch, limit="2")
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "1")
    with respx.mock(assert_all_called=False) as router:
        _routes(router)
        await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc1"}, attempt=4)
        with pytest.raises(ApplicationError) as e:
            await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc2"}, attempt=5)
        assert e.value.type == "RoundBudgetError"
        await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc2", "limit": 2}, attempt=5)
        with pytest.raises(ApplicationError) as e:
            await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc3", "limit": True}, attempt=6)
        assert "invalid review_round limit" in str(e.value)


def test_shot_workflow_sends_the_allowance_as_limit():
    import inspect

    from hfvg.workflows import shot
    src = inspect.getsource(shot)
    assert '"limit": int(shot_plan["recompose"]["allowance"])' in src


# ---------------------------------------------------------------- GAP-44 adopt a reuse slot
_FIELDS = {"prompt": "CharA sniffs an item basket", "shot_meta": {"count": 2, "characters": ["H", "M"]},
           "params": {"model": "kling", "duration": 4}, "trim": {"in": 0.0, "out": 4.0},
           "ref_keys": ["char1", "char2", "lobby"]}


async def test_adopt_registers_a_recomposable_plan(monkeypatch):
    import hfvg.workflows.episode_v2 as mod
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    wf = _wf()
    assert "unknown shot" in wf._recompose_problem("V06", "r", "p", ["/r/a.png"])
    row = await wf.adopt_reuse_slot("V06", "Studio r8: V06 scale", "V03", dict(_FIELDS))
    assert row == {"shot_id": "V06", "template": "V03", "reason": "Studio r8: V06 scale"}
    plan = wf.launched_shot_plans["V06"]
    assert plan["shot_id"] == "V06" and plan["version"] == 0 and plan["prompt"] == _FIELDS["prompt"]
    assert plan["shot_meta"] == _FIELDS["shot_meta"] and "workflow_id" not in plan
    assert plan["adopted_reuse"]["template"] == "V03"
    assert [s["shot_id"] for s in wf.assembly_shots].count("V06") == 1
    assert wf._recompose_problem("V06", "r", "p", ["/r/a.png"]) is None
    rp = wf.retry_prompt_plan("V06")
    assert rp["found"] and rp["shot_meta"] == _FIELDS["shot_meta"] and rp["clip_model"] == "kling"
    assert wf.get_state()["recompose"]["adopted_reuse"] == [row]


def test_adopt_refusals():
    wf = _wf()
    p = wf._adopt_reuse_problem
    assert "already has a plan" in p("V03", "r", "V11", dict(_FIELDS))
    assert "unknown template" in p("V06", "r", "V99", dict(_FIELDS))
    assert "reason" in p("V06", " ", "V03", dict(_FIELDS))
    assert "may only set" in p("V06", "r", "V03", {**_FIELDS, "version": 9})
    assert "fields.prompt" in p("V06", "r", "V03", {k: v for k, v in _FIELDS.items() if k != "prompt"})
    wf.state.current_gate = "G3_CLIPS"
    assert "G_FINAL_CUT" in p("V06", "r", "V03", dict(_FIELDS))


def test_generated_clip_supersedes_the_reuse_clip():
    from hfvg.activities.media import _apply_edit_plan
    plan = {"order": ["V05", "V06", "V07"],
            "reuse": {"V06": {"clip": "/old/F04.mp4", "sha256": "abc", "trim_in": 0.0, "trim_out": 4.0}}}
    gen = [{"shot_id": "V05", "clip_path": "/a.mp4", "trim_in": 0, "trim_out": 4},
           {"shot_id": "V07", "clip_path": "/c.mp4", "trim_in": 0, "trim_out": 4}]
    merged, _, notes = _apply_edit_plan(gen, plan)
    assert merged[1]["reused"] and "reuse_superseded" not in notes
    gen.insert(1, {"shot_id": "V06", "clip_path": "/new/V06.mp4", "trim_in": 0, "trim_out": 4})
    merged, _, notes = _apply_edit_plan(gen, plan)
    assert merged[1]["clip_path"] == "/new/V06.mp4" and not merged[1].get("reused")
    assert notes["reuse_superseded"] == ["V06"]


def test_api_adopt_fields_from_the_package_entry(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from api.main import _adopt_reuse_fields
    ep = tmp_path / "ep99"
    (ep / "refs").mkdir(parents=True)
    (ep / "refs" / "char1.png").write_bytes(b"x")
    (ep / "edit_plan.json").write_text(json.dumps({"order": ["V06"], "reuse": {
        "V06": {"clip": "/c.mp4", "sha256": "s", "trim_in": 0.0, "trim_out": 4.0}}}))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    entry = {"id": "V06", "count": 2, "characters": ["H", "M"], "duck_present": False, "model": "kling",
             "wet": False, "water": False, "exterior": False, "end_frame": False,
             "duration": 4, "ref_keys": ["char1", "char2"], "prompt": "CharA sniffs a basket",
             "reuse": {"clip": "/c.mp4"}, "pre_approved_still": {"path": "/s.jpg"}}
    f = _adopt_reuse_fields("ep99", "V06", entry)
    assert f["prompt"] == "CharA sniffs a basket" and f["shot_meta"]["count"] == 2
    assert f["params"]["model"] == "kling" and f["trim"] == {"in": 0.0, "out": 4.0}
    assert f["refs"] == [str((ep / "refs" / "char1.png").resolve())]   # only refs on disk
    with pytest.raises(HTTPException):
        _adopt_reuse_fields("ep99", "V05", {**entry, "id": "V05"})       # not a reuse slot
    with pytest.raises(HTTPException):
        _adopt_reuse_fields("ep99", "V06", {**entry, "id": "V07"})       # id mismatch
    with pytest.raises(HTTPException):
        _adopt_reuse_fields("ep99", "V06", {**entry, "duration": 5})     # slot length changes


# ---------------------------------------------------------------- GAP-47 lanczos edit scale
def test_edit_scale_is_lanczos_and_format_bumped():
    import inspect

    from hfvg.activities import media
    assert media.EDIT_SCALE_FLAGS == "lanczos" and media.EDIT_FORMAT_VERSION == 3
    assert ":flags={EDIT_SCALE_FLAGS}" in inspect.getsource(media.assemble_mute_edit)


# ---------------------------------------------------------------- GAP-48 recompose numbers past retry stills
async def test_recompose_version_skips_g2_retry_still_versions(monkeypatch):
    """V04 had G2 still children v1..v4 (retries superseded, v1 selected); the recompose took v2 and then v3
    and overwrote the retries' still files. It must number past every still child version."""
    wf, plans = _wf(), []
    _start_child(monkeypatch, plans)
    wf.still_version_children = {"V03": [{"version": v, "workflow_id": f"ep-shot-V03-r{v}"} for v in (1, 2, 3, 4)]}
    await wf.recompose_shot("V03", "Studio r8", "new lobby still", ["/r/lobby.png"], ["lobby"])
    assert plans[-1]["version"] == 5 and plans[-1]["attempt"] == 5
    wf.pending_recompose.clear()
    await wf.recompose_shot("V03", "Studio r8", "new lobby still", ["/r/lobby.png"], ["lobby"])
    assert plans[-1]["version"] == 6
    wf.pending_recompose.clear()
    await wf.recompose_shot("V11", "Studio r8", "pool", ["/r/pool.png"], ["pool"])   # no still children
    assert plans[-1]["version"] == 2
    for t in wf.background_tasks:
        t.cancel()


# ---------------------------------------------------------------- GAP-49 back out a failed recompose clip
async def _land_recompose(wf, monkeypatch, shot_id, result):
    """Run recompose_shot + approve and let the tracker see `result` (the child's return value)."""
    import asyncio
    import hfvg.workflows.episode_v2 as mod

    class _Info:
        workflow_id = "ep05v2"
        task_queue = "q"
    fut = asyncio.get_event_loop().create_future()

    class _Handle:
        def __await__(self):
            return fut.__await__()

    async def start(name, args, id, task_queue):
        return _Handle()
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    monkeypatch.setattr(mod.workflow, "info", lambda: _Info())
    monkeypatch.setattr(mod.workflow, "start_child_workflow", start)
    # r9 GAP-54: GAP-48 sits behind workflow.patched; a fresh execution sees the patch
    monkeypatch.setattr(mod.workflow, "patched", lambda _id: True)
    await wf.recompose_shot(shot_id, "Studio r8", "new still", ["/r/a.png"], ["a"])
    wf.pending_recompose[shot_id]["decision"] = "approved"
    fut.set_result(result)
    await asyncio.gather(*wf.background_tasks)


async def test_failed_recompose_clip_can_be_backed_out(monkeypatch):
    wf = _wf()
    accepted = {"status": "completed", "clip_url": "u-old", "clip_path": "data/V03/clip_v2002.mp4"}
    wf.shot_outcomes = {"V03": dict(accepted)}
    wf.clip_takes = {"V03": {2: {"clip_url": "u-old", "clip_path": "data/V03/clip_v2002.mp4"}}}
    wf.clip_review_acceptances = {"V03": {"reason": "10fps PASS"}}
    wf.assembly_sig = []
    await _land_recompose(wf, monkeypatch, "V03",
                          {"status": "needs_review", "still_url": "s-new", "clip_url": "u-new",
                           "clip_path": "data/V03/clip_v4.mp4"})
    # the new composition replaced the shot (as before) and the shot left the edit ...
    assert wf.shot_outcomes["V03"]["status"] == "needs_review" and wf.clip_takes["V03"] == {}
    entries, paths, _, excluded = wf._assembly_plan()
    assert "V03" not in paths and {"shot_id": "V03", "status": "needs_review"} in excluded
    # ... and can now be backed out to the accepted clip
    assert wf._restore_outcome_problem("V03", "") == "a reason is required"
    out = await wf.restore_pre_recompose_outcome("V03", "r8 clip v4 FAIL (intruder)")
    assert out["restored_clip"] == "data/V03/clip_v2002.mp4" and out["backed_out_clip"] == "u-new"
    assert wf.shot_outcomes["V03"] == accepted
    assert wf.clip_takes["V03"] == {2: {"clip_url": "u-old", "clip_path": "data/V03/clip_v2002.mp4"}}
    assert wf.clip_review_acceptances["V03"] == {"reason": "10fps PASS"}
    assert wf._assembly_plan()[1]["V03"] == "data/V03/clip_v2002.mp4"
    assert wf.launched_shot_plans["V03"]["prompt"] == "lobby"          # the old composition's plan
    assert "no accepted pre-recompose outcome" in wf._restore_outcome_problem("V03", "again")
    assert wf.get_state()["recompose"]["restore_log"][0]["shot_id"] == "V03"


async def test_restore_refusals(monkeypatch):
    wf = _wf()
    assert "no accepted pre-recompose" in wf._restore_outcome_problem("V03", "r")
    wf.shot_outcomes = {"V03": {"status": "completed", "clip_url": "u", "clip_path": "p"}}
    await _land_recompose(wf, monkeypatch, "V03",
                          {"status": "completed", "still_url": "s", "clip_url": "u2", "clip_path": "p2"})
    assert "current outcome is accepted" in wf._restore_outcome_problem("V03", "r")
    wf.shot_outcomes["V03"]["status"] = "needs_review"
    wf.pending_clip_retries["V03"] = "x"
    assert "clip retry is pending" in wf._restore_outcome_problem("V03", "r")
    wf.pending_clip_retries.clear()
    wf.passed_gates.append("G_FINAL_CUT")
    assert "final cut already approved" in wf._restore_outcome_problem("V03", "r")


async def test_unaccepted_previous_outcome_is_not_saved(monkeypatch):
    wf = _wf()
    wf.shot_outcomes = {"V03": {"status": "needs_review", "clip_url": "u", "clip_path": "p"}}
    await _land_recompose(wf, monkeypatch, "V03", {"status": "needs_review", "still_url": "s", "clip_url": "u2"})
    assert "V03" not in wf.pre_recompose_outcomes


def test_api_restore_route_calls_the_update():
    import inspect

    import api.main as m
    src = inspect.getsource(m.restore_pre_recompose_outcome)
    assert '"restore_pre_recompose_outcome"' in src and "audit_log" in src


# ---------------------------------------------------------------- GAP-50 pre-check at the routed resolution
def test_seedance_resolution_of():
    f = EpisodeWorkflowV2.seedance_resolution_of
    assert f({"model": "seedance", "seedance_resolution": "720p"}) == "720p"
    assert f({"model": "seedance"}) is None
    assert f({"model": "kling", "seedance_resolution": "720p"}) is None
    assert f(None) is None


async def test_round_budget_precheck_estimates_the_routed_resolution(tmp_path, monkeypatch):
    import hfvg.activities.studio_generation as sg
    from hfvg.pricing import clip_estimate_usd_micros
    seen = []

    class _Ledger:
        def __init__(self, db):
            pass

        async def authorize_round_spend(self, ep, rnd, shot, hold, amount, **kw):
            seen.append(amount)
            return {"ok": True}
    monkeypatch.setattr(sg, "BudgetLedger", _Ledger)
    r720 = await sg.check_round_budget_activity("ep", "G3_CLIPS", "V11", "j", "seedance", 4, "rc2", "720p")
    full = await sg.check_round_budget_activity("ep", "G3_CLIPS", "V11", "j", "seedance", 4, "rc2")
    kl = await sg.check_round_budget_activity("ep", "G3_CLIPS", "V21", "j", "kling", 5, "rc3", "720p")
    assert r720["estimate_usd_micros"] == clip_estimate_usd_micros("seedance", None, 4, "720p") < full["estimate_usd_micros"]
    assert kl["estimate_usd_micros"] == clip_estimate_usd_micros("kling", None, 5)


def test_both_prechecks_pass_the_resolution():
    import inspect
    src = inspect.getsource(EpisodeWorkflowV2)
    assert src.count("self.seedance_resolution_of(params)]") == 2
