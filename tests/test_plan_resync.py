"""GAP-28 (ep05v2 2026-10-27): the reviewer corrected the package at G2 (a wardrobe wording and a new
qc_fail_if row), but the running episode had parsed shots.json ONCE at start: the clip prompt, clip
QC rubric and still re-roll base were the stale text. resync_shot_plans(reason) re-parses the package
at G2 and puts the corrected TEXT fields in force everywhere (parked still children's clip submit and
clip QC, every still version incl. an earlier selected one, later re-rolls); structural changes and
any resync after G2 are refused."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from hfvg.plan_resync import clip_fields, plan_resync
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)

OLD_CLIP = lint_ok("A duck in a plain vest waddles slowly")
NEW_CLIP = lint_ok("A duck in a two-tone vest with no badge waddles slowly")
OLD_ROW, NEW_ROW = "vest colour change", "badge, zipper or vest colour change"


def _shot(**kw):
    base = {"shot_id": "A01", "prompt": "p", "clip_prompt": "c", "qc_fail_if": ["x"], "end_prompt": None,
            "shot_meta": {"count": 1}, "ref_keys": [], "trim": None,
            "params": {"model": "kling", "duration": 6, "end_frame": False}}
    base.update(kw)
    return base


# ---------- unit ----------

def test_text_changes_are_reported_per_field():
    r = plan_resync([_shot()], [_shot(prompt="p2", qc_fail_if=["x", "y"])])
    assert r["problems"] == []
    assert r["changes"] == {"A01": {"prompt": "p2", "qc_fail_if": ["x", "y"]}}
    assert clip_fields(r["changes"]["A01"]) == {"qc_fail_if": ["x", "y"]}


def test_identical_package_has_no_changes():
    assert plan_resync([_shot()], [_shot()]) == {"changes": {}, "problems": []}


@pytest.mark.parametrize("new", [
    _shot(shot_meta={"count": 2}),
    _shot(ref_keys=["R1"]),
    _shot(trim={"in": 1}),
    _shot(params={"model": "seedance", "duration": 6, "end_frame": False}),
    _shot(params={"model": "kling", "duration": 8, "end_frame": False}),
    _shot(params={"model": "kling", "duration": 6, "end_frame": True}),
    _shot(prompt="  "),
])
def test_structural_or_empty_changes_are_refused(new):
    assert plan_resync([_shot()], [new])["problems"]


def test_shot_set_change_is_refused():
    r = plan_resync([_shot()], [_shot(), _shot(shot_id="A02")])
    assert r["problems"] and "A02" in r["problems"][0]
    assert plan_resync([_shot(), _shot(shot_id="A02")], [_shot()])["problems"]


# ---------- integration (dry) ----------

def _rewrite(beatmap: str, **fields):
    p = Path(beatmap).with_name("shots.json")
    data = json.loads(p.read_text())
    data["shots"][0].update(fields)
    p.write_text(json.dumps(data))


def _recording_review(rubrics):
    from hfvg.activities.qc import review_clip as real

    @activity.defn(name="review_clip")
    async def review(asset_url, rubric, episode_id=None, shot_id=None):
        rubrics.append(dict(rubric or {}))
        return await real(asset_url, rubric, episode_id, shot_id)
    return review


def _acts(h, clips, extra=()):
    names = {getattr(a, "__temporal_activity_definition").name for a in extra} | {"save_asset_locally"}
    base = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name not in names]
    return base + [clips.activity(), *extra]


async def _still_landed(env, wid):
    for _ in range(200):
        st = await env.client.get_workflow_handle(wid).query("get_state")
        if st.get("still_url"):
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"{wid}: no still")


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_resync_at_g2_reaches_clip_submit_clip_qc_and_rerolls(dry_env, tmp_path, no_counter):
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    rubrics: list[dict] = []
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips, [_recording_review(rubrics)])):
            ep = "ep81"
            beatmap = _write_episode(tmp_path, ep, lint_ok("A duck in a plain vest stands in snow"),
                                     clip_prompt=OLD_CLIP, qc_fail_if=[OLD_ROW])
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            v1 = await _still_landed(env, f"{ep}-shot-A01")

            new_still = lint_ok("A duck in a two-tone vest with no badge stands in snow")
            _rewrite(beatmap, prompt=new_still, clip_prompt=NEW_CLIP, qc_fail_if=[OLD_ROW, NEW_ROW])
            before = (await handle.query("clip_plans"))["A01"]
            assert before["clip_prompt"] == OLD_CLIP and before["qc_fail_if"] == [OLD_ROW]
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("resync_shot_plans", args=["  "])
            r = await handle.execute_update("resync_shot_plans", args=["vest wording corrected"])
            assert r["success"] and set(r["changed"]["A01"]) == {"prompt", "clip_prompt", "qc_fail_if"}, r
            assert r["signalled"] == [f"{ep}-shot-A01"], r
            after = (await handle.query("clip_plans"))["A01"]
            assert after["clip_prompt"] == NEW_CLIP and after["qc_fail_if"] == [OLD_ROW, NEW_ROW] and after["resynced"]
            for _ in range(100):
                cs = await env.client.get_workflow_handle(f"{ep}-shot-A01").query("get_state")
                if cs.get("clip_plan_update"):
                    break
                await asyncio.sleep(0.05)
            assert cs["clip_plan_update"] == {"clip_prompt": NEW_CLIP, "qc_fail_if": [OLD_ROW, NEW_ROW]}

            # a re-roll after the resync is built on the corrected still prompt
            n_still = len(h.still_calls)
            rr = await handle.execute_update("retry_shot", args=["A01", "warmer light"])
            assert rr["status"] == "queued", rr
            await _wait(handle, lambda s: not s["pending_retries"], what="retry landed")
            await _still_landed(env, f"{ep}-shot-A01-retry1")
            sent = [a for a in h.still_calls[n_still][:6] if isinstance(a, str) and "vest" in a]
            assert sent and "two-tone vest with no badge" in sent[0] and "plain vest" not in sent[0], sent

            # keep the EARLIER version (started before the resync): its clip still uses the corrected text
            await handle.execute_update("select_still_version", args=["A01", 1, "v1 kept"])
            n_clip = len(h.clip_calls)
            await handle.execute_update("approve_g2_scene_stills")
            kept = await _child_result(env, f"{ep}-shot-A01", timeout=90)
            assert kept["still_url"] == v1["still_url"], kept
            assert len(h.clip_calls) == n_clip + 1
            assert h.clip_calls[n_clip][3] == NEW_CLIP, h.clip_calls[n_clip][3]
            assert rubrics and rubrics[-1].get("qc_fail_if") == [OLD_ROW, NEW_ROW], rubrics[-1]

            # after G2 the plans are frozen
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("resync_shot_plans", args=["too late"])


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_structural_resync_is_refused_and_changes_nothing(dry_env, tmp_path, no_counter):
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            ep = "ep82"
            beatmap = _write_episode(tmp_path, ep, lint_ok("A duck stands in snow"), clip_prompt=OLD_CLIP)
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            with pytest.raises(WorkflowUpdateFailedError):  # not at G2 yet
                await handle.execute_update("resync_shot_plans", args=["early"])
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _still_landed(env, f"{ep}-shot-A01")
            _rewrite(beatmap, clip_prompt=NEW_CLIP, duration=8)
            with pytest.raises(WorkflowUpdateFailedError) as ei:
                await handle.execute_update("resync_shot_plans", args=["longer shot"])
            assert "duration" in str(ei.value.cause)
            assert (await handle.query("clip_plans"))["A01"]["clip_prompt"] == OLD_CLIP
            st = await handle.query("get_state")
            assert st["resync_log"] == [] and st["clip_plan_updates"] == {}
            await handle.terminate("test done")
