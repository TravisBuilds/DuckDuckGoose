"""Replay safety for recompose_shot taking a show's shot flags (``shot_meta.flags``).

fixtures/replay/recompose_with_flags.json is a real EpisodeWorkflowV2 history recorded by
scripts/capture_recompose_flags_history.py (local Temporal server, the dry-run harness, real ShotWorkflow
children, real ffmpeg). A worker on main 76d22d1 (before show profiles) ran the episode to G_FINAL_CUT and a
recompose of A01 with shot_meta {"count": 2}, rejected at its still: the run is parked at G_FINAL_CUT (the
first ``parked_events`` events, a run like ep61opr when this deploys). Then the deploy: a worker on the
show-profile code replayed it and ran a recompose with shot_meta {"count": 1, "flags": ...}, rejected at its
still; the final cut was approved and the run completed.

The flags merge into the child's plan behind ``workflow.patched("recompose-meta-flags")``, called only for
a recompose that carries flags: the parked history (no such recompose, no marker) replays on this code
unchanged, and the new recompose replays with its marker."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "recompose_with_flags.json"
EPISODE = "ep-recompose-flags"
PATCH = "recompose-meta-flags"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _history(doc: dict, events: list[dict]) -> WorkflowHistory:
    return WorkflowHistory.from_json(doc["workflow_id"], {**doc["history"], "events": events})


async def _replay_failure(doc: dict, events: list[dict]) -> Exception | None:
    replayer = Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)
    return (await replayer.replay_workflow(_history(doc, events), raise_on_replay_failure=False)).replay_failure


def _payload(p: dict):
    return json.loads(base64.b64decode(p["data"])) if "data" in p else None


def _recompose_updates(events: list[dict]) -> list[tuple[str, list]]:
    out = []
    for e in events:
        request = e.get("workflowExecutionUpdateAcceptedEventAttributes", {}).get("acceptedRequest", {})
        if request.get("input", {}).get("name") == "recompose_shot":
            out.append((request["meta"]["updateId"], [_payload(p) for p in request["input"]["args"]["payloads"]]))
    return out


def _children(events: list[dict]) -> dict[str, dict]:
    """The child workflows started, by workflow id: their plan (second argument)."""
    out = {}
    for e in events:
        attrs = e.get("startChildWorkflowExecutionInitiatedEventAttributes")
        if attrs:
            out[attrs["workflowId"]] = _payload(attrs["input"]["payloads"][1])
    return out


def _patches(events: list[dict]) -> list[str]:
    return [json.loads(base64.b64decode(p["data"]))["id"] for e in events
            for p in e.get("markerRecordedEventAttributes", {}).get("details", {}).get("patch-data", {})
            .get("payloads", [])]


def test_fixture_is_a_recompose_before_the_deploy_then_one_with_flags():
    doc = _doc()
    events = doc["history"]["events"]
    parked, after = events[:doc["parked_events"]], events[doc["parked_events"]:]
    recompose_1, _, recompose_2, _ = doc["update_ids"]
    assert doc["old_worker_commit"].startswith("76d22d1") and doc["workflow_type"] == "EpisodeWorkflowV2"
    assert doc["patch"] == PATCH

    assert [u for u, _ in _recompose_updates(parked)] == [recompose_1]
    assert _recompose_updates(parked)[0][1][6] == {"count": 2}
    assert PATCH not in _patches(parked), "recorded before the patch"
    first = _children(parked)[f"{EPISODE}-shot-A01-recompose1"]
    assert first["shot_meta"]["count"] == 2 and "flags" not in first["shot_meta"]

    (update,) = _recompose_updates(after)
    assert update[0] == recompose_2 and update[1][6] == {"count": 1, "flags": doc["flags"]}
    assert PATCH in _patches(after)
    second = _children(after)[f"{EPISODE}-shot-A01-recompose2"]
    assert second["shot_meta"]["count"] == 1 and second["shot_meta"]["flags"] == doc["flags"]
    assert second["recompose"]["shot_meta_patch"] == {"count": 1, "flags": doc["flags"]}
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


async def test_the_parked_run_replays_on_this_code():
    doc = _doc()
    failure = await _replay_failure(doc, doc["history"]["events"][:doc["parked_events"]])
    assert failure is None, failure


async def test_the_whole_run_with_the_flags_recompose_replays():
    doc = _doc()
    failure = await _replay_failure(doc, doc["history"]["events"])
    assert failure is None, failure


async def test_replay_catches_a_changed_command():
    """The replay check can fail: the same history with the flags recompose's child recorded under
    another workflow id no longer matches what the workflow code starts."""
    doc = copy.deepcopy(_doc())
    for e in doc["history"]["events"]:
        attrs = e.get("startChildWorkflowExecutionInitiatedEventAttributes")
        if attrs and attrs["workflowId"] == f"{EPISODE}-shot-A01-recompose2":
            attrs["workflowId"] = f"{EPISODE}-shot-A01-recompose9"
    with pytest.raises(NondeterminismError):
        await Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter).replay_workflow(
            _history(doc, doc["history"]["events"]))


def test_flags_merge_over_the_planned_ones():
    base = {"count": 1, "flags": {"on_camera": False, "crowd": "few"}}
    assert EpisodeWorkflowV2.merged_recompose_flags(base, {"flags": {"on_camera": True}}) == {
        "on_camera": True, "crowd": "few"}
    assert EpisodeWorkflowV2.merged_recompose_flags({"count": 1}, {"flags": {"x": 1}}) == {"x": 1}
    assert EpisodeWorkflowV2.merged_recompose_flags(None, {"flags": {}}) == {}
