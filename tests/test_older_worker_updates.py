"""A worker still on older code (mid deploy) rejects an update it has no handler for with the SDK's
"Update handler for '<name>' expected but not found" RuntimeError. Every route that sends such an
update answers 409 with only "The studio is updating. Try again in a minute." (never the raw error),
while a refusal by the workflow itself keeps its reason."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError

from tests.test_props_api import EP, _client, _running, studio  # noqa: F401

UPDATING = "The studio is updating. Try again in a minute."
ROUTES = [  # the update, how the route is called, and the prefix of a workflow refusal's 409
    ("approve_story_shape", "post", f"/api/episodes/{EP}/approve", {"gate_id": "story_shape"},
     "Story shape approval refused: "),
    ("approve_g1_shot_plan", "post", f"/api/episodes/{EP}/approve", {"gate_id": "g1_shot_plan"},
     "G1 approval refused: "),
    ("approve_g2_scene_stills", "post", f"/api/episodes/{EP}/approve", {"gate_id": "g2_scene_stills"},
     "G2 approval refused: "),
    ("update_story_shape", "put", f"/api/episodes/{EP}/story-shape", {"beats": []}, "Story shape update refused: "),
    ("retry_shot", "post", f"/api/episodes/{EP}/shots/A01/retry", None, ""),
]


async def _episode_workflows(*_a, **_k):
    yield SimpleNamespace(id=EP)


def _send(studio, method: str, path: str, body: dict | None, refusal: BaseException):  # noqa: F811
    _running(studio)
    studio.temporal_client.list_workflows = MagicMock(side_effect=_episode_workflows)
    studio.temporal_client.handle.execute_update = AsyncMock(side_effect=WorkflowUpdateFailedError(refusal))
    owner = _client(studio, "owner")
    return getattr(owner, method)(path, **({"json": body} if body is not None else {}))


@pytest.mark.parametrize("update,method,path,body,prefix", ROUTES, ids=[r[0] for r in ROUTES])
async def test_a_worker_without_the_update_answers_a_plain_409(studio, update, method, path, body, prefix):  # noqa: F811
    unknown = ApplicationError(f"Update handler for '{update}' expected but not found, and there is no dynamic "
                               "handler. known updates: [approve_story_shape replan_shots]", type="RuntimeError")
    res = _send(studio, method, path, body, unknown)
    assert (res.status_code, res.json()["detail"]) == (409, UPDATING)
    assert studio.temporal_client.handle.execute_update.call_args.args[0] == update


@pytest.mark.parametrize("update,method,path,body,prefix", ROUTES, ids=[r[0] for r in ROUTES])
async def test_a_refusal_by_the_workflow_keeps_its_reason(studio, update, method, path, body, prefix):  # noqa: F811
    reason = "Cannot do that now: a shot is still being retried"
    res = _send(studio, method, path, body, ApplicationError(reason, non_retryable=True))
    assert (res.status_code, res.json()["detail"]) == (409, prefix + reason)
    assert studio.temporal_client.handle.execute_update.call_args.args[0] == update
