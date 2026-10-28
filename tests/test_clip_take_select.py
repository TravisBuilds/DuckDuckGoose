"""GAP-18 (ep05full 2026-10-27): after a paid clip retry the reviewer could only keep the LATEST
take. When the retry came out worse than the original (F15: the start-frame issue persisted and the
retry added a new one), the better original could not be kept without hand-editing state.
accept_clip_review(shot, reason, None, take) keeps an earlier landed take; the edit is rebuilt
with that take's file. Unknown takes are refused; the API checks the take's file is on disk."""

from __future__ import annotations

import asyncio

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from tests.lint_helpers import lint_ok
from tests.test_ep05_review_gaps import _escalating_review
from tests.test_moderation_recovery import Harness, _child_result, _until, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_reviewer_keeps_original_take_after_worse_retry(dry_env, tmp_path, no_counter):  # noqa: F811
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
            beatmap = _write_episode(tmp_path, "ep93", lint_ok("A duck waddles through the snow"))
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=["ep93", beatmap, True, None, None, None],
                id="ep93", task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: h.still_calls, what="still")
            await asyncio.sleep(0.5)
            await handle.execute_update("approve_g2_scene_stills")
            original = await _child_result(env, "ep93-shot-A01", timeout=90)
            assert original["status"] == "needs_review"
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                        and (s["assembly"]["qc"] or {}).get("verdict"), timeout=90, what="edit 1")

            await handle.execute_update("retry_clip", args=["A01", "tray vanishes", "Duck walks slowly"])
            retry = await _child_result(env, "ep93-shot-A01-clipretry1", timeout=90)
            assert retry["status"] == "needs_review" and retry["clip_path"] != original["clip_path"]
            state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry landed")
            takes = state["clip_takes"]["A01"]
            assert takes["0"]["clip_path"] == original["clip_path"], takes
            assert takes["1"]["clip_path"] == retry["clip_path"], takes
            assert state["shot_outcomes"]["A01"].get("clip_url") == retry["clip_url"], state["shot_outcomes"]["A01"]

            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("accept_clip_review", args=["A01", "keep", None, 7])
            assert "no clip take 7" in str(refused.value.cause), refused.value.cause
            out = await handle.execute_update(
                "accept_clip_review", args=["A01", "retry worse: keep the original", None, 0])
            assert out["clip_path"] == original["clip_path"], out
            state = await _wait(handle, lambda s: (s["assembly"]["qc"] or {}).get("edit_version", 0) >= 2
                                and s["assembly"]["manifest"] and s["assembly"]["manifest"]["clips"],
                                timeout=120, what="rebuilt edit")
            assert state["assembly"]["manifest"]["clips"][0]["source"] == original["clip_path"]
            assert state["shot_outcomes"]["A01"]["clip_url"] == original["clip_url"]
            assert state["clip_review_acceptances"]["A01"]["take"] == 0
            # no take: the latest outcome is kept, exactly as before (unchanged default)
            await handle.terminate("test done")
