"""Replay safety for a G2 resync that takes a revised cast lock (docs/CAST.md, "Revising a locked look").

fixtures/replay/cast_revision_resync_refused.json holds a real history recorded on main c6cf8c5 (before
this change) by scripts/capture_cast_revision_resync_history.py (local Temporal server, the gate tests'
dry-run harness): A01 has a locked cast character and is approved at G2; the locked words are rewritten
and resync_shot_plans is refused, as a changed shot_meta was then - a run like ep61opr at G2 when this
deploys, the first ``parked_events`` events - then G2 is approved, both clips are made and the run
completes. Its second parse changed only the locked words, so this code reaches the new branch on replay.

Taking the revised words is behind workflow.patched("g2-resync-cast-revision-v1:<update id>"): a history
recorded without that marker replays the refusal it recorded (tests/test_cast_lock_revision_resync.py
replays one recorded with it)."""

from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Interceptor, Replayer, UnsandboxedWorkflowRunner, WorkflowInboundInterceptor
from temporalio.workflow import NondeterminismError

from hfvg.plan_resync import plan_resync
from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS
from hfvg.workflows.episode_v2 import CAST_REVISION_RESYNC_PATCH

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "cast_revision_resync_refused.json"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _replayer() -> Replayer:
    return Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)


def _history(doc: dict, events: list[dict]) -> WorkflowHistory:
    return WorkflowHistory.from_json(doc["workflow_id"], {**doc["history"], "events": events})


def _decode(payload: dict):
    return json.loads(base64.b64decode(payload["data"])) if "data" in payload else None


def _scheduled(events: list[dict], activity: str) -> list[dict]:
    return [e for e in events if e.get("activityTaskScheduledEventAttributes", {})
            .get("activityType", {}).get("name") == activity]


def _results(events: list[dict], activity: str) -> list:
    ids = {e["eventId"] for e in _scheduled(events, activity)}
    return [_decode(e["activityTaskCompletedEventAttributes"]["result"]["payloads"][0]) for e in events
            if e.get("activityTaskCompletedEventAttributes", {}).get("scheduledEventId") in ids]


def _updates(events: list[dict]) -> list[str]:
    return [e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["input"]["name"]
            for e in events if "workflowExecutionUpdateAcceptedEventAttributes" in e]


def _patches(events: list[dict]) -> set[str]:
    return {_decode(p)["id"] for e in events
            for p in e.get("markerRecordedEventAttributes", {}).get("details", {}).get("patch-data", {})
            .get("payloads", [])}


def test_fixture_is_a_refused_resync_of_revised_cast_words_at_g2():
    doc = _doc()
    events = doc["history"]["events"]
    parked = events[:doc["parked_events"]]
    assert doc["captured_on_commit"].startswith("c6cf8c5") and doc["workflow_type"] == "EpisodeWorkflowV2"
    assert _updates(parked) == ["approve_g1_shot_plan", "approve_shot", "resync_shot_plans"]
    assert _updates(events) == ["approve_g1_shot_plan", "approve_shot", "resync_shot_plans", "approve_g2_scene_stills"]
    (refused,) = [e["workflowExecutionUpdateCompletedEventAttributes"]["outcome"]["failure"]["message"]
                  for e in parked if "failure" in e.get("workflowExecutionUpdateCompletedEventAttributes", {})
                  .get("outcome", {})]
    assert refused == doc["refusal"] == "Cannot resync shot plans: A01: shot_meta changed (structural; needs a new G1 plan)"
    assert not _scheduled(parked, "approve_g2_shots"), "parked at G2: not approved yet"
    assert not any(p.startswith(CAST_REVISION_RESYNC_PATCH) for p in _patches(events)), _patches(events)
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"

    # the re-parse changed only the locked words: this code takes them when the marker is there
    first, again = (r["shots"] for r in _results(parked, "parse_beatmap_activity"))
    assert [c["clause"] for c in first[0]["shot_meta"]["cast"]] == [doc["words"]["before"]]
    assert [c["clause"] for c in again[0]["shot_meta"]["cast"]] == [doc["words"]["after"]]
    assert plan_resync(first, again)["problems"] == ["A01: shot_meta changed (structural; needs a new G1 plan)"]
    taken = plan_resync(first, again, cast_revisions=True)
    assert taken["problems"] == [] and taken["cast_revisions"]["A01"][0]["after"] == doc["words"]["after"]


async def test_the_run_parked_at_g2_after_the_refusal_replays_on_this_code():
    doc = _doc()
    history = _history(doc, doc["history"]["events"][:doc["parked_events"]])
    assert (await _replayer().replay_workflow(history)).replay_failure is None


async def test_the_whole_run_replays_on_this_code():
    doc = _doc()
    assert (await _replayer().replay_workflow(_history(doc, doc["history"]["events"]))).replay_failure is None


class _UpdateOutcomes(Interceptor):
    """Each update handler's outcome while a history replays: (name, None) or (name, its error)."""

    def __init__(self):
        self.outcomes: list[tuple[str, str | None]] = []

    def workflow_interceptor_class(self, input):
        outcomes = self.outcomes

        class Inbound(WorkflowInboundInterceptor):
            async def handle_update_handler(self, input):
                try:
                    result = await super().handle_update_handler(input)
                except Exception as e:
                    outcomes.append((input.update, str(e)))
                    raise
                outcomes.append((input.update, None))
                return result

        return Inbound


async def test_on_replay_the_refused_resync_is_refused_again():
    """Replay does not compare an update's outcome (the resync taken instead of refused replays without
    a failure too), so the run's state is only the one it had if the handler refuses it again."""
    doc = _doc()
    seen = _UpdateOutcomes()
    replayer = Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter,
                        interceptors=[seen], workflow_runner=UnsandboxedWorkflowRunner())
    assert (await replayer.replay_workflow(_history(doc, doc["history"]["events"]))).replay_failure is None
    assert seen.outcomes == [("approve_g1_shot_plan", None), ("approve_shot", None),
                             ("resync_shot_plans", doc["refusal"]), ("approve_g2_scene_stills", None)]


async def test_replay_catches_a_changed_command():
    """The replay check can fail: the same history with the G2 approval's DB write recorded as another
    activity type no longer matches what the workflow code schedules."""
    doc = copy.deepcopy(_doc())
    (approve,) = _scheduled(doc["history"]["events"], "approve_g2_shots")
    approve["activityTaskScheduledEventAttributes"]["activityType"]["name"] = "record_shot_result"
    with pytest.raises(NondeterminismError):
        await _replayer().replay_workflow(_history(doc, doc["history"]["events"]))
