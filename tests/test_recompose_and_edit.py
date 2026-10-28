"""GAP-21 / GAP-22 (ep05full 2026-10-27, Studio G3 review).

GAP-21: after G2 there was no way to re-shoot a shot with a NEW composition: retry_shot only runs at
G1/G2 and only appends to the old prompt with the old refs; retry_clip re-animates the old still.
recompose_shot(shot, reason, prompt, refs) runs a fresh ShotWorkflow child whose still uses the new
prompt (replaced, not appended) and exactly the given refs, with a new version (new job ids). The
still PARKS until recompose_decision: reject -> no clip spend, the shot keeps its clip; approve ->
the clip is made from the new still and replaces the shot in the edit. Final cut is held meanwhile.

GAP-22: a shot could not be trimmed or dropped at G_FINAL_CUT without hand-editing state.
set_shot_edit(shot, reason, trim_in, trim_out, drop) re-assembles the edit with the override."""

from __future__ import annotations

import asyncio
import json

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from tests.lint_helpers import lint_ok
from tests.test_ep05_review_gaps import _escalating_review
from tests.test_moderation_recovery import Harness, _child_result, _until, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)


async def _to_final_cut(env, h, tmp_path, ep, two_shots=False):
    beatmap = _write_episode(tmp_path, ep, lint_ok("A duck waddles through the snow"))
    if two_shots:
        ep_dir = tmp_path / "data" / "episodes" / ep
        shots = json.loads((ep_dir / "shots.json").read_text())["shots"]
        second = dict(shots[0], id="A02", prompt=lint_ok("A duck stands by the pool"))
        (ep_dir / "shots.json").write_text(json.dumps({"shots": [shots[0], second]}))
        (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": [], "A02": []}}))
        bm = (ep_dir / "BEATMAP.md").read_text().rstrip("\n")
        (ep_dir / "BEATMAP.md").write_text(bm + "\n| 2 | A02 | 5.5 (6.0) | Park | Day | D | Duck by pool | Outro | - | - | K |\n")
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _until(lambda: h.still_calls, what="still")
    await asyncio.sleep(0.5)
    await handle.execute_update("approve_g2_scene_stills")
    await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                and (s["assembly"]["qc"] or {}).get("verdict"), timeout=120, what="edit 1")
    return handle


