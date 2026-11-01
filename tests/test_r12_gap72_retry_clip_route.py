"""GAP-72 (ep05v2 r12 v10b, 2026-10-28 21:00 CET): retry_clip could not change the clip model. V11 is a
Seedance-routed shot (720p, $1.85 per 4 s); a clip-only Kling re-take from the SAME approved still
($0.56 / 5 s) was only reachable through a paid recompose (new still). retry_clip now takes an optional
clip_route (same keys and validation as the recompose clip_route, GAP-45): the retry is submitted and
budget-checked on the routed model, the launched plan is not changed, the route is logged, a bad route
is refused before anything is counted, and the API preflights the prompt against the ROUTED model."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from tests.lint_helpers import lint_ok
from tests.test_ep05_review_gaps import _escalating_review
from tests.test_moderation_recovery import Harness, _child_result, _until, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)
from tests.test_r12_gap66_retry_clip_preflight import _Client, _Handle, _plan, _prompt, kit  # noqa: F401
from tests.test_retry_prompt_preflight import client  # noqa: F401


def test_clip_retry_problem_validates_route_and_routed_duration():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W
    fake = SimpleNamespace(
        approved_g2_scene_stills=True, passed_gates={"G2_SCENE_STILLS"}, approved_g_final_cut=False,
        launched_shot_plans={"A01": {"params": {"model": "seedance", "seedance_resolution": "720p"}}},
        state=SimpleNamespace(shot_decisions={}), pending_clip_retries={}, clip_retries={},
        shot_outcomes={"A01": {"status": "completed", "still_url": "u"}},
        clip_route_problem=W.clip_route_problem, routed_params=W.routed_params,
        clip_duration_problem=W.clip_duration_problem)
    f = W._clip_retry_problem
    assert f(fake, "A01", "r") is None
    assert f(fake, "A01", "r", None, {"model": "kling"}) is None
    assert "clip_route.model must be one of" in f(fake, "A01", "r", None, {"model": "veo"})
    assert "may only set" in f(fake, "A01", "r", None, {"model": "kling", "tier": "x"})
    # Kling allows 3 s, Seedance does not: the duration is checked on the ROUTED model
    assert f(fake, "A01", "r", 3, {"model": "kling"}) is None
    assert "4-15" in f(fake, "A01", "r", 3)
    # a resolution-only route keeps the launched Seedance model
    assert f(fake, "A01", "r", None, {"seedance_resolution": "480p"}) is None


def test_api_preflights_the_prompt_against_the_routed_model(client, kit, monkeypatch):  # noqa: F811
    api_main, c = client
    monkeypatch.setenv("DRY_RUN", "false")
    seen = {}
    from hfvg import preflight as pf
    real = pf.prompt_limit_violations

    def spy(*a, **k):
        seen["model"] = k.get("clip_model_alias")
        return real(*a, **k)
    monkeypatch.setattr(pf, "prompt_limit_violations", spy)
    plan = dict(_plan(_prompt(800)), clip_model="seedance")
    h = _Handle(plan)
    sent = []

    async def _exec(ep, name, args, what):
        sent.append((name, list(args)))
        return {"status": "queued", "retry": 1, "job_id": "j"}
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    monkeypatch.setattr(api_main, "_execute_episode_update", _exec)
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip",
               json={"reason": "t", "duration_s": 5, "clip_route": {"model": "kling"}})
    assert r.status_code == 200, r.text
    assert seen["model"] == "kling"
    assert sent == [("retry_clip", ["A01", "t", None, 5, {"model": "kling"}])]
    # no route: the old shapes are unchanged
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t", "duration_s": 5})
    assert r.status_code == 200 and sent[-1] == ("retry_clip", ["A01", "t", None, 5]) and seen["model"] == "seedance"
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t"})
    assert r.status_code == 200 and sent[-1] == ("retry_clip", ["A01", "t", None])


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_retry_clip_route_submits_on_the_routed_model(dry_env, tmp_path, no_counter, monkeypatch):
    from hfvg.config import config
    monkeypatch.setattr(config, "MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", 2)
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        acts = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name
                not in ("save_asset_locally", "review_clip")] + [clips.activity(), _escalating_review()]
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=acts):
            beatmap = _write_episode(tmp_path, "ep72", lint_ok("A duck waddles through the snow"))
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=["ep72", beatmap, True, None, None, None],
                id="ep72", task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: h.still_calls, what="still")
            await asyncio.sleep(0.5)
            await handle.execute_update("approve_g2_scene_stills")
            await _child_result(env, "ep72-shot-A01", timeout=90)
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                        and (s["assembly"]["qc"] or {}).get("verdict"), timeout=90, what="edit 1")
            plan_model = h.clip_calls[0][8]
            assert plan_model == "kling"
            n_calls = len(h.clip_calls)

            # a bad route is refused by the validator: nothing submitted, no retry counted
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("retry_clip", args=["A01", "routed", None, None, {"model": "veo"}])
            assert "clip_route.model must be one of" in str(refused.value.cause)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("retry_clip", args=["A01", "routed", None, 3, {"model": "seedance"}])
            assert len(h.clip_calls) == n_calls
            state = await handle.query("get_state")
            assert state["clip_retries"].get("A01", 0) == 0

            await handle.execute_update("retry_clip", args=["A01", "routed", None, 5, {"model": "seedance"}])
            await _child_result(env, "ep72-shot-A01-clipretry1", timeout=90)
            assert h.clip_calls[n_calls][8] == "seedance" and h.clip_calls[n_calls][4] == 5
            state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry 1 landed")
            assert state["clip_retry_log"][-1]["clip_route"] == {"model": "seedance"}

            # no route again: the launched plan's model (the route did not change the plan)
            await handle.execute_update("retry_clip", args=["A01", "plan route", None])
            await _child_result(env, "ep72-shot-A01-clipretry2", timeout=90)
            assert h.clip_calls[-1][8] == plan_model
            state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry 2 landed")
            assert "clip_route" not in state["clip_retry_log"][-1]
            await handle.terminate("test done")
