"""Item 4: the voice check no longer happens only at the start.

- The create wizard's uploads (prompt kit, shots + refs, audio plan) are refused while the episode has
  a run in progress; only a reopened run (parked at the final cut) takes a new audio plan, and then
  every voice-over line must have a voice that run can use.
- /reopen and /reopen-from-saved re-run the voice check before starting the reopened run.
- A live run checks the voices again right after G1 approval (approve_g108_activity), before its first
  paid ref or still; workflow code itself reads nothing.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from hfvg import audio_mix
from tests.test_round4_shot_data import _shot

ADMIN = "voice-recheck-admin-secret-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
EP = "ep71vc"

LINES = [{"id": "L01", "at": 0.5, "text": "The kite is back."}, {"id": "L02", "at": 2.0, "text": "Hold on!"}]
VOICELESS = {"vo": {"lines": LINES}}
PREVIEW_VOICE = {"vo": {"voice_id": "dryrun-voice-maple", "lines": LINES}}
REAL_VOICE = {"vo": {"voice_id": "live-voice-1234", "lines": LINES}}

PROMPT_KIT = {"style": "paper cut-out, soft light", "characters": {}}
SHOTS = {"shots": [_shot("A01")]}
REFS = {"per_shot": {"A01": []}}


def _files(route: str, audio_plan: dict | None = None) -> dict:
    def part(name, data):
        return (name, io.BytesIO(json.dumps(data).encode()), "application/json")
    return {
        "upload-prompt-kit": {"prompt_kit_file": part("prompt_kit.json", PROMPT_KIT)},
        "upload-shots": {"shots_file": part("shots.json", SHOTS), "refs_file": part("refs.json", REFS)},
        "upload-audio-plan": {"audio_plan_file": part("audio_plan.json", audio_plan or REAL_VOICE)},
    }[route]


def _stored(route: str, ep: str = EP):
    return {"upload-prompt-kit": audio_mix.episode_dir(ep) / "prompt_kit.json",
            "upload-shots": audio_mix.episode_dir(ep) / "shots.json",
            "upload-audio-plan": audio_mix.audio_plan_path(ep)}[route]


ROUTES = ["upload-prompt-kit", "upload-shots", "upload-audio-plan"]


def _temporal(status="RUNNING", run_id="run-1", attrs=None, describe_exc=None, query_exc=None, snapshot=None):
    """A Temporal client whose episode workflow describes as `status` and answers reopen_snapshot."""
    client = MagicMock()
    handle = MagicMock(id=EP)
    if describe_exc is not None:
        handle.describe = AsyncMock(side_effect=describe_exc)
    else:
        handle.describe = AsyncMock(return_value=MagicMock(
            run_id=run_id, status=getattr(WorkflowExecutionStatus, status)))
    if query_exc is not None:
        handle.query = AsyncMock(side_effect=query_exc)
    else:
        handle.query = AsyncMock(return_value=snapshot if snapshot is not None else {"attrs": attrs or {}})
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.handle = handle
    client.start_workflow = AsyncMock(return_value=MagicMock(result_run_id="run-new"))
    return client


def _reopened(run_id: str, dry_run: bool) -> dict:
    return {"reopen_log": [{"reason": "Studio", "from_run_id": "run-0", "run_id": run_id}], "_dry_run": dry_run}


@pytest.fixture
async def api(monkeypatch):
    """The API with a studio DB that has the episode's row (an episode that has run records one)."""
    import os
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import create_episode, init_studio_db

    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    await create_episode(db, EP)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    return api_main


def _post(api_main, route: str, ep: str = EP, audio_plan: dict | None = None):
    from fastapi.testclient import TestClient
    return TestClient(api_main.app).post(f"/api/episodes/{ep}/{route}", headers=AUTH,
                                         files=_files(route, audio_plan))


def _seed(route: str, ep: str = EP) -> bytes:
    path = _stored(route, ep)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'{"before": "the run started"}')
    return path.read_bytes()


