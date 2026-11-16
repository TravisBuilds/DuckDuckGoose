"""A finished video stays visible once Temporal has purged its run.

The dev server's retention deletes a closed run's history 24 h after it ends; GET /api/episodes/{id} then
answered 404 "No workflow found" and the episode page showed "This video hasn't started yet" with the
delivered files never shown. Now the API answers with the approved final cut from the saved state
(<episode>/state/reopen_snapshot.json, or a rebuild from the saved artifacts), or says the run's history
expired when no current saved state shows how it ended; an episode that never ran is still a 404.
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment

from hfvg.saved_state import (SNAPSHOT_REL, archived_state, changed_since, finished_problem, load_archive_rows,
                              read_saved, write_snapshot)
from tests.lint_helpers import lint_ok
from tests.test_audio_delivery_workflow import PROMPT, write_audio
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, ClipFactory, _to_final_cut, _worker, needs_ffmpeg, no_counter  # noqa: F401
from tests.test_r13_gap79_reopen_from_saved import AUDIT, EP, PARSED, _fixture
from tests.test_story_revise_live import ADMIN_SECRET, start, studio, wait_for

FINISHED_RUN = {"status": "ARCHIVED", "ended": True, "outcome": "completed", "reason": None,
                "can_start_again": False, "archived": True}


def _utc(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def approved_final_cut(snapshot: dict, delivery_paths: dict | None = None) -> dict:
    """A workflow snapshot as the periodic saver stores it once the final cut is approved."""
    snap = {**snapshot, "state": dict(snapshot["state"]), "attrs": dict(snapshot["attrs"])}
    snap["attrs"].update({
        "passed_gates": ["STORY_SHAPE", "G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"],
        "approved_story_shape": True, "approved_g1_shot_plan": True, "approved_g2_scene_stills": True,
        "approved_g_final_cut": True, "pending_clip_retries": [], "pending_recompose": {},
        "shot_outcomes": {"S01": {"shot_id": "S01", "status": "completed", "still_url": "/x/still_v1.png",
                                  "clip_url": None, "clip_path": "x/clip_v1.mp4"}},
        "clip_retries": {"S01": 1}, "assembly_version": 1, "delivery_version": 1, "delivery_phase": "done",
        "final_cut_rejections": [{"reason": "a slower ending"}], "_dry_run": True,
    })
    snap["state"].update({
        "stage": "mute_edit", "current_gate": "G_FINAL_CUT", "pending_retries": {},
        "shot_decisions": {"S01": "approved"}, "assembly_path": "/x/mute_assembly.mp4",
        "assembly_qc": {"verdict": "DRY_RUN", "blocking": False},
        "delivery_paths": dict(delivery_paths or {}), "delivery_qc": {"verdict": "DRY_RUN", "blocking": False},
    })
    return snap


def deliver(episode_id: str) -> dict:
    """The delivery files a finished run leaves (their bytes do not matter to the API)."""
    from hfvg.audio_mix import delivery_paths
    paths = delivery_paths(episode_id)
    for key in ("master_subs", "nosubs", "phone", "srt"):
        Path(paths[key]).parent.mkdir(parents=True, exist_ok=True)
        Path(paths[key]).write_bytes(b"delivered " + key.encode())
    return paths


async def purge(env, episode_id: str) -> None:
    """Delete the episode's run and its shot runs from Temporal, as the retention does 24 h after a run
    closes (an open run is deleted at once; a closed one only after the server's ack interval)."""
    from temporalio.api.common.v1 import WorkflowExecution
    from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
    ids = [episode_id] + [wf.id async for wf in env.client.list_workflows(f'WorkflowId STARTS_WITH "{episode_id}-"')]
    for wid in ids:
        await env.client.workflow_service.delete_workflow_execution(DeleteWorkflowExecutionRequest(
            namespace=env.client.namespace, workflow_execution=WorkflowExecution(workflow_id=wid)))
    for _ in range(150):
        try:
            await env.client.get_workflow_handle(episode_id).describe()
        except RPCError as e:
            if e.status == RPCStatusCode.NOT_FOUND:
                return
            raise
        await asyncio.sleep(0.1)
    pytest.fail(f"{episode_id} was not deleted")


def audit(episode_id: str, action: str, details: str, timestamp: str) -> None:
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as c:
        c.execute("INSERT INTO audit_log (episode_id, action, details, user, timestamp) VALUES (?, ?, ?, 'admin', ?)",
                  (episode_id, action, details, timestamp))


@asynccontextmanager
async def admin_api(monkeypatch, client):
    """The API (as an admin client) reading workflows from the given Temporal client."""
    from httpx import ASGITransport, AsyncClient
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    monkeypatch.setattr(api_main, "temporal_client", client)
    async with AsyncClient(transport=ASGITransport(app=api_main.app), base_url="http://test",
                           headers={"Authorization": f"Bearer {ADMIN_SECRET}"}) as api:
        yield api


@needs_ffmpeg
@pytest.mark.timeout(600)
async def test_a_run_purged_after_its_final_cut_was_approved_and_delivered_reads_as_finished(
        dry_env, tmp_path, no_counter, monkeypatch):  # noqa: F811
    """The live case ep98cle: its final cut was approved and delivered (_subs, _nosubs, _phone), Temporal
    purged the run a day later, and the studio home card said "Not started" and its page "This video
    hasn't started yet". A real run to an approved final cut, saved through the snapshot saver's own
    code path; once the history is gone the API answers with the state the closed run reported."""
    from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
    from hfvg.spend_attribution import record_run_start
    from hfvg.studio_db import create_episode

    ep, db = "ep98cle", os.environ["DATABASE_PATH"]
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    write_audio(tmp_path, ep)
    h = Harness({})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
            _, handle, _ = await _to_final_cut(env, h, tmp_path, ep, lint_ok(PROMPT))
            run_id = (await handle.describe()).run_id
            await create_episode(db, ep)
            await record_run_start(db, ep, run_id, "admin", "admin", "api")
            delivered = await _wait(handle, lambda s: (s["delivery"]["qc"] or {}).get("verdict"), timeout=180,
                                    what="delivery QC")
            assert (delivered["delivery"]["qc"]["verdict"], delivered["delivery"]["phase"]) == ("PASS", "done")
            async with admin_api(monkeypatch, env.client) as api:
                res = await api.post(f"/api/episodes/{ep}/approve", json={"gate_id": "G_FINAL_CUT", "note": "ship it"})
                assert res.status_code == 200, res.text
                await asyncio.wait_for(handle.result(), timeout=60)
                closed = (await api.get(f"/api/episodes/{ep}")).json()
                assert (closed["run"]["status"], closed["run"]["outcome"]) == ("COMPLETED", "completed")
                assert closed["approvals"]["g_final_cut"] is True
                files = (await api.get(f"/api/episodes/{ep}/files")).json()["files"]
                res = await api.post(f"/api/episodes/{ep}/snapshot")   # what the periodic saver does every 5 min
                assert res.status_code == 200, res.text
    saved_at = read_saved(Path(os.environ["EPISODES_DIR"]) / ep)[1]
    for name in ("master_subs", "nosubs", "phone", "srt"):
        assert Path(closed["delivery_paths"][name]).is_file(), name

    async with await WorkflowEnvironment.start_local() as env:   # a fresh server: the run's history is gone
        async with admin_api(monkeypatch, env.client) as api:
            with pytest.raises(RPCError) as gone:
                await env.client.get_workflow_handle(ep).describe()
            assert gone.value.status == RPCStatusCode.NOT_FOUND
            res = await api.get(f"/api/episodes/{ep}")
            assert res.status_code == 200, res.text
            archived = res.json()
            assert archived["run"] == {**FINISHED_RUN, "run_id": run_id, "dry_run": True, "saved_at": saved_at}
            assert {k: v for k, v in archived.items() if k != "run"} == {k: v for k, v in closed.items() if k != "run"}
            assert archived["delivery"]["qc"]["verdict"] == "PASS" and archived["assembly"]["qc"]["verdict"] == "PASS"
            assert (await api.get(f"/api/episodes/{ep}/files")).json()["files"] == files
            assert [t for t in ("master_subs", "master_nosubs", "phone", "subtitles_srt") if files[t]["available"]] == [
                "master_subs", "master_nosubs", "phone", "subtitles_srt"]


async def test_a_purged_run_reads_as_expired_then_as_finished_from_its_saved_state(monkeypatch):
    """On a real Temporal dev server: the run is deleted the way the retention deletes it."""
    from hfvg.studio_db import create_episode
    from hfvg.worker import PRODUCTION_ACTIVITIES

    ep = "ep86gone"
    async with studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=False) as (env, api):
        started = await start(api, ep, live=False)
        await wait_for(api, f"/api/episodes/{ep}/story-shape", lambda b: bool(b.get("story_shape")), "shaped")
        handle = env.client.get_workflow_handle(ep)
        snapshot = await handle.query("reopen_snapshot")
        outline = snapshot["attrs"]["story_shape"]
        await purge(env, ep)
        with pytest.raises(RPCError) as gone:
            await handle.query("get_state")
        assert gone.value.status == RPCStatusCode.NOT_FOUND

        # gone before its final cut and nothing saved: the history expired, nothing is claimed
        res = await api.get(f"/api/episodes/{ep}")
        assert res.status_code == 200, res.text
        state = res.json()
        assert state["run"] == {"run_id": started["run_id"], "status": "ARCHIVED", "ended": True,
                                "outcome": "expired", "can_start_again": False, "archived": True,
                                "reason": "How this run ended is no longer known: no finished state was saved for it.",
                                "dry_run": True, "saved_at": None}
        assert (state["approvals"], state["passed_gates"], state["current_gate"]) == (
            {"story_shape": False, "g1_shot_plan": False, "g2_scene_stills": False, "g_final_cut": False}, [], None)
        assert (state["shots_count"], state["shot_outcomes"], state["delivery"], state["assembly"]) == (0, {}, None, None)
        assert (await api.get(f"/api/episodes/{ep}/story-shape")).status_code == 404

        # the saved state of its approved final cut (the periodic saver's file) and the delivered files
        saved_at = _utc()
        paths = deliver(ep)
        write_snapshot(Path(os.environ["EPISODES_DIR"]) / ep, approved_final_cut(snapshot, paths), saved_at)
        state = (await api.get(f"/api/episodes/{ep}")).json()
        assert state["run"] == {**FINISHED_RUN, "run_id": started["run_id"], "dry_run": True, "saved_at": saved_at}
        assert state["approvals"] == {"story_shape": True, "g1_shot_plan": True, "g2_scene_stills": True,
                                      "g_final_cut": True}
        assert state["passed_gates"] == ["STORY_SHAPE", "G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"]
        assert (state["current_gate"], state["stage"], state["shots_count"]) == ("G_FINAL_CUT", "mute_edit", 1)
        assert state["shot_outcomes"]["S01"]["status"] == "completed"
        assert (state["shot_decisions"], state["clip_retries"]) == ({"S01": "approved"}, {"S01": 1})
        assert (state["pending_retries"], state["pending_clip_retries"]) == ([], [])
        assert state["final_cut_rejections"] == [{"reason": "a slower ending"}]
        assert state["delivery_paths"]["phone"] == paths["phone"]
        assert state["delivery"]["qc"]["verdict"] == "DRY_RUN" and state["delivery"]["blocking"] is False
        assert state["assembly_path"] == "/x/mute_assembly.mp4"
        files = (await api.get(f"/api/episodes/{ep}/files")).json()["files"]
        assert [t for t in ("master_subs", "master_nosubs", "phone", "subtitles_srt") if files[t]["available"]] == [
            "master_subs", "master_nosubs", "phone", "subtitles_srt"]
        story = (await api.get(f"/api/episodes/{ep}/story-shape")).json()
        assert (story["story_shape"], story["approved"]) == (outline, True)

        # a change recorded after the save: the saved state no longer shows how the run ended
        audit(ep, "retry_clip", "Shot S01 clip retry 2 (clip-ep86gone-S01-v1-c2): jitter", _utc(2))
        state = (await api.get(f"/api/episodes/{ep}")).json()
        assert state["run"]["outcome"] == "expired"
        assert state["run"]["reason"].startswith(f"How this run ended is no longer known: its saved state ({saved_at} "
                                                 "UTC) does not show how it ended: the run changed after it was saved "
                                                 "(retry_clip at ")
        assert state["approvals"]["g_final_cut"] is False and state["delivery"] is None
        assert (await api.get(f"/api/episodes/{ep}/story-shape")).status_code == 404
        assert (await api.get(f"/api/episodes/{ep}/files")).json()["files"]["phone"]["available"] is True

        # an episode that never ran is still not found
        await create_episode(os.environ["DATABASE_PATH"], "ep86never")
        res = await api.get("/api/episodes/ep86never")
        assert (res.status_code, res.json()["detail"]) == (404, "No workflow found for episode ep86never")


