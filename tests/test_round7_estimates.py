"""Round 7 B4: G1 shows the same per-call estimate the submits reserve, in dry and live runs
(no flat placeholder; pre-approved start stills cost nothing)."""

import asyncio
import json

import httpx
import pytest
import respx
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities.g1_plan import compute_g1_plan_activity
from hfvg.pricing import DRY_STILL_USD_MICROS, rate_table_estimate
from tests.test_gate_correctness_round1 import TASK_QUEUE, Recorder, _activities, _until, _wait, episode_env  # noqa: F401
from tests.test_round7_pre_approved import _anchor_a03, _start


def _shot(sid, model="kling", duration=5, tier=None, end_frame=False):
    params = {"model": model, "duration": duration, "end_frame": end_frame}
    if tier:
        params["tier"] = tier
    return {"shot_id": sid, "prompt": f"Shot {sid}", "params": params}


@pytest.mark.asyncio
async def test_dry_g1_is_the_sum_of_the_dry_per_call_reservations():
    shots = [_shot("A01", duration=6), _shot("A02", end_frame=True), _shot("A03", "seedance", 4, "draft"),
             _shot("A04", "seedance", 10)]
    plan = await compute_g1_plan_activity("ep80", shots, dry_run=True)
    rows = {r["shot_id"]: r for r in plan["shots"]}
    assert rows["A01"]["still_usd_micros"] == DRY_STILL_USD_MICROS
    assert rows["A02"]["still_usd_micros"] == 2 * DRY_STILL_USD_MICROS  # start + end still
    assert rows["A01"]["clip_usd_micros"] == rate_table_estimate("kling-3.0-pro", None, 6).usd_micros
    assert rows["A03"]["clip_usd_micros"] == rate_table_estimate("seedance-2.5", "480p", 4).usd_micros
    assert rows["A04"]["clip_usd_micros"] == rate_table_estimate("seedance-2.5", "1080p", 10).usd_micros
    total = sum(r["still_usd_micros"] + r["clip_usd_micros"] for r in rows.values())
    assert plan["total_usd_micros"] == total
    assert plan["estimates_available"] is True
    # not the old flat placeholder (1 cr / still, 5 cr / clip)
    assert rows["A01"]["clip_usd_micros"] != rows["A04"]["clip_usd_micros"]


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_dry_g1_total_equals_the_dry_reservations_made(episode_env, tmp_path):
    """Run the real workflow (2 of 4 pre-approved): what G1 shows == what the submits reserved."""
    from hfvg.activities import studio_generation as sg
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    reserved: list[tuple[str, str, int]] = []
    rec = Recorder()

    @activity.defn(name="submit_still_job_enforced")
    async def still(*args):
        rec.still_calls.append(args)
        out = await sg.submit_still_job_enforced(*args)
        reserved.append(("still", args[1], out["reserved_amount"]))
        return out

    @activity.defn(name="submit_clip_job_enforced")
    async def clip(*args):
        rec.clip_calls.append(args)
        out = await sg.submit_clip_job_enforced(*args)
        reserved.append(("clip", args[1], out["reserved_amount"]))
        return out

    def name(a):
        return getattr(a, "__temporal_activity_definition").name

    acts = [a for a in _activities(rec) if name(a) not in {"submit_still_job_enforced", "submit_clip_job_enforced"}]
    ep = "ep81"
    await _anchor_a03(episode_env, tmp_path, ep)
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=[*acts, still, clip]):
            handle = await _start(env, tmp_path, ep)
            g1 = (await handle.query("get_state"))["g1_plan"]
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: len([r for r in reserved if r[0] == "still"]) == 2, what="2 stills")
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            await _until(lambda: len([r for r in reserved if r[0] == "clip"]) == 4, what="4 clips")
            await handle.terminate()
    rows = {r["shot_id"]: r for r in g1["shots"]}
    for sid in ("A01", "A02", "A03", "A04"):
        made = sum(a for _, s, a in reserved if s == sid)
        assert rows[sid]["still_usd_micros"] + rows[sid]["clip_usd_micros"] == made, (sid, rows[sid], reserved)
    assert g1["total_usd_micros"] == sum(a for *_, a in reserved)
    assert rows["A01"]["still_usd_micros"] == 0 and rows["A03"]["still_usd_micros"] == 0


@pytest.mark.asyncio
async def test_live_g1_uses_the_provider_estimates_the_live_submit_reserves(monkeypatch):
    """Live: per-call provider estimates (stills, Kling) and the Seedance rate table at the
    resolution the submit routes to (draft -> 480p), in integer usd micros; pre-approved shots
    have no start-still estimate."""
    from hfvg.activities import g1_plan

    async def no_pre_approved(_ep, _shots):
        return {"A01"}

    monkeypatch.setattr(g1_plan, "_pre_approved_ids", no_pre_approved)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    monkeypatch.setenv("G1_ESTIMATE_PLACEHOLDER_IMAGE_URL", "https://example.com/placeholder.jpg")
    with respx.mock:
        still = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"credits": "1.5", "usd": "0.0731"}))
        respx.post("http://mock.api/estimate/kling-video/v3.0/pro/image-to-video").mock(
            return_value=httpx.Response(200, json={"credits": "10", "usd": "0.336"}))
        shots = [_shot("A01", duration=6), _shot("A02", "seedance", 4, "draft"), _shot("A03", "seedance", 5)]
        plan = await compute_g1_plan_activity("ep82", shots, dry_run=False)
    rows = {r["shot_id"]: r for r in plan["shots"]}
    assert still.call_count == 2, "no start-still estimate for the pre-approved A01"
    assert rows["A01"]["still_usd_micros"] == 0 and rows["A01"]["pre_approved"] is True
    assert rows["A01"]["clip_usd_micros"] == 336_000
    assert rows["A02"]["still_usd_micros"] == 73_100
    assert rows["A02"]["clip_usd_micros"] == rate_table_estimate("seedance-2.5", "480p", 4).usd_micros
    assert rows["A03"]["clip_usd_micros"] == rate_table_estimate("seedance-2.5", "1080p", 5).usd_micros
    assert plan["total_usd_micros"] == sum(r["still_usd_micros"] + r["clip_usd_micros"] for r in rows.values())
    assert plan["estimates_available"] is True