def _worker(env, h, clips):
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    acts = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name
            not in ("save_asset_locally", "review_clip")] + [clips.activity(), _escalating_review()]
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=acts)


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_recompose_parks_still_then_reject_or_approve(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "char_r.png"
    ref.write_bytes(b"png")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep94")
            state = await handle.query("get_state")
            original = state["shot_outcomes"]["A01"]
            stills_before, clips_before = len(h.still_calls), len(h.clip_calls)

            # the old route stays G1/G2 only
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("retry_shot", args=["A01", "more ducks"])
            # explicit refs are required
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("recompose_shot", args=["A01", "r", "new prompt", []])
            assert "ref list" in str(refused.value.cause)

            new_prompt = lint_ok("Tighter wide shot. EXACTLY THREE ducks on the boardwalk")
            out = await handle.execute_update(
                "recompose_shot", args=["A01", "Studio: new composition", new_prompt, [str(ref)], ["char_r"]])
            assert out["workflow_id"] == "ep94-shot-A01-recompose1" and out["version"] >= 2, out
            state = await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                                timeout=90, what="recompose still parked")
            call = h.still_calls[-1]
            assert len(h.still_calls) == stills_before + 1
            assert call[2] == new_prompt, call[2]          # replaced, not appended
            assert list(call[4]) == [str(ref)], call[4]     # exactly the given refs
            assert len(h.clip_calls) == clips_before        # parked: no clip spend yet
            # final cut is held while the recompose is pending
            await handle.signal("approve_g_final_cut")
            state = await _wait(handle, lambda s: any("recompose pending" in r for r in s["final_cut_refusals"]),
                                what="refusal")
            assert not state["approvals"]["g_final_cut"]
            # a second recompose cannot start while one is pending
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("recompose_shot", args=["A01", "again", new_prompt, [str(ref)]])

            # reject: the child ends without a clip, the shot keeps its clip
            await handle.execute_update("recompose_decision", args=["A01", False, "4 char_cs in the still"])
            rejected = await _child_result(env, "ep94-shot-A01-recompose1", timeout=60)
            assert rejected["status"] == "rejected"
            state = await _wait(handle, lambda s: not s["recompose"]["pending"], what="recompose 1 closed")
            assert len(h.clip_calls) == clips_before
            assert state["shot_outcomes"]["A01"]["clip_url"] == original["clip_url"]
            assert state["recompose"]["log"][0]["decision"] == "rejected"

            # second attempt: approve -> clip from the NEW still replaces the shot in the edit
            out2 = await handle.execute_update(
                "recompose_shot", args=["A01", "Studio: retry", new_prompt, [str(ref)], ["char_r"]])
            assert out2["version"] == out["version"] + 1  # new still/clip job ids, never a duplicate
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="recompose 2 parked")
            new_still = h.still_calls[-1]
            await handle.execute_update("recompose_decision", args=["A01", True, "exactly 3"])
            landed = await _child_result(env, "ep94-shot-A01-recompose2", timeout=90)
            assert landed.get("clip_url") and landed["clip_path"] != original.get("clip_url")
            assert len(h.clip_calls) == clips_before + 1
            state = await _wait(handle, lambda s: not s["recompose"]["pending"]
                                and s["shot_outcomes"]["A01"]["clip_url"] == landed["clip_url"], what="replaced")
            assert state["shot_workflow_ids"]["A01"] == "ep94-shot-A01-recompose2"
            assert new_still[3] == out2["version"]
            # the reviewer keeps the new needs_review clip; the edit is rebuilt from it
            await handle.execute_update("accept_clip_review", args=["A01", "ok", None])
            state = await _wait(handle, lambda s: s["assembly"]["manifest"].get("clips")
                                and s["assembly"]["manifest"]["clips"][0]["source"] == landed["clip_path"],
                                timeout=120, what="edit rebuilt with the recomposed clip")
            # bounded
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("recompose_shot", args=["A01", "third", new_prompt, [str(ref)]])
            assert "max recomposes" in str(refused.value.cause)
            await handle.terminate("test done")


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_set_shot_edit_trims_and_drops(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep95", two_shots=True)
            for sid in ("A01", "A02"):
                await handle.execute_update("accept_clip_review", args=[sid, "ok", None])
            state = await _wait(handle, lambda s: len(s["assembly"]["manifest"].get("clips") or []) == 2
                                and not s["assembly"]["stale"], timeout=120, what="2-shot edit")
            v = state["assembly"]["qc"]["edit_version"]
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("set_shot_edit", args=["A01", "too short", 0.0, 0.5])
            assert "< 1.0s" in str(refused.value.cause)
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("set_shot_edit", args=["A01", "past end", 0.0, 9.0])
            await handle.execute_update("set_shot_edit", args=["A01", "end before the rock", None, 3.0])
            state = await _wait(handle, lambda s: any(x["id"] == "A01" and x["trim_out"] == 3.0
                                                      for x in s["assembly"]["manifest"].get("clips") or [])
                                and (s["assembly"]["qc"] or {}).get("edit_version", 0) > v
                                and not s["assembly"]["stale"], timeout=120, what="trimmed edit")
            c = {x["id"]: x for x in state["assembly"]["manifest"]["clips"]}
            assert c["A01"]["trim_out"] == 3.0 and abs(c["A01"]["seg_s"] - 3.0) < 1e-6, c["A01"]
            v = state["assembly"]["qc"]["edit_version"]
            await handle.execute_update("set_shot_edit", args=["A02", "headcount failed", None, None, True])
            state = await _wait(handle, lambda s: [x["id"] for x in s["assembly"]["manifest"].get("clips") or []]
                                == ["A01"] and (s["assembly"]["qc"] or {}).get("edit_version", 0) > v
                                and (s["assembly"]["qc"] or {}).get("excluded_shots")
                                and not s["assembly"]["stale"], timeout=120, what="edit without A02")
            assert [x["id"] for x in state["assembly"]["manifest"]["clips"]] == ["A01"]
            assert {"shot_id": "A02", "status": "dropped", "reason": "headcount failed"} in \
                state["assembly"]["qc"]["excluded_shots"]
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("set_shot_edit", args=["A01", "x", None, None, True])
            assert "last shot" in str(refused.value.cause)
            await handle.terminate("test done")