# ---------------------------------------------------------------- the rules, without Temporal


def _db(tmp_path: Path, *, audit_rows=(), run_starts=(), jobs=(), live_mode: int = 0, known: bool = True) -> Path:
    db = tmp_path / "studio.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, episode_id TEXT, action TEXT, details TEXT, "
                  "user TEXT, timestamp TEXT)")
        c.execute("CREATE TABLE episodes (episode_id TEXT, beatmap_path TEXT, live_mode INTEGER)")
        c.execute("CREATE TABLE job_ledger (job_id TEXT, episode_id TEXT, shot_id TEXT, status TEXT)")
        c.execute("CREATE TABLE episode_runs (episode_id TEXT, run_id TEXT, started_at TEXT, started_by TEXT, "
                  "started_by_role TEXT, via TEXT)")
        if known:
            c.execute("INSERT INTO episodes VALUES ('ep87', '/nowhere/BEATMAP.md', ?)", (live_mode,))
        for action, ts in audit_rows:
            c.execute("INSERT INTO audit_log (episode_id, action, details, user, timestamp) VALUES "
                      "('ep87', ?, '', 'admin', ?)", (action, ts))
        for run_id, ts, via in run_starts:
            c.execute("INSERT INTO episode_runs VALUES ('ep87', ?, ?, NULL, NULL, ?)", (run_id, ts, via))
        for job in jobs:
            c.execute("INSERT INTO job_ledger VALUES (?, 'ep87', 'S01', 'committed')", (job,))
    return db


