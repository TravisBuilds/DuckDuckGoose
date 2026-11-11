"""Episode runs that have ended (failed, terminated, completed) on a real Temporal dev server.

A closed run takes no more signals or updates; the routes used to turn Temporal's NOT_FOUND into 500s
(the incident: after the revise crash, Approve and the story-outline save returned 500 and the page
showed stage "script" with no gate and no error). Now GET /api/episodes/{id} reports the run, every
action route refuses with a 409 that says how the run ended, and POST .../start-again starts a new run
with the same inputs.
"""

import os
import sys
from pathlib import Path

import aiosqlite
import pytest
from temporalio import activity, workflow

from tests.test_story_revise_live import (
    ADMIN_SECRET,
    FLAGGED_PAYOFF,
    fake_openai,
    live_activities,
    model_reply,
    start,
    studio,
    wait_for,
)

# Every other route that signals or updates the episode run (approve and the story-outline save are
# checked one by one), with requests that pass their own body validation
ACTION_ROUTES = [
    ("POST", "/assembly/accept", {"json": {"reason": "reviewed"}}),
    ("POST", "/assembly/rebuild", {"json": {"reason": "reviewed"}}),
    ("POST", "/delivery/accept", {"json": {"reason": "reviewed"}}),
    ("POST", "/delivery/rebuild", {"json": {"reason": "reviewed"}}),
    ("POST", "/final-cut/reject", {"json": {"reason": "pacing"}}),
    ("POST", "/raise-cap", {"json": {"new_cap": 900}}),
    ("POST", "/resync-plans", {"json": {"reason": "new shots.json"}}),
    ("POST", "/refs/generate", {"json": {"reason": "new refs", "keys": ["r_cat"]}}),
    ("POST", "/shots/S01/approve", {}),
    ("POST", "/shots/S01/reject", {"data": {"reason": "framing"}}),
    ("POST", "/shots/S01/retry", {}),
    ("POST", "/shots/S01/accept-clip", {"json": {"reason": "reviewed"}}),
    ("POST", "/shots/S01/select-still", {"json": {"version": 1, "reason": "better"}}),
    ("POST", "/shots/S01/retry-clip", {"json": {"reason": "motion"}}),
    ("POST", "/shots/S01/recompose", {"json": {"reason": "framing", "prompt": "a cat at the window",
                                               "ref_keys": ["r_cat"]}}),
    ("POST", "/shots/S01/recompose/decision", {"json": {"approve": True, "reason": "ok"}}),
    ("POST", "/shots/S01/recompose/grant", {"json": {"reason": "one more"}}),
    ("POST", "/shots/S01/adopt-reuse", {"json": {"reason": "same plate", "template_shot_id": "S02",
                                                 "shot": {"id": "S01"}}}),
    ("POST", "/shots/S01/restore-outcome", {"json": {"reason": "undo"}}),
    ("POST", "/shots/S01/edit", {"json": {"reason": "trim", "trim_in": 0.5}}),
]


async def run_inputs(client, workflow_id: str, run_id: str) -> list:
    async for event in client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history_events(page_size=1):
        payloads = event.workflow_execution_started_event_attributes.input.payloads
        return list(await client.data_converter.decode(payloads))
    raise AssertionError(f"no history for {workflow_id} run {run_id}")


async def audit_details(episode_id: str, action: str) -> list[str]:
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        cursor = await db.execute(
            "SELECT details FROM audit_log WHERE episode_id = ? AND action = ? ORDER BY id", (episode_id, action))
        return [row[0] for row in await cursor.fetchall()]


def ended(state: dict) -> bool:
    return bool(state.get("run") and state["run"]["ended"])


def story_log(*actions: str):
    return lambda body: [e["action"] for e in body.get("log") or []] == list(actions)