# ------------------------------------------------------------------ uploads while a run is open
@pytest.mark.parametrize("route", ROUTES)
async def test_uploads_refused_while_the_episode_runs(api, route):
    api.temporal_client = _temporal("RUNNING", attrs={"reopen_log": [], "_dry_run": False})
    before = _seed(route)
    res = _post(api, route)
    assert res.status_code == 409, res.text
    assert f"Episode {EP} has a run in progress (run run-1)" in res.json()["detail"]
    assert _stored(route).read_bytes() == before


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "TERMINATED", "CANCELED", "TIMED_OUT", "gone"])
async def test_uploads_accepted_once_the_run_has_ended(api, route, status):
    api.temporal_client = (_temporal(describe_exc=RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
                           if status == "gone" else _temporal(status))
    _seed(route)
    res = _post(api, route)
    assert res.status_code == 200, res.text
    assert b"the run started" not in _stored(route).read_bytes()


@pytest.mark.parametrize("route", ["upload-prompt-kit", "upload-shots"])
async def test_reopened_run_keeps_the_shot_files_and_prompt_kit(api, route):
    api.temporal_client = _temporal("RUNNING", run_id="run-2", attrs=_reopened("run-2", dry_run=False))
    before = _seed(route)
    res = _post(api, route)
    assert res.status_code == 409, res.text
    assert "cannot change until the run ends" in res.json()["detail"]
    assert "reopened" not in res.json()["detail"]
    assert _stored(route).read_bytes() == before


async def test_reopened_live_run_takes_an_audio_plan_only_with_real_voices(api):
    api.temporal_client = _temporal("RUNNING", run_id="run-2", attrs=_reopened("run-2", dry_run=False))
    before = _seed("upload-audio-plan")
    res = _post(api, "upload-audio-plan", audio_plan=VOICELESS)
    assert res.status_code == 400, res.text
    assert res.json()["detail"].startswith(f"Refusing the audio plan of reopened {EP}: "
                                           "no voice chosen for the voice-over lines L01, L02")
    res = _post(api, "upload-audio-plan", audio_plan=PREVIEW_VOICE)
    assert res.status_code == 400, res.text
    assert "dry-run preview voices cannot speak in a live run" in res.json()["detail"]
    assert _stored("upload-audio-plan").read_bytes() == before
    res = _post(api, "upload-audio-plan", audio_plan=REAL_VOICE)
    assert res.status_code == 200, res.text
    assert json.loads(_stored("upload-audio-plan").read_text()) == REAL_VOICE


async def test_reopened_dry_run_takes_preview_voices_but_not_voiceless_lines(api):
    api.temporal_client = _temporal("RUNNING", run_id="run-2", attrs=_reopened("run-2", dry_run=True))
    assert _post(api, "upload-audio-plan", audio_plan=PREVIEW_VOICE).status_code == 200
    res = _post(api, "upload-audio-plan", audio_plan=VOICELESS)
    assert res.status_code == 400 and "no voice chosen" in res.json()["detail"]
    assert json.loads(_stored("upload-audio-plan").read_text()) == PREVIEW_VOICE


async def test_default_voice_speaks_voiceless_lines_in_a_reopened_run(api, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DEFAULT_VOICE_ID", "live-default-voice")
    api.temporal_client = _temporal("RUNNING", run_id="run-2", attrs=_reopened("run-2", dry_run=False))
    assert _post(api, "upload-audio-plan", audio_plan=VOICELESS).status_code == 200


@pytest.mark.parametrize("why,client", [
    ("an earlier run was the reopened one",
     lambda: _temporal("RUNNING", run_id="run-3", attrs=_reopened("run-2", dry_run=False))),
    ("the run cannot answer the snapshot query",
     lambda: _temporal("RUNNING", run_id="run-2", query_exc=RuntimeError("no worker"))),
])
async def test_audio_plan_refused_unless_the_open_run_itself_was_reopened(api, why, client):
    api.temporal_client = client()
    before = _seed("upload-audio-plan")
    res = _post(api, "upload-audio-plan", audio_plan=REAL_VOICE)
    assert res.status_code == 409, (why, res.text)
    assert "or the episode is reopened at the final cut" in res.json()["detail"]
    assert _stored("upload-audio-plan").read_bytes() == before


@pytest.mark.parametrize("route", ROUTES)
async def test_temporal_errors_fail_closed(api, route):
    api.temporal_client = _temporal(describe_exc=RPCError("down", RPCStatusCode.UNAVAILABLE, b""))
    before = _seed(route)
    res = _post(api, route)
    assert res.status_code == 503, res.text
    assert _stored(route).read_bytes() == before


@pytest.mark.parametrize("route", ROUTES)
async def test_without_temporal_a_held_run_lock_refuses(api, route):
    import os
    api.temporal_client = None
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO run_locks (episode_id, workflow_id, locked_at) VALUES (?, ?, ?)",
                  (EP, EP, "2026-11-11T10:00:00+00:00"))
    before = _seed(route)
    res = _post(api, route)
    assert res.status_code == 503, res.text
    assert "holds the run lock" in res.json()["detail"]
    assert _stored(route).read_bytes() == before
    with sqlite3.connect(db) as c:
        c.execute("DELETE FROM run_locks")
    assert _post(api, route).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
async def test_an_episode_that_never_ran_does_not_ask_temporal(api, route):
    api.temporal_client = _temporal("RUNNING")
    res = _post(api, route, ep="ep72new")
    assert res.status_code == 200, res.text
    api.temporal_client.handle.describe.assert_not_called()


# ------------------------------------------------------------------ reopen re-checks the voices
def _snapshot(dry_run: bool) -> dict:
    return {"version": 1, "episode_id": EP, "state": {"pending_retries": {}},
            "attrs": {"passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"],
                      "approved_g_final_cut": True, "pending_clip_retries": {}, "pending_recompose": {},
                      "_dry_run": dry_run, "_beatmap_path": None}}