def _snapshot(**attrs) -> dict:
    base = {"version": 1, "episode_id": "ep87", "state": {"pending_retries": {}, "stage": "mute_edit"},
            "attrs": {"passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS", "G_FINAL_CUT"], "approved_g_final_cut": True,
                      "shot_outcomes": {"S01": {"status": "completed"}}}, "shot_workflow_ids": {}}
    base["attrs"].update(attrs)
    return base


SAVED = "2026-11-14 10:40:00"
START = [("run-1", "2026-11-14T09:00:00.000000+00:00", "start")]


@pytest.mark.parametrize("audit_rows, run_starts, finished", [
    ([("approve_g_final_cut", "2026-11-14 10:35:00")], START, True),
    ([("download_file", "2026-11-14 11:00:00")], START, True),  # watching it changes nothing
    ([("retry_clip", "2026-11-14 10:41:00")], START, False),
    ([("approve_g_final_cut", "2026-11-14 10:45:00")], START, False),
    ([("reopen_episode", "2026-11-14 11:00:00")], START, False),
    ([("start_again", "2026-11-14 11:00:00")], START, False),
    # another run started after the save (episode_runs keeps ISO times)
    ([], START + [("run-2", "2026-11-14T10:41:00.000000+00:00", "reopen")], False),
    ([], [("run-2", "2026-11-14T10:39:59.000000+00:00", "reopen")], True),
])
def test_finished_only_while_nothing_changed_since_the_save(tmp_path, audit_rows, run_starts, finished):
    db = _db(tmp_path, audit_rows=audit_rows, run_starts=run_starts)
    write_snapshot(tmp_path / "ep87", _snapshot(), SAVED)
    state = archived_state("ep87", tmp_path / "ep87", load_archive_rows(db, "ep87"), has_files=True)
    assert state["run"]["outcome"] == ("completed" if finished else "expired")
    assert state["run"]["run_id"] == run_starts[-1][0]
    assert state["approvals"]["g_final_cut"] is finished
    assert state["run"]["saved_at"] == (SAVED if finished else None)


