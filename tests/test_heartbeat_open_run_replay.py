"""Replay safety for the runs that are open when the subprocess heartbeat fix deploys.

fixtures/replay/open_run_heartbeat_timeout.json is a real EpisodeWorkflowV2 history recorded on main
e1c5181 by scripts/capture_open_run_history.py (local Temporal server, real ffmpeg): mix_and_deliver
fails all 3 attempts with "activity Heartbeat timeout" and the run parks at G_FINAL_CUT with the
delivery NEEDS_REVIEW (the production state); then the delivery rebuild signal, the rebuilt delivery
(QC PASS), the G_FINAL_CUT approval and completion.

When the open production run's next workflow task (the rebuild signal) reaches a restarted worker, the
worker replays the run's whole history with the deployed workflow code before it schedules anything
new. The heartbeat fix is in activity code, which is never replayed: the rebuild's mix_and_deliver is a
new activity whose attempts run the new code, with the timeouts the workflow code gives it then."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "open_run_heartbeat_timeout.json"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _replayer() -> Replayer:
    return Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)


def _attrs(events: list[dict], kind: str) -> list[dict]:
    return [e[kind] for e in events if kind in e]


def _scheduled(events: list[dict], activity: str) -> list[dict]:
    return [e for e in events if e.get("activityTaskScheduledEventAttributes", {})
            .get("activityType", {}).get("name") == activity]


def test_fixture_is_the_production_failure_then_a_rebuild():
    events = _doc()["history"]["events"]
    first, rebuild = _scheduled(events, "mix_and_deliver")
    for e in (first, rebuild):
        opts = e["activityTaskScheduledEventAttributes"]
        assert (opts["startToCloseTimeout"], opts["heartbeatTimeout"]) == ("3600s", "120s"), opts
        assert opts["retryPolicy"]["maximumAttempts"] == 3, opts

    (timed_out,) = [t for t in _attrs(events, "activityTaskTimedOutEventAttributes")
                    if t["scheduledEventId"] == first["eventId"]]
    assert timed_out["failure"]["timeoutFailureInfo"]["timeoutType"] == "TIMEOUT_TYPE_HEARTBEAT", timed_out
    assert timed_out["retryState"] == "RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED", timed_out
    (started,) = [s for s in _attrs(events, "activityTaskStartedEventAttributes")
                  if s["scheduledEventId"] == first["eventId"]]
    assert started["attempt"] == 3, started

    signals = [s["signalName"] for s in _attrs(events, "workflowExecutionSignaledEventAttributes")]
    assert signals.index("request_delivery_rebuild") < signals.index("approve_g_final_cut"), signals
    assert int(first["eventId"]) < int(rebuild["eventId"])
    assert any(c["scheduledEventId"] == rebuild["eventId"]
               for c in _attrs(events, "activityTaskCompletedEventAttributes"))
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


async def test_open_run_history_replays_on_this_code():
    doc = _doc()
    result = await _replayer().replay_workflow(WorkflowHistory.from_json(doc["workflow_id"], doc["history"]))
    assert result.replay_failure is None


async def test_replay_catches_a_changed_command():
    """The replay check can fail: the same history with the rebuild's mix_and_deliver recorded as
    another activity type no longer matches what the workflow code schedules."""
    doc = copy.deepcopy(_doc())
    _, rebuild = _scheduled(doc["history"]["events"], "mix_and_deliver")
    rebuild["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "mix_audio"
    with pytest.raises(NondeterminismError):
        await _replayer().replay_workflow(WorkflowHistory.from_json(doc["workflow_id"], doc["history"]))