def _write_plan(plan: dict) -> None:
    path = audio_mix.audio_plan_path(EP)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan))


NO_VOICE = "no voice chosen for the voice-over lines L01, L02"
CASES = [  # (run mode, plan, refused with)
    (False, VOICELESS, NO_VOICE),
    (False, PREVIEW_VOICE, f"{NO_VOICE} (dry-run preview voices cannot speak in a live run)"),
    (True, VOICELESS, NO_VOICE),
    (False, REAL_VOICE, None),
    (True, PREVIEW_VOICE, None),
    (False, None, None),
]


@pytest.mark.parametrize("dry_run,plan,refused", CASES)
async def test_reopen_rechecks_the_voices(api, dry_run, plan, refused):
    from fastapi.testclient import TestClient
    if plan is not None:
        _write_plan(plan)
    api.temporal_client = _temporal("COMPLETED", run_id="run-1", snapshot=_snapshot(dry_run))
    res = TestClient(api.app).post(f"/api/episodes/{EP}/reopen", headers=AUTH, json={"reason": "Studio"})
    if refused:
        assert res.status_code == 409, res.text
        assert res.json()["detail"].startswith(f"Cannot reopen {EP}: {refused}.")
        api.temporal_client.start_workflow.assert_not_called()
    else:
        assert res.status_code == 200, res.text
        assert api.temporal_client.start_workflow.call_args.kwargs["args"][2] is dry_run


@pytest.mark.parametrize("dry_run,plan,refused", CASES)
async def test_reopen_from_saved_rechecks_the_voices(api, dry_run, plan, refused):
    from fastapi.testclient import TestClient
    from hfvg.saved_state import write_snapshot
    if plan is not None:
        _write_plan(plan)
    write_snapshot(audio_mix.episode_dir(EP), _snapshot(dry_run), "2026-11-11 10:00:00")
    api.temporal_client = _temporal(describe_exc=RPCError("purged", RPCStatusCode.NOT_FOUND, b""))
    res = TestClient(api.app).post(f"/api/episodes/{EP}/reopen-from-saved", headers=AUTH,
                                   json={"reason": "Studio"})
    if refused:
        assert res.status_code == 409, res.text
        assert res.json()["detail"].startswith(f"Cannot reopen {EP}: {refused}.")
        api.temporal_client.start_workflow.assert_not_called()
    else:
        assert res.status_code == 200, res.text
        assert res.json()["source"] == "persisted"
        assert api.temporal_client.start_workflow.call_args.kwargs["args"][2] is dry_run


# ------------------------------------------------------------------ before the first paid activity
async def _g108_approved(db: str) -> int:
    with sqlite3.connect(db) as c:
        return c.execute("SELECT g108_approved FROM episodes WHERE episode_id = ?", (EP,)).fetchone()[0]


@pytest.mark.parametrize("plan,why", [
    (VOICELESS, NO_VOICE),
    (PREVIEW_VOICE, f"{NO_VOICE} (dry-run preview voices cannot speak in a live run)"),
])
async def test_g108_activity_refuses_a_plan_a_live_run_cannot_voice(api, plan, why):
    import os
    from hfvg.activities.workflow_support import approve_g108_activity
    _write_plan(plan)
    with pytest.raises(ApplicationError) as err:
        await approve_g108_activity(EP)
    assert err.value.type == "VoiceCheckFailed" and err.value.non_retryable
    assert err.value.message.startswith(f"Refusing to spend on {EP}: {why}.")
    assert await _g108_approved(os.environ["DATABASE_PATH"]) == 0


@pytest.mark.parametrize("plan", [REAL_VOICE, None])
async def test_g108_activity_approves_a_plan_with_real_voices(api, plan):
    import os
    from hfvg.activities.workflow_support import approve_g108_activity
    if plan is not None:
        _write_plan(plan)
    await approve_g108_activity(EP)
    assert await _g108_approved(os.environ["DATABASE_PATH"]) == 1