@pytest.mark.parametrize("attrs, state, problem", [
    ({"approved_g_final_cut": False}, {}, "the saved state is not an approved final cut"),
    ({"passed_gates": ["G1_SHOT_PLAN", "G2_SCENE_STILLS"]}, {}, "the saved state is not an approved final cut"),
    ({"pending_clip_retries": ["S01"]}, {}, "the saved state has work in flight"),
    ({"pending_recompose": {"S01": {}}}, {}, "the saved state has work in flight"),
    ({}, {"pending_retries": {"S01": "wf"}}, "the saved state has work in flight"),
])
def test_only_an_approved_final_cut_with_nothing_in_flight_is_finished(tmp_path, attrs, state, problem):
    snap = _snapshot(**attrs)
    snap["state"].update(state)
    assert finished_problem(snap) == problem
    write_snapshot(tmp_path / "ep87", snap, SAVED)
    result = archived_state("ep87", tmp_path / "ep87", load_archive_rows(_db(tmp_path, run_starts=START), "ep87"),
                            has_files=False)
    assert result["run"]["outcome"] == "expired"
    assert result["run"]["reason"] == (f"How this run ended is no longer known: its saved state ({SAVED} UTC) "
                                       f"does not show how it ended: {problem}.")


def test_a_saved_state_for_another_episode_or_an_unknown_episode_is_not_finished(tmp_path):
    ep_dir = tmp_path / "ep87"
    write_snapshot(tmp_path / "ep88", {**_snapshot(), "episode_id": "ep88"}, SAVED)
    ep_dir.mkdir()
    (ep_dir / "state").mkdir()
    (ep_dir / SNAPSHOT_REL).write_bytes((tmp_path / "ep88" / SNAPSHOT_REL).read_bytes())
    state = archived_state("ep87", ep_dir, load_archive_rows(_db(tmp_path), "ep87"), has_files=False)
    assert state["run"]["outcome"] == "expired" and "the saved state is for 'ep88'" in state["run"]["reason"]

    write_snapshot(ep_dir, _snapshot(), SAVED)
    other = tmp_path / "other"
    other.mkdir()
    state = archived_state("ep87", ep_dir, load_archive_rows(_db(other, known=False), "ep87"), has_files=False)
    assert state["run"]["outcome"] == "expired" and state["run"]["dry_run"] is None
    assert "the episode is not in the studio DB" in state["run"]["reason"]


