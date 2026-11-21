"""MAX_SHOT_RETRIES can be raised for a running episode: retry_shot checks the limit in its update
validator. A validator refusal is not written to the workflow history, so the worker that receives
the update applies its own MAX_SHOT_RETRIES, and an accepted retry replays the same whatever the limit
is later (raised or lowered again). Each accepted retry records its own patch marker; updates recorded
by older workers (no marker, the limit applied in the handler) replay against LEGACY_MAX_SHOT_RETRIES:
tests/test_shot_retry_limit_replay.py replays a recorded history with such a refusal.

Real EpisodeWorkflowV2 + ShotWorkflow children, production activities in DRY_RUN."""

from __future__ import annotations

import asyncio
import json

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from hfvg.config import config
from hfvg.workflows.episode_v2 import RETRY_SHOT_LIMIT_PATCH, EpisodeWorkflowV2
from hfvg.workflows.shot import ShotWorkflow
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import TASK_QUEUE, _write_episode, no_counter  # noqa: F401


def _worker(env, h: Harness) -> Worker:
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=h.activities("kling"))


async def _still_landed(client, workflow_id: str, timeout: float = 40.0) -> dict:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            state = await client.get_workflow_handle(workflow_id).query("get_state")
            if state.get("still_url"):
                return state
        except RPCError:
            pass  # not started yet
        await asyncio.sleep(0.05)
    raise AssertionError(f"{workflow_id}: still never landed")


async def _parked_at_g2(env, tmp_path, ep: str):
    beatmap = _write_episode(tmp_path, ep, lint_ok("A duck waddles through the snow"))
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _still_landed(env.client, f"{ep}-shot-A01")
    return handle


async def _retry(env, handle, ep: str, n: int) -> None:
    """retry_shot A01 is accepted as retry n and its still lands."""
    result = await handle.execute_update("retry_shot", "A01")
    assert (result["status"], result["workflow_id"]) == ("queued", f"{ep}-shot-A01-retry{n}"), result
    await _wait(handle, lambda s: not s["pending_retries"], what=f"retry {n} landed")
    await _still_landed(env.client, f"{ep}-shot-A01-retry{n}")


async def _refusal(handle, update_id: str) -> str | None:
    """The refusal of retry_shot A01 sent with this update id (None: it was accepted)."""
    try:
        await handle.execute_update("retry_shot", "A01", id=update_id)
    except WorkflowUpdateFailedError as e:
        return str(e.cause)
    return None


def _accepted_retry_updates(history) -> list[str]:
    """Update ids of the retry_shot updates the history recorded."""
    ids = []
    for event in history.events:
        if event.HasField("workflow_execution_update_accepted_event_attributes"):
            request = event.workflow_execution_update_accepted_event_attributes.accepted_request
            if request.input.name == "retry_shot":
                ids.append(request.meta.update_id)
    return ids


def _retry_limit_markers(history) -> list[str]:
    ids = []
    for event in history.events:
        if event.HasField("marker_recorded_event_attributes"):
            for payload in event.marker_recorded_event_attributes.details["patch-data"].payloads:
                patch_id = json.loads(payload.data)["id"]
                if patch_id.startswith(RETRY_SHOT_LIMIT_PATCH):
                    ids.append(patch_id)
    return ids


def _mentions(history, text: str) -> bool:
    return text in history.to_json()


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_limit_1_accepts_one_retry_and_refuses_the_next_outside_the_history(
        dry_env, tmp_path, no_counter, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", 1)
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, Harness({})):
            handle = await _parked_at_g2(env, tmp_path, "ep-lim1")
            await _retry(env, handle, "ep-lim1", 1)
            assert await _refusal(handle, "retry-over-limit-1") == "Max retries (1) exceeded for shot A01"
            state = await handle.query("get_state")
            assert state["shot_attempts"]["A01"] == 2 and state["current_gate"] == "G2_SCENE_STILLS", state
            history = await handle.fetch_history()
            accepted = _accepted_retry_updates(history)
            assert len(accepted) == 1, accepted
            assert not _mentions(history, "retry-over-limit-1"), "a validator refusal must leave no history"
            assert _retry_limit_markers(history) == [f"{RETRY_SHOT_LIMIT_PATCH}:{accepted[0]}"]
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_limit_2_accepts_two_retries_and_the_history_replays_with_any_limit(
        dry_env, tmp_path, no_counter, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", 2)
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, Harness({})):
            handle = await _parked_at_g2(env, tmp_path, "ep-lim2")
            await _retry(env, handle, "ep-lim2", 1)
            await _retry(env, handle, "ep-lim2", 2)
            assert await _refusal(handle, "retry-over-limit-2") == "Max retries (2) exceeded for shot A01"
            assert (await handle.query("get_state"))["shot_attempts"]["A01"] == 3
            await handle.terminate("test done")
            history = await handle.fetch_history()

    accepted = _accepted_retry_updates(history)
    assert len(accepted) == 2 and not _mentions(history, "retry-over-limit-2"), accepted
    assert _retry_limit_markers(history) == [f"{RETRY_SHOT_LIMIT_PATCH}:{i}" for i in accepted]
    # neither limit is read when these updates replay: lowered, raised, or a legacy limit that
    # would refuse every retry
    monkeypatch.setattr(config, "LEGACY_MAX_SHOT_RETRIES", 0)
    for limit in (1, 2, 5):
        monkeypatch.setattr(config, "MAX_SHOT_RETRIES", limit)
        result = await Replayer(workflows=[EpisodeWorkflowV2]).replay_workflow(
            history, raise_on_replay_failure=False)
        assert result.replay_failure is None, (limit, result.replay_failure)


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_raising_the_limit_and_restarting_the_worker_lets_a_parked_episode_retry_again(
        dry_env, tmp_path, no_counter, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", 1)
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, Harness({})):
            handle = await _parked_at_g2(env, tmp_path, "ep-raise")
            await _retry(env, handle, "ep-raise", 1)
            assert await _refusal(handle, "refused-at-1") == "Max retries (1) exceeded for shot A01"

        monkeypatch.setattr(config, "MAX_SHOT_RETRIES", 2)  # deploy: a new worker with the raised limit
        async with _worker(env, Harness({})):
            state = await handle.query("get_state")
            assert state["shot_attempts"]["A01"] == 2 and state["current_gate"] == "G2_SCENE_STILLS", state
            await _retry(env, handle, "ep-raise", 2)
            assert await _refusal(handle, "refused-at-2") == "Max retries (2) exceeded for shot A01"
            history = await handle.fetch_history()
            assert len(_accepted_retry_updates(history)) == 2
            assert not _mentions(history, "refused-at-1") and not _mentions(history, "refused-at-2")
            await handle.terminate("test done")
