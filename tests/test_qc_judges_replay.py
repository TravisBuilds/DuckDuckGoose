"""Replay safety for the shots that are parked at still review when the QC judges deploy.

fixtures/replay/qc_still_review_parked.json is a real ShotWorkflow history recorded on main fd6d557
(before the QC judges) by scripts/capture_qc_review_history.py (local Temporal server, the dry-run
harness, real ffmpeg): the still review escalates (a live review without a judge), the shot records
the still as needs_review and waits for G2 with its 24 h review timer - the state of a run parked at
review at deploy time, the first ``parked_events`` events - then G2 is approved, the clip is made and
reviewed and the shot completes.

The judges' still rubric is built behind ``workflow.patched("qc-still-rubric")`` and the clip
rubric's shot_meta behind ``workflow.patched("qc-judges-clip-rubric")``. When the parked run's next
workflow task (the G2 approval) reaches a restarted worker, the worker replays the history with the
deployed code: the patches answer False for a history recorded without them, so the commands are
the ones recorded (an empty still rubric, a clip rubric without shot_meta)."""

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

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "qc_still_review_parked.json"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _replayer() -> Replayer:
    return Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)


def _scheduled(events: list[dict], activity: str) -> list[dict]:
    return [e for e in events if e.get("activityTaskScheduledEventAttributes", {})
            .get("activityType", {}).get("name") == activity]


def _args(event: dict) -> list:
    """The activity's arguments (a None argument is a payload without data)."""
    return [json.loads(base64.b64decode(p["data"])) if "data" in p else None
            for p in event["activityTaskScheduledEventAttributes"]["input"]["payloads"]]


def _result(events: list[dict], scheduled: dict):
    (done,) = [e["activityTaskCompletedEventAttributes"] for e in events
               if e.get("activityTaskCompletedEventAttributes", {}).get("scheduledEventId") == scheduled["eventId"]]
    return json.loads(base64.b64decode(done["result"]["payloads"][0]["data"]))


def _history(doc: dict, events: list[dict]) -> WorkflowHistory:
    return WorkflowHistory.from_json(doc["workflow_id"], {**doc["history"], "events": events})


def test_fixture_is_a_shot_parked_at_still_review_then_completed():
    doc = _doc()
    events = doc["history"]["events"]
    parked = events[:doc["parked_events"]]
    assert doc["captured_on_commit"].startswith("fd6d557") and doc["workflow_type"] == "ShotWorkflow"

    (review,) = _scheduled(parked, "review_still")
    assert _args(review)[1] == {}, "recorded before the judges: the still review got the empty rubric"
    assert _result(parked, review)["escalate"] is True
    (needs_review,) = [e for e in _scheduled(parked, "record_shot_result") if int(e["eventId"]) > int(review["eventId"])]
    assert _args(needs_review)[3] == "needs_review"
    assert parked[-1]["eventType"] == "EVENT_TYPE_TIMER_STARTED", "parked on the 24 h review timer"

    signal = events[doc["parked_events"]]
    assert signal["workflowExecutionSignaledEventAttributes"]["signalName"] == "stills_approved"
    (clip_review,) = _scheduled(events, "review_clip")
    assert "shot_meta" not in _args(clip_review)[1], "recorded before the judges: no shot_meta in the clip rubric"
    patches = [json.loads(base64.b64decode(p["data"]))["id"] for e in events
               for p in e.get("markerRecordedEventAttributes", {}).get("details", {}).get("patch-data", {})
               .get("payloads", [])]
    assert not {"qc-still-rubric", "qc-judges-clip-rubric"} & set(patches), patches
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


async def test_the_shot_parked_at_review_replays_on_this_code():
    doc = _doc()
    result = await _replayer().replay_workflow(_history(doc, doc["history"]["events"][:doc["parked_events"]]))
    assert result.replay_failure is None


async def test_the_whole_run_after_the_g2_approval_replays_on_this_code():
    doc = _doc()
    result = await _replayer().replay_workflow(_history(doc, doc["history"]["events"]))
    assert result.replay_failure is None


async def test_replay_catches_a_changed_command():
    """The replay check can fail: the same history with the still review recorded as another
    activity type no longer matches what the workflow code schedules."""
    doc = copy.deepcopy(_doc())
    (review,) = _scheduled(doc["history"]["events"], "review_still")
    review["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "review_clip"
    with pytest.raises(NondeterminismError):
        await _replayer().replay_workflow(_history(doc, doc["history"]["events"]))