async def test_failed_run_reports_its_reason_refuses_every_action_and_starts_again(monkeypatch):
    """The incident: a live run fails in the revise; the page's next actions get a clear 409, and Start
    again runs the same inputs (this time the revise passes and approval works)."""
    ep = "ep85fail"
    revise_dry_runs: list = []

    @activity.defn(name="revise_story_shape_activity")
    async def revise_crashes_the_first_time(story_shape, critique, dry_run):
        revise_dry_runs.append(dry_run)
        if len(revise_dry_runs) == 1:
            raise TypeError("Object of type datetime is not JSON serializable")
        return {"story_shape": story_shape, "changes_made": ["kept"], "dry_run": dry_run}

    fake_openai(monkeypatch, model_reply(FLAGGED_PAYOFF))  # one live generation; the run started again reuses it
    async with studio(monkeypatch, live_activities(revise_crashes_the_first_time)) as (env, api):
        started = await start(api, ep)
        state = await wait_for(api, f"/api/episodes/{ep}", ended, "ended")
        reason = "revise_story_shape_activity: TypeError: Object of type datetime is not JSON serializable"
        assert state["run"] == {"run_id": started["run_id"], "status": "FAILED", "ended": True, "outcome": "failed",
                                "reason": reason, "dry_run": False, "can_start_again": True}
        assert (state["stage"], state["current_gate"]) == ("script", "")
        refused = f"This run has ended (failed: {reason}). Start it again."

        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert (res.status_code, res.json()["detail"]) == (409, refused)
        shaped = (await api.get(f"/api/episodes/{ep}/story-shape")).json()
        assert shaped["story_shape"]["payoff"] == FLAGGED_PAYOFF
        res = await api.put(f"/api/episodes/{ep}/story-shape", json=dict(shaped["story_shape"], logline="An edit"))
        assert (res.status_code, res.json()["detail"]) == (409, refused)
        answers = {}
        for method, path, request in ACTION_ROUTES:
            res = await api.request(method, f"/api/episodes/{ep}{path}", **request)
            answers[f"{method} {path}"] = (res.status_code, res.json().get("detail"))
        assert len(answers) == len(ACTION_ROUTES)
        assert {route: answer for route, answer in answers.items() if answer != (409, refused)} == {}

        res = await api.post(f"/api/episodes/{ep}/start-again")
        assert res.status_code == 200, res.text
        again = res.json()
        assert (again["from_run_id"], again["dry_run"]) == (started["run_id"], False)
        assert again["run_id"] not in ("", started["run_id"])
        inputs = await run_inputs(env.client, ep, started["run_id"])
        assert inputs[:3] == [ep, str(Path(os.environ["EPISODES_DIR"]) / ep / "BEATMAP.md"), False]
        assert await run_inputs(env.client, ep, again["run_id"]) == inputs
        [audited] = await audit_details(ep, "start_again")
        assert started["run_id"] in audited and again["run_id"] in audited and reason in audited

        await wait_for(api, f"/api/episodes/{ep}/story-shape", story_log("generated", "revised"), "revised")
        state = (await api.get(f"/api/episodes/{ep}")).json()
        assert state["run"] == {"run_id": again["run_id"], "status": "RUNNING", "ended": False, "outcome": None,
                                "reason": None, "dry_run": None, "can_start_again": False}
        res = await api.post(f"/api/episodes/{ep}/start-again")
        assert res.status_code == 409
        assert res.json()["detail"] == (f"{ep} is still running (RUNNING); "
                                        "only a run that has ended can be started again")
        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert res.status_code == 200, res.text
        assert revise_dry_runs == [False, False]


async def test_terminated_run_reports_the_reason_and_starts_again_after_the_file_checks(monkeypatch):
    from hfvg.worker import PRODUCTION_ACTIVITIES

    ep = "ep85stop"
    async with studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=False) as (env, api):
        started = await start(api, ep, live=False)
        await wait_for(api, f"/api/episodes/{ep}/story-shape", lambda b: bool(b.get("story_shape")), "shaped")
        await env.client.get_workflow_handle(ep).terminate(reason="stopped by the operator")

        state = await wait_for(api, f"/api/episodes/{ep}", ended, "ended")
        assert state["run"] == {"run_id": started["run_id"], "status": "TERMINATED", "ended": True,
                                "outcome": "terminated", "reason": "stopped by the operator", "dry_run": True,
                                "can_start_again": True}
        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert (res.status_code, res.json()["detail"]) == (
            409, "This run has ended (terminated: stopped by the operator). Start it again.")

        beatmap = Path(os.environ["EPISODES_DIR"]) / ep / "BEATMAP.md"
        content = beatmap.read_text(encoding="utf-8")
        beatmap.unlink()
        res = await api.post(f"/api/episodes/{ep}/start-again")
        assert (res.status_code, res.json()["detail"]) == (
            400, f"Refusing to start {ep} again: its beatmap {beatmap} is gone (upload BEATMAP.md again)")
        beatmap.write_text(content, encoding="utf-8")

        res = await api.post(f"/api/episodes/{ep}/start-again")
        assert res.status_code == 200, res.text
        assert (res.json()["dry_run"], res.json()["message"]) == (True, f"{ep} started again (dry_run=True)")
        await wait_for(api, f"/api/episodes/{ep}/story-shape", lambda b: bool(b.get("story_shape")), "shaped again")
        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert res.status_code == 200, res.text
        await wait_for(api, f"/api/episodes/{ep}", lambda s: s["approvals"]["story_shape"], "story approved")


