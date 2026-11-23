"""Replay safety for the runs at G2 when a judge's FAIL starts waiting at G2 (docs/QC-JUDGES.md).

fixtures/replay/g2_judge_fail_and_reapproved.json holds real histories recorded on main c6cf8c5
(before this change) by scripts/capture_g2_judge_fail_history.py (local Temporal server, the gate
tests' dry-run harness): A01's still fails the QC judge and its shot ends failed, as a judge's FAIL
did then; A02's still is rejected at G2 and then approved - a run like ep61opr at G2 when this
deploys, the first ``parked_events`` events - then A01 is retried, G2 is approved, both clips are made
and the run completes. ``failed_shot`` is the history of A01's failed shot.

The judge-FAIL wait is behind workflow.patched("qc-fail-parks-at-g2-v1"), the keep signal behind
"g2-keep-qc-failed-still-v1" and approve_g2_shots' kept rows behind "g2-approve-kept-rows-v1": a
history recorded without them replays with the commands it recorded."""

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

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "g2_judge_fail_and_reapproved.json"
NEW_PATCHES = {"qc-fail-parks-at-g2-v1", "g2-keep-qc-failed-still-v1", "g2-approve-kept-rows-v1"}


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _replayer() -> Replayer:
    return Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)


def _history(workflow_id: str, history: dict, events: list[dict]) -> WorkflowHistory:
    return WorkflowHistory.from_json(workflow_id, {**history, "events": events})


def _scheduled(events: list[dict], activity: str) -> list[dict]:
    return [e for e in events if e.get("activityTaskScheduledEventAttributes", {})
            .get("activityType", {}).get("name") == activity]


def _args(event: dict) -> list:
    return [json.loads(base64.b64decode(p["data"])) if "data" in p else None
            for p in event["activityTaskScheduledEventAttributes"]["input"]["payloads"]]


def _updates(events: list[dict]) -> list[str]:
    return [e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"]
            for e in events if "workflowExecutionUpdateAcceptedEventAttributes" in e]


def _patches(events: list[dict]) -> set[str]:
    return {json.loads(base64.b64decode(p["data"]))["id"] for e in events
            for p in e.get("markerRecordedEventAttributes", {}).get("details", {}).get("patch-data", {})
            .get("payloads", [])}


def test_fixture_is_a_judge_fail_that_ended_the_shot_and_a_reapproval_at_g2():
    doc = _doc()
    events = doc["history"]["events"]
    parked = events[:doc["parked_events"]]
    assert doc["captured_on_commit"].startswith("c6cf8c5") and doc["workflow_type"] == "EpisodeWorkflowV2"
    assert _updates(parked) == ["approve_g1_shot_plan", "reject_shot", "approve_shot"]
    assert _updates(events) == ["approve_g1_shot_plan", "reject_shot", "approve_shot", "retry_shot",
                                "approve_g2_scene_stills"]
    assert not _scheduled(parked, "approve_g2_shots"), "parked at G2: not approved yet"
    (approve,) = _scheduled(events, "approve_g2_shots")
    assert _args(approve) == ["ep-g2-judge-fail"], "as recorded on c6cf8c5: the episode id only"
    assert not NEW_PATCHES & _patches(events), _patches(events)
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"

    shot = doc["failed_shot"]["history"]["events"]
    (review,) = _scheduled(shot, "review_still")
    reviewed = next(e["activityTaskCompletedEventAttributes"] for e in shot
                    if e.get("activityTaskCompletedEventAttributes", {}).get("scheduledEventId") == review["eventId"])
    verdict = json.loads(base64.b64decode(reviewed["result"]["payloads"][0]["data"]))
    assert verdict["passed"] is False and verdict["escalate"] is False, verdict
    assert not _scheduled(shot, "submit_clip_job_enforced") and not NEW_PATCHES & _patches(shot)
    done = shot[-1]["workflowExecutionCompletedEventAttributes"]["result"]["payloads"][0]["data"]
    assert json.loads(base64.b64decode(done))["status"] == "failed", "a judge FAIL ended the shot then"


async def test_the_run_parked_at_g2_replays_on_this_code():
    doc = _doc()
    history = _history(doc["workflow_id"], doc["history"], doc["history"]["events"][:doc["parked_events"]])
    assert (await _replayer().replay_workflow(history)).replay_failure is None


async def test_the_whole_run_with_the_g2_approval_replays_on_this_code():
    doc = _doc()
    history = _history(doc["workflow_id"], doc["history"], doc["history"]["events"])
    assert (await _replayer().replay_workflow(history)).replay_failure is None


async def test_the_shot_a_judge_fail_ended_replays_on_this_code():
    shot = _doc()["failed_shot"]
    history = _history(shot["workflow_id"], shot["history"], shot["history"]["events"])
    assert (await _replayer().replay_workflow(history)).replay_failure is None


async def test_replay_catches_a_changed_command():
    """The replay check can fail: the same history with the G2 approval's DB write recorded as another
    activity type no longer matches what the workflow code schedules."""
    doc = copy.deepcopy(_doc())
    (approve,) = _scheduled(doc["history"]["events"], "approve_g2_shots")
    approve["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "record_shot_result"
    with pytest.raises(NondeterminismError):
        await _replayer().replay_workflow(_history(doc["workflow_id"], doc["history"], doc["history"]["events"]))
