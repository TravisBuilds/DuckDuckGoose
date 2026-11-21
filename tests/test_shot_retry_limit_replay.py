"""Replay safety for the episodes parked at G2 with a refused still retry when MAX_SHOT_RETRIES is
raised.

fixtures/replay/shot_retry_refused_then_limit_raised.json is a real EpisodeWorkflowV2 history recorded
by scripts/capture_shot_retry_limit_history.py (local Temporal server, the dry-run harness, real
ShotWorkflow children, real ffmpeg). A worker on main c62f654 (before the retry_shot validator,
MAX_SHOT_RETRIES=1) accepted retry_shot A01 once and refused the next one in the handler, "Max retries
(1) exceeded for shot A01", which the history records: the run is parked at G2 (the first
``parked_events`` events, the production state). Then the deploy: a worker on the validator code with
MAX_SHOT_RETRIES=2 accepted retry_shot A01 again (retry2) and its validator refused the next one (not
recorded); G2 approved, the clip made, the final cut approved, completed.

The history does not hold the limit a refusal applied. The handler replays an update recorded without
its patch marker against LEGACY_MAX_SHOT_RETRIES (the old workers' limit, 1), and an update recorded
with its marker without any limit, so the value of MAX_SHOT_RETRIES never changes a replay. The old
code replayed the recorded refusal against MAX_SHOT_RETRIES: raised to 2 it accepted the update instead
-> NondeterminismError (the ops finding, reproduced last with the legacy limit set to 2)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer
from temporalio.workflow import NondeterminismError

from hfvg.config import config
from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS
from hfvg.workflows.episode_v2 import RETRY_SHOT_LIMIT_PATCH

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "shot_retry_refused_then_limit_raised.json"
EPISODE = "ep-retry-limit"


def _doc() -> dict:
    return json.loads(FIXTURE.read_text())


def _history(doc: dict, events: list[dict]) -> WorkflowHistory:
    return WorkflowHistory.from_json(doc["workflow_id"], {**doc["history"], "events": events})


async def _replay_failure(doc: dict, events: list[dict]) -> Exception | None:
    replayer = Replayer(workflows=PRODUCTION_WORKFLOWS, data_converter=temporal_data_converter)
    return (await replayer.replay_workflow(_history(doc, events), raise_on_replay_failure=False)).replay_failure


def _retry_updates(events: list[dict]) -> list[str]:
    return [e["workflowExecutionUpdateAcceptedEventAttributes"]["acceptedRequest"]["meta"]["updateId"]
            for e in events if e.get("workflowExecutionUpdateAcceptedEventAttributes", {})
            .get("acceptedRequest", {}).get("input", {}).get("name") == "retry_shot"]


def _update_failure(events: list[dict], update_id: str) -> str | None:
    (outcome,) = [e["workflowExecutionUpdateCompletedEventAttributes"]["outcome"] for e in events
                  if e.get("workflowExecutionUpdateCompletedEventAttributes", {}).get("meta", {})
                  .get("updateId") == update_id]
    return outcome["failure"]["message"] if "failure" in outcome else None


def _children(events: list[dict]) -> list[str]:
    return [e["startChildWorkflowExecutionInitiatedEventAttributes"]["workflowId"] for e in events
            if "startChildWorkflowExecutionInitiatedEventAttributes" in e]


def _patches(events: list[dict]) -> list[str]:
    return [json.loads(base64.b64decode(p["data"]))["id"] for e in events
            for p in e.get("markerRecordedEventAttributes", {}).get("details", {}).get("patch-data", {})
            .get("payloads", [])]


def test_fixture_is_a_refused_retry_at_limit_1_then_a_retry_at_the_raised_limit():
    doc = _doc()
    events = doc["history"]["events"]
    parked, after = events[:doc["parked_events"]], events[doc["parked_events"]:]
    retry_1, refused_at_1, retry_2, refused_at_2 = doc["retry_update_ids"]
    assert doc["old_worker_commit"].startswith("c62f654") and doc["workflow_type"] == "EpisodeWorkflowV2"

    assert _retry_updates(parked) == [retry_1, refused_at_1]
    assert _update_failure(parked, retry_1) is None
    assert _update_failure(parked, refused_at_1) == "Max retries (1) exceeded for shot A01"
    assert parked[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_COMPLETED", "parked on the refusal"
    assert _children(parked) == [f"{EPISODE}-shot-A01", f"{EPISODE}-shot-A01-retry1"]
    assert not [p for p in _patches(parked) if p.startswith(RETRY_SHOT_LIMIT_PATCH)], "recorded before the validator"

    assert _retry_updates(after) == [retry_2] and _update_failure(after, retry_2) is None
    assert f"{RETRY_SHOT_LIMIT_PATCH}:{retry_2}" in _patches(after)
    assert _children(after) == [f"{EPISODE}-shot-A01-retry2"]
    assert refused_at_2 not in json.dumps(doc["history"]), "a validator refusal is not recorded"
    assert events[-1]["eventType"] == "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED"


@pytest.mark.parametrize("limit", [2, 5, 1])
async def test_the_parked_run_replays_with_the_raised_limit(monkeypatch, limit):
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", limit)
    doc = _doc()
    failure = await _replay_failure(doc, doc["history"]["events"][:doc["parked_events"]])
    assert failure is None, failure


@pytest.mark.parametrize("limit", [2, 5, 1])
async def test_the_whole_run_with_the_retry_after_the_deploy_replays(monkeypatch, limit):
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", limit)
    doc = _doc()
    failure = await _replay_failure(doc, doc["history"]["events"])
    assert failure is None, failure


async def test_the_recorded_refusal_replays_only_against_the_old_workers_limit(monkeypatch):
    """The replay check can fail: replayed against a limit of 2 (what the old code did when
    MAX_SHOT_RETRIES was raised), the refused update is accepted and starts a child the history does
    not have."""
    monkeypatch.setattr(config, "LEGACY_MAX_SHOT_RETRIES", 2)
    doc = _doc()
    failure = await _replay_failure(doc, doc["history"]["events"][:doc["parked_events"]])
    assert isinstance(failure, NondeterminismError), failure
