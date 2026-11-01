"""GAP-41 (ep05v2 r6): a COMPLETED episode (final cut approved) could not be reopened: every recompose /
retry / edit update requires a running workflow parked at G_FINAL_CUT. reopen_snapshot (query on the
closed run) + a new run with reopen_from restores the state, un-approves the final cut and parks at
G_FINAL_CUT again; recompose numbering / versions continue (no repeated job id) and no G1/G2/still/clip
work is redone."""
from __future__ import annotations

import asyncio

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, needs_ffmpeg, no_counter  # noqa: F401
from tests.test_recompose_and_edit import _to_final_cut, _worker


def _snap(**attrs):
    base = {"passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"], "approved_g_final_cut": True,
            "pending_clip_retries": {}, "pending_recompose": {}}
    base.update(attrs)
    return {"version": 1, "episode_id": "ep1", "state": {"pending_retries": {}}, "attrs": base}


def test_reopen_problem_rules():
    p = EpisodeWorkflowV2.reopen_problem
    assert p("ep1", {"snapshot": _snap(), "reason": "Studio"}) is None
    assert "reason" in p("ep1", {"snapshot": _snap(), "reason": " "})
    assert "missing" in p("ep1", {"reason": "r"})
    assert "not 'ep2'" in p("ep2", {"snapshot": _snap(), "reason": "r"})
    assert "final cut was approved" in p("ep1", {"snapshot": _snap(approved_g_final_cut=False), "reason": "r"})
    assert "final cut was approved" in p("ep1", {"snapshot": _snap(passed_gates=["G1_SHOT_PLAN"]), "reason": "r"})
    assert "in flight" in p("ep1", {"snapshot": _snap(pending_recompose={"A01": {}}), "reason": "r"})
    bad = _snap(); bad["version"] = 99
    assert "version" in p("ep1", {"snapshot": bad, "reason": "r"})
    st = _snap(); st["state"]["pending_retries"] = {"A01": "x"}
    assert "pending still retries" in p("ep1", {"snapshot": st, "reason": "r"})


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(500)
async def test_completed_episode_reopens_at_final_cut_and_recompose_continues(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "character1.png"
    ref.write_bytes(b"png")
    new_prompt = lint_ok("Tighter wide shot. EXACTLY THREE ducks on the boardwalk")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep93")
            # one recompose in the first run (rejected: no clip), then approve -> COMPLETED
            await handle.execute_update("recompose_shot", args=["A01", "first", new_prompt, [str(ref)], ["character1"]])
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="parked")
            await handle.execute_update("recompose_decision", args=["A01", False, "no"])
            before = await _wait(handle, lambda s: not s["recompose"]["pending"], what="closed")
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=90)
            desc = await handle.describe()
            assert desc.status == WorkflowExecutionStatus.COMPLETED
            # a completed run takes no update
            with pytest.raises(Exception):
                await handle.execute_update("recompose_shot", args=["A01", "x", new_prompt, [str(ref)]])

            stills, clips_n = len(h.still_calls), len(h.clip_calls)
            snap = await env.client.get_workflow_handle("ep93", run_id=desc.run_id).query("reopen_snapshot")
            reopen_from = {"snapshot": snap, "reason": "Studio reopened", "from_run_id": desc.run_id}
            assert EpisodeWorkflowV2.reopen_problem("ep93", reopen_from) is None
            attrs = snap["attrs"]
            new = await env.client.start_workflow(
                "EpisodeWorkflowV2",
                args=["ep93", attrs["_beatmap_path"], attrs["_dry_run"], None, None, None, reopen_from],
                id="ep93", task_queue=TASK_QUEUE, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
            latest = env.client.get_workflow_handle("ep93")   # API handles resolve to the new run
            state = await _wait(latest, lambda s: s["current_gate"] == "G_FINAL_CUT" and s["reopen_log"],
                                what="reopened")
            assert new.result_run_id != desc.run_id
            assert not state["approvals"]["g_final_cut"]
            assert "G_FINAL_CUT" not in state["passed_gates"] and "G2_SCENE_STILLS" in state["passed_gates"]
            assert state["reopen_log"][0]["reason"] == "Studio reopened"
            assert state["reopen_log"][0]["from_run_id"] == desc.run_id
            assert state["shot_outcomes"] == before["shot_outcomes"]
            assert state["recompose"]["counts"] == {"A01": 1}
            assert state["assembly"]["edit_version"] == before["assembly"]["edit_version"]  # not re-assembled
            assert state["assembly"]["stale"] is False and state["delivery"] == before["delivery"]
            await asyncio.sleep(1.0)
            assert (len(h.still_calls), len(h.clip_calls)) == (stills, clips_n)   # nothing regenerated

            # recompose continues the numbering: recompose2, a NEW version (never a repeated job id)
            out = await latest.execute_update(
                "recompose_shot", args=["A01", "after reopen", new_prompt, [str(ref)], ["character1"]])
            assert out["workflow_id"] == "ep93-shot-A01-recompose2", out
            assert out["version"] > before["recompose"]["log"][0]["version"]
            await _wait(latest, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="parked after reopen")
            assert len(h.still_calls) == stills + 1
            await latest.execute_update("recompose_decision", args=["A01", True, "ok"])
            landed = await _child_result(env, "ep93-shot-A01-recompose2", timeout=90)
            assert landed.get("clip_url")
            await _wait(latest, lambda s: not s["recompose"]["pending"]
                        and s["shot_outcomes"]["A01"]["clip_url"] == landed["clip_url"], what="replaced")
            assert len(h.clip_calls) == clips_n + 1
            # a second reopen of a RUNNING episode is not possible (API checks COMPLETED); the reopened
            # run can be approved again
            await latest.execute_update("accept_clip_review", args=["A01", "ok", None])
            await _wait(latest, lambda s: s["assembly"]["edit_version"] > before["assembly"]["edit_version"],
                        timeout=120, what="re-assembled")
            await latest.terminate("test done")