def _live_run_activities(spent: list):
    """Every activity of a live run up to its first ref stubbed, except the real approve_g108_activity."""
    from temporalio import activity
    from hfvg.activities.workflow_support import approve_g108_activity

    def stub(name, result=None):
        async def run(*_a):
            return result if result is not None else {"ok": True, "valid": True, "errors": [], "warnings": []}
        return activity.defn(name=name)(run)

    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_lines(*_a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}

    @activity.defn(name="generate_ref_activity")
    async def generate_ref(*_a):
        spent.append("ref")
        return {"status": "completed", "credits_used": 1}

    g1_plan = {"shots": [], "total_still_credits": 0, "total_clip_credits": 0, "total_credits": 0,
               "total_still_usd": "0.00", "total_clip_usd": "0.00", "total_usd": "0.00",
               "estimates_available": True}
    ref = {"key": "kite", "prompt": "a paper kite", "output_path": "refs/kite.png", "version": 1}
    return [
        stub("verify_episode_files_activity"), stub("acquire_run_lock", True), stub("release_run_lock", True),
        stub("upsert_episode_activity"), stub("init_episode_credit_line_activity"), budget_lines,
        stub("load_gate_policy_activity", {}), stub("preflight_validate_episode_activity"),
        stub("parse_beatmap_activity", {"shots": [{"shot_id": "A01", "prompt": "a kite", "duration": 4}]}),
        stub("compute_g1_plan_activity", g1_plan), stub("update_credit_cap_activity"),
        stub("load_refs_to_generate_activity", [ref]), generate_ref, approve_g108_activity,
    ]


async def _live_run_to_g1(env, spent: list, task_queue: str):
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

    worker = Worker(env.client, task_queue=task_queue, workflows=[EpisodeWorkflowV2],
                    activities=_live_run_activities(spent))
    handle = await env.client.start_workflow(EpisodeWorkflowV2.run, args=[EP, None, False, ["A01"], 40],
                                             id=f"{EP}-{task_queue}", task_queue=task_queue)
    return worker, handle, EpisodeWorkflowV2


async def _wait_for_gate(handle, gate: str, timeout: float = 20.0) -> None:
    import asyncio
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        try:
            if (await handle.query("get_state"))["current_gate"] == gate:
                return
        except Exception:
            pass
        await asyncio.sleep(0.2)
    raise AssertionError(f"never reached {gate}")


def _scheduled(history) -> list[str]:
    return [e.activity_task_scheduled_event_attributes.activity_type.name for e in history.events
            if e.HasField("activity_task_scheduled_event_attributes")]


@pytest.mark.timeout(120)
async def test_live_run_stops_before_its_first_paid_ref_when_the_plan_cannot_be_voiced(api):
    import os
    from temporalio.exceptions import ActivityError
    from temporalio.testing import WorkflowEnvironment

    _write_plan(VOICELESS)
    spent: list = []
    async with await WorkflowEnvironment.start_local() as env:
        worker, handle, W = await _live_run_to_g1(env, spent, "voice-recheck-refused")
        async with worker:
            await _wait_for_gate(handle, "G1_SHOT_PLAN")
            await handle.execute_update(W.approve_g1_shot_plan)
            with pytest.raises(WorkflowFailureError) as failed:
                await handle.result()
            history = await handle.fetch_history()
    activity_error = failed.value.cause
    assert isinstance(activity_error, ActivityError)
    assert isinstance(activity_error.cause, ApplicationError)
    assert activity_error.cause.type == "VoiceCheckFailed"
    assert activity_error.cause.message.startswith(f"Refusing to spend on {EP}: {NO_VOICE}.")
    scheduled = _scheduled(history)
    assert "approve_g108_activity" in scheduled and "release_run_lock" in scheduled
    assert "load_refs_to_generate_activity" not in scheduled and "generate_ref_activity" not in scheduled
    assert not [e for e in history.events if e.HasField("start_child_workflow_execution_initiated_event_attributes")]
    assert spent == []
    assert await _g108_approved(os.environ["DATABASE_PATH"]) == 0


@pytest.mark.timeout(120)
async def test_live_run_with_real_voices_goes_on_to_its_refs(api):
    import asyncio
    import contextlib
    import os
    from temporalio.testing import WorkflowEnvironment

    _write_plan(REAL_VOICE)
    spent: list = []
    async with await WorkflowEnvironment.start_local() as env:
        worker, handle, W = await _live_run_to_g1(env, spent, "voice-recheck-voiced")
        async with worker:
            await _wait_for_gate(handle, "G1_SHOT_PLAN")
            await handle.execute_update(W.approve_g1_shot_plan)
            for _ in range(100):
                if spent:
                    break
                await asyncio.sleep(0.2)
            with contextlib.suppress(Exception):  # past its ref the stubbed run may already have stopped
                await handle.terminate("test done")
    assert spent == ["ref"]
    assert await _g108_approved(os.environ["DATABASE_PATH"]) == 1