def test_an_unreadable_saved_state_reads_as_expired(tmp_path):
    ep_dir = tmp_path / "ep87"
    (ep_dir / "state").mkdir(parents=True)
    (ep_dir / SNAPSHOT_REL).write_text("{not json", encoding="utf-8")
    state = archived_state("ep87", ep_dir, load_archive_rows(_db(tmp_path, live_mode=1), "ep87"), has_files=False)
    assert state["run"]["outcome"] == "expired" and state["run"]["dry_run"] is False
    assert "cannot be read" in state["run"]["reason"]


@pytest.mark.parametrize("evidence", ["nothing", "files", "start", "spend", "run action", "other action"])
def test_an_episode_that_never_ran_has_no_archived_state(tmp_path, evidence):
    db = _db(tmp_path,
             run_starts=START if evidence == "start" else (),
             jobs=["still-ep87-S01-v1"] if evidence == "spend" else (),
             audit_rows=[("approve_shot", SAVED)] if evidence == "run action"
             else [("upload_beatmap", SAVED)] if evidence == "other action" else ())
    state = archived_state("ep87", tmp_path / "ep87", load_archive_rows(db, "ep87"), has_files=evidence == "files")
    if evidence in ("nothing", "other action"):
        assert state is None
    else:
        assert state["run"]["outcome"] == "expired"
        assert state["run"]["reason"] == "How this run ended is no longer known: no finished state was saved for it."


