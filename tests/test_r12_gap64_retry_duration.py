"""GAP-64 (ep05v2 r12): retry_clip(shot, reason, prompt, duration_s) submits the retry at the new clip
length (paid submit + round-budget estimate), leaves the launched plan alone, logs it, refuses an
out-of-range length before any spend, and the old 3-arg call keeps the plan duration."""
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
async def test_retry_clip_duration_override(dry_env, tmp_path, no_counter, monkeypatch):
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
            beatmap = _write_episode(tmp_path, "ep64", lint_ok("A duck waddles through the snow"))
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=["ep64", beatmap, True, None, None, None],
                id="ep64", task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: h.still_calls, what="still")
            await asyncio.sleep(0.5)
            await handle.execute_update("approve_g2_scene_stills")
            await _child_result(env, "ep64-shot-A01", timeout=90)
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                        and (s["assembly"]["qc"] or {}).get("verdict"), timeout=90, what="edit 1")
            plan_duration = h.clip_calls[0][4]
            n_calls = len(h.clip_calls)

            # out of range: refused by the validator, nothing submitted, no retry counted
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("retry_clip", args=["A01", "longer take", None, 20])
            assert "duration_s must be 3-15" in str(refused.value.cause)
            assert len(h.clip_calls) == n_calls

            out = await handle.execute_update("retry_clip", args=["A01", "longer take", None, 7])
            await _child_result(env, "ep64-shot-A01-clipretry1", timeout=90)
            assert h.clip_calls[-1][4] == 7 and plan_duration != 7
            state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry 1 landed")
            assert state["clip_retry_log"][-1]["duration_s"] == 7
            assert out["estimate_usd_micros"] is None or out["estimate_usd_micros"] >= 0

            # the old 3-arg call: the plan duration (launched plan was not changed by the override)
            await handle.execute_update("retry_clip", args=["A01", "plan length again", None])
            await _child_result(env, "ep64-shot-A01-clipretry2", timeout=90)
            assert h.clip_calls[-1][4] == plan_duration
            state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry 2 landed")
            assert "duration_s" not in state["clip_retry_log"][-1]
            await handle.terminate("test done")