async def test_run_that_refused_to_start_reads_as_failed(monkeypatch):
    """A live start without shots.json: the workflow returns status "failed" (Temporal says COMPLETED)."""
    from hfvg.worker import PRODUCTION_ACTIVITIES

    ep = "ep85nofiles"
    async with studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=True) as (env, api):
        started = await start(api, ep, live=True)
        state = await wait_for(api, f"/api/episodes/{ep}", ended, "ended")
        reason = "Missing required files in live mode: Activity task failed"
        assert state["run"] == {"run_id": started["run_id"], "status": "COMPLETED", "ended": True,
                                "outcome": "failed", "reason": reason, "dry_run": False, "can_start_again": True}
        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
        assert (res.status_code, res.json()["detail"]) == (409, f"This run has ended (failed: {reason}). Start it again.")


@workflow.defn(sandboxed=False)
class FinishedEpisode:
    """An episode run that has completed (the real one needs every gate approved first)."""

    def __init__(self) -> None:
        self.episode_id = ""

    @workflow.run
    async def run(self, episode_id: str) -> dict:
        self.episode_id = episode_id
        return {"episode_id": episode_id, "status": "complete"}

    @workflow.query
    def get_state(self) -> dict:
        return {"episode_id": self.episode_id, "stage": "delivery", "current_gate": "",
                "passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"],
                "approvals": {"g_final_cut": True}, "shots": 1}


async def test_completed_run_is_refused_and_cannot_be_started_again(monkeypatch):
    ep = "ep85done"
    async with studio(monkeypatch, [], live=False, workflows=[FinishedEpisode]) as (env, api):
        handle = await env.client.start_workflow(FinishedEpisode.run, ep, id=ep, task_queue="hfvg-tasks")
        await handle.result()

        state = (await api.get(f"/api/episodes/{ep}")).json()
        assert state["run"] == {"run_id": handle.result_run_id, "status": "COMPLETED", "ended": True,
                                "outcome": "completed", "reason": None, "dry_run": True, "can_start_again": False}
        assert state["passed_gates"][-1] == "G_FINAL_CUT"
        res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "G_FINAL_CUT"})
        assert (res.status_code, res.json()["detail"]) == (
            409, "This run has ended (completed). Reopen the episode to change it.")
        res = await api.post(f"/api/episodes/{ep}/start-again")
        assert (res.status_code, res.json()["detail"]) == (
            409, f"Cannot start {ep} again: it completed; reopen the episode to change it")
        res = await api.post("/api/episodes/ep85none/start-again")
        assert (res.status_code, res.json()["detail"]) == (404, "No workflow found for episode ep85none")


@pytest.mark.parametrize("run, detail", [
    ({"outcome": "failed", "reason": "parse_beatmap_activity: ShotDataError: no shots.json", "can_start_again": True},
     "This run has ended (failed: parse_beatmap_activity: ShotDataError: no shots.json). Start it again."),
    ({"outcome": "timed out", "reason": None, "can_start_again": True}, "This run has ended (timed out). Start it again."),
    ({"outcome": "completed", "reason": None, "can_start_again": False},
     "This run has ended (completed). Reopen the episode to change it."),
    # a reopened run, or one whose start inputs could not be read: start-again would refuse it
    ({"outcome": "failed", "reason": "boom", "can_start_again": False}, "This run has ended (failed: boom)."),
])
def test_the_409_only_offers_what_start_again_allows(monkeypatch, run, detail):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    sys.modules.pop("api.main", None)
    import api.main as api_main

    assert api_main._ended_run_detail(run) == detail


async def test_a_run_temporal_cannot_describe_is_never_refused(monkeypatch):
    """No describe (outage, an older server, a mocked client): no run view and no 409; the routes go on
    as before and report Temporal's own answer."""
    from unittest.mock import AsyncMock, MagicMock

    from temporalio.service import RPCError, RPCStatusCode

    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    sys.modules.pop("api.main", None)
    import api.main as api_main

    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    client = MagicMock()
    client.get_workflow_handle.return_value = handle
    monkeypatch.setattr(api_main, "temporal_client", client)
    assert await api_main._episode_run(handle) is None
    await api_main._refuse_ended_run("ep85x")
    handle.describe = AsyncMock(return_value=MagicMock())  # no WorkflowExecutionStatus
    assert await api_main._episode_run(handle) is None
    await api_main._refuse_ended_run("ep85x")