def test_rows_of_an_older_db_without_the_run_tables(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE episodes (episode_id TEXT, beatmap_path TEXT, live_mode INTEGER)")
        c.execute("INSERT INTO episodes VALUES ('ep87', NULL, 0)")
    assert load_archive_rows(db, "ep87") == {"known": True, "audit": [], "jobs": [], "run_starts": [],
                                             "beatmap_path": None, "live_mode": False}
    assert load_archive_rows(tmp_path / "missing.db", "ep87")["known"] is False


def test_rebuilt_from_the_saved_artifacts_when_no_state_was_saved(tmp_path):
    """No snapshot file (the saver never ran): the GAP-79 rebuild shows the approved final cut, unless the
    approval is not the episode's last run change."""
    run, ed, db = _fixture(tmp_path, AUDIT)
    rows = load_archive_rows(db, EP)
    state = archived_state(EP, ed, rows, has_files=True, parsed_beatmap_fn=lambda _p: PARSED)
    assert state["run"]["outcome"] == "completed" and state["run"]["saved_at"] is None
    assert state["run"]["dry_run"] is False  # the rebuild takes the episode's live mode
    assert state["approvals"]["g_final_cut"] is True
    assert sorted(state["shot_outcomes"]) == ["A01", "A02", "A03"]
    assert state["delivery"]["qc"]["verdict"] == "RESTORED"
    # without the parse the rebuild is not tried
    assert archived_state(EP, ed, rows, has_files=True)["run"]["outcome"] == "expired"

    for later in (("reopen_episode", "reopened"), ("reject_g_final_cut", "slower"), ("retry_clip", "jitter")):
        run2, ed2, db2 = _fixture(tmp_path / later[0], AUDIT + [later])
        state = archived_state(EP, ed2, load_archive_rows(db2, EP), has_files=True, parsed_beatmap_fn=lambda _p: PARSED)
        assert state["run"]["outcome"] == "expired", later

    run3, ed3, db3 = _fixture(tmp_path / "again", AUDIT)
    with sqlite3.connect(db3) as c:
        c.execute("CREATE TABLE episode_runs (episode_id TEXT, run_id TEXT, started_at TEXT, started_by TEXT, "
                  "started_by_role TEXT, via TEXT)")
        c.execute("INSERT INTO episode_runs VALUES (?, 'run-9', '2026-10-29T11:00:00+00:00', NULL, NULL, 'reopen')",
                  (EP,))
    state = archived_state(EP, ed3, load_archive_rows(db3, EP), has_files=True, parsed_beatmap_fn=lambda _p: PARSED)
    assert state["run"]["outcome"] == "expired" and state["run"]["run_id"] == "run-9"

    def broken(_path):
        raise ValueError("BEATMAP.md cannot be parsed")
    assert archived_state(EP, ed, rows, has_files=True, parsed_beatmap_fn=broken)["run"]["outcome"] == "expired"


def test_changed_since_reads_both_time_formats():
    rows = {"audit": [{"action": "retry_shot", "timestamp": "2026-11-14 10:40:01"}],
            "run_starts": [{"run_id": "r", "started_at": "2026-11-14T10:00:00+00:00", "via": "start"}]}
    assert changed_since("2026-11-14 10:40:00", rows) == (
        "the run changed after it was saved (retry_shot at 2026-11-14 10:40:01 UTC)")
    assert changed_since("2026-11-14 10:40:01", rows) is None
    assert changed_since("", rows) == "the saved state has no time"
