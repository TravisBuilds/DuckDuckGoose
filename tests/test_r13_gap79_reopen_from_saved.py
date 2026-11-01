"""GAP-79 (ep05v2 r13): the dev server purged the COMPLETED v10 run after its 24 h retention, so the
GAP-41 /reopen had no closed run to query. The API now persists every episode's reopen snapshot
(<episode>/state/reopen_snapshot.json) and /reopen-from-saved reopens an episode whose workflow is gone
from that file, or from a reconstruction of the saved artifacts + DB (hfvg.saved_state). Counters
continue (no repeated job id), nothing is regenerated or re-assembled, and it refuses while any
workflow with the id exists."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from unittest.mock import AsyncMock
from temporalio.common import WorkflowIDReusePolicy
from temporalio.testing import WorkflowEnvironment

from hfvg.saved_state import (SavedStateError, read_persisted, reconstruct_snapshot, saved_snapshot,
                              snapshot_path, write_snapshot)
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, needs_ffmpeg, no_counter  # noqa: F401
from tests.test_recompose_and_edit import _to_final_cut, _worker

EP = "ep79"


def _plan(sid):
    return {"shot_id": sid, "prompt": f"{sid} prompt", "duration": 4, "refs": []}


def _fixture(tmp_path: Path, audit: list[tuple[str, str]], jobs=(), clips=None, stills=None):
    """A run dir (run/data/episodes/ep79) with clip + still files, the accepted edit and a studio DB."""
    run = tmp_path / "run"
    ed = run / "data" / "episodes" / EP
    clips = clips or {"A01": ["clip_v2.mp4", "clip_v1002.mp4", "clip_v2002.mp4", "clip_v1.mp4", "clip_v3001.mp4"],
                      "A02": ["clip_v1.mp4"], "A03": ["clip_v1004.mp4", "clip_v4.mp4"]}
    stills = stills or {"A01": ["still_v1.jpg", "still_v2.jpg"], "A02": ["still_v1.jpg"],
                        "A03": ["still_v4.png", "still_v3.png"]}
    for sid, fs in clips.items():
        d = ed / "shots" / sid
        d.mkdir(parents=True, exist_ok=True)
        for f in fs + stills.get(sid, []):
            (d / f).write_bytes(b"x")
    used = {"A01": "clip_v2002.mp4", "A02": "clip_v1.mp4", "A03": "clip_v1004.mp4"}
    (ed / "edit").mkdir(parents=True)
    (ed / "edit" / "assembly_manifest.json").write_text(json.dumps({"episode_id": EP, "clips": [
        {"id": sid, "source": f"data/episodes/{EP}/shots/{sid}/{f}", "shot_meta": {"m": sid}}
        for sid, f in used.items()]}))
    (ed / "delivery").mkdir()
    (ed / "delivery" / "delivery_manifest.json").write_text(json.dumps({"version": 3, "paths": {"a": "b"}}))
    db = run / "studio.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, episode_id TEXT, action TEXT, details TEXT, "
              "user TEXT, timestamp TEXT DEFAULT '2026-10-29 10:00:00')")
    c.execute("CREATE TABLE job_ledger (job_id TEXT, episode_id TEXT, shot_id TEXT, status TEXT)")
    c.execute("CREATE TABLE episodes (episode_id TEXT, beatmap_path TEXT, live_mode INTEGER)")
    c.execute("INSERT INTO episodes VALUES (?, ?, 1)", (EP, str(ed / "BEATMAP.md")))
    for i, (a, d) in enumerate(audit):
        c.execute("INSERT INTO audit_log (episode_id, action, details, user, timestamp) VALUES (?,?,?,?,?)",
                  (EP, a, d, "admin", f"2026-10-29 10:{i:02d}:00"))
    for j in jobs:
        c.execute("INSERT INTO job_ledger VALUES (?, ?, 'x', 'completed')", (j, EP))
    c.commit(); c.close()
    return run, ed, db


PARSED = {"shots": [_plan("A01"), _plan("A02")]}
AUDIT = [
    ("retry_clip", "Shot A01 clip retry 1 (clip-ep79-A01-v2-c1): jitter"),
    ("recompose_shot", "Shot A01 recompose 1 v2 refs=['r']: wrong count"),
    ("recompose_decision", "Shot A01 recompose approved: ok"),
    ("retry_clip", "Shot A01 clip retry 2 (clip-ep79-A01-v2-c2): reversal"),
    ("adopt_reuse_slot", "Shot A03 adopted (template A02): re-shoot the reuse slot"),
    ("grant_recompose", "Shot A03 recompose grant: Studio"),
    ("accept_clip", "Shot A03 needs_review clip kept (take 1): fine"),
    ("recompose_shot", 'Shot A02 recompose 3 v1 refs=[\'a\'] shot_meta={"count": 0, "x": "a: b"} '
                       'clip_route={"model": "kling"} plate=1e5a6fb92ee5 (/p/plate.json): Studio: plate'),
    ("approve_g_final_cut", "approved"),
]


def _rows(db):
    from hfvg.saved_state import load_db_rows
    return load_db_rows(db, EP)


def test_reconstruct_counters_takes_and_adopted_shot(tmp_path):
    run, ed, db = _fixture(tmp_path, AUDIT, jobs=["clip-ep79-A01-v2-c3", "still-ep79-A01-rc4", "clip-ep79-A02-v1"])
    snap = reconstruct_snapshot(EP, ed, _rows(db), PARSED)
    a = snap["attrs"]
    assert snap["recompute_signatures"] and snap["reconstructed"]
    assert EpisodeWorkflowV2.reopen_problem(EP, {"snapshot": snap, "reason": "r"}) is None
    # ledger beats the audit (a retry job without its row still counts: never a repeated job id)
    assert a["clip_retries"] == {"A01": 3}
    assert a["recompose_counts"] == {"A01": 4, "A02": 3}   # incl. a plate + shot_meta + route row
    assert a["recompose_log"][-1]["reason"] == "Studio: plate"
    assert a["recompose_versions"]["A01"] == 2 and a["recompose_versions"]["A03"] == 4
    # takes: same still version only, keyed by take; the edit's take is the outcome
    assert sorted(a["clip_takes"]["A01"]) == ["0", "1", "2"]
    assert a["clip_takes"]["A01"]["2"]["clip_path"].endswith("clip_v2002.mp4")
    assert a["shot_outcomes"]["A01"]["still_url"].endswith("A01/still_v2.jpg")
    assert Path(a["shot_outcomes"]["A01"]["still_url"]).is_absolute()
    assert a["launched_shot_plans"]["A01"]["version"] == 2
    assert a["launched_shot_plans"]["A01"]["shot_meta"] == {"m": "A01"}
    # adopted reuse slot rebuilt from its template, with its grant and acceptance
    assert a["launched_shot_plans"]["A03"]["adopted_reuse"]["template"] == "A02"
    assert a["adopted_reuse_log"][0]["shot_id"] == "A03"
    assert a["recompose_grants"] == {"A03": 1}
    assert a["clip_review_acceptances"]["A03"]["take"] == 1
    assert [s["shot_id"] for s in a["assembly_shots"]] == ["A01", "A02", "A03"]
    assert a["delivery_version"] == 3 and a["_beatmap_path"].endswith("BEATMAP.md") and a["_dry_run"] is False
    assert snap["state"]["current_gate"] == "G_FINAL_CUT"
    assert snap["state"]["assembly_qc"]["blocking"] is False


@pytest.mark.parametrize("audit,why", [
    (AUDIT[:-1], "never approved"),
    (AUDIT + [("retry_clip", "Shot A01 clip retry 3 (clip-ep79-A01-v2-c3): x")], "after the last"),
    (AUDIT + [("reopen_from_saved", "x"), ("recompose_shot", "Shot A01 recompose 5 v3 refs=[]: y")], "after the last"),
    (AUDIT + [("retry_shot", "Shot A01 retry")], "after the last"),
])
def test_reconstruct_fails_closed_on_unknown_state(tmp_path, audit, why):
    run, ed, db = _fixture(tmp_path, audit)
    with pytest.raises(SavedStateError, match=why):
        reconstruct_snapshot(EP, ed, _rows(db), PARSED)


def test_reconstruct_fails_closed_on_missing_artifacts(tmp_path):
    run, ed, db = _fixture(tmp_path, AUDIT)
    (ed / "shots" / "A01" / "still_v2.jpg").unlink()
    with pytest.raises(SavedStateError, match="no still_v2"):
        reconstruct_snapshot(EP, ed, _rows(db), PARSED)
    (ed / "shots" / "A01" / "still_v2.jpg").write_bytes(b"x")
    (ed / "shots" / "A02" / "clip_v1.mp4").unlink()
    with pytest.raises(SavedStateError, match="missing on disk"):
        reconstruct_snapshot(EP, ed, _rows(db), PARSED)
    (ed / "edit" / "assembly_manifest.json").unlink()
    with pytest.raises(SavedStateError, match="no accepted edit"):
        reconstruct_snapshot(EP, ed, _rows(db), PARSED)


def test_persisted_snapshot_round_trip_and_staleness(tmp_path):
    run, ed, db = _fixture(tmp_path, AUDIT)
    snap = {"version": 1, "episode_id": EP, "state": {}, "attrs": {"x": 1}}
    with pytest.raises(SavedStateError):
        write_snapshot(ed, {**snap, "episode_id": "other"}, "2026-10-29 11:00:00")
    with pytest.raises(SavedStateError):
        write_snapshot(ed, {**snap, "version": 2}, "2026-10-29 11:00:00")
    assert read_persisted(ed) is None
    write_snapshot(ed, snap, "2026-10-29 11:00:00")
    assert snapshot_path(ed).exists() and read_persisted(ed) == snap
    assert saved_snapshot(EP, ed, db, lambda _p: PARSED) == (snap, "persisted")
    # a change recorded after the save makes the file stale: refused, never silently used
    c = sqlite3.connect(db)
    c.execute("INSERT INTO audit_log (episode_id, action, details, user, timestamp) VALUES "
              "(?, 'retry_clip', 'Shot A01 clip retry 3 (j): x', 'admin', '2026-10-29 12:00:00')", (EP,))
    c.commit(); c.close()
    with pytest.raises(SavedStateError, match="older than"):
        saved_snapshot(EP, ed, db, lambda _p: PARSED)
    snapshot_path(ed).unlink()
    with pytest.raises(SavedStateError, match="after the last"):
        saved_snapshot(EP, ed, db, lambda _p: PARSED)


def test_unchanged_reopen_is_transparent(tmp_path):
    run, ed, db = _fixture(tmp_path, AUDIT + [("reopen_from_saved", "x"), ("reopen_episode", "y")])
    assert reconstruct_snapshot(EP, ed, _rows(db), PARSED)["attrs"]["clip_retries"] == {"A01": 2}


def test_saved_snapshot_reconstructs_without_a_file(tmp_path):
    run, ed, db = _fixture(tmp_path, AUDIT)
    snap, src = saved_snapshot(EP, ed, db, lambda _p: PARSED)
    assert src == "reconstructed" and snap["attrs"]["clip_retries"] == {"A01": 2}


# ------------------------------------------------------------------ API: idempotent, sanctioned path
def _api(monkeypatch, tmp_path, describe_exc=None, state=None, run_id="r1", status="COMPLETED"):
    import sys
    from unittest.mock import AsyncMock, MagicMock
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    sys.modules.pop("api.main", None)
    import api.main as m
    client = MagicMock()
    handle = MagicMock()
    if describe_exc is None:
        st = MagicMock(); st.name = status
        handle.describe = AsyncMock(return_value=MagicMock(run_id=run_id, status=st))
    else:
        handle.describe = AsyncMock(side_effect=describe_exc)
    handle.query = AsyncMock(return_value=state or {})
    handle.terminate = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.handle = handle
    client.start_workflow = AsyncMock(return_value=MagicMock(result_run_id="new"))
    m.temporal_client = client
    return m, client


def test_reopen_from_saved_refused_while_a_workflow_exists(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    m, client = _api(monkeypatch, tmp_path)
    r = TestClient(m.app).post("/api/episodes/ep79/reopen-from-saved", json={"reason": "Studio"},
                               headers={"Authorization": "Bearer " + "a" * 32})
    assert r.status_code == 409 and "exists" in r.json()["detail"]
    client.start_workflow.assert_not_called()


def test_reopen_from_saved_starts_one_run_from_the_saved_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from temporalio.service import RPCError, RPCStatusCode
    run, ed, db = _fixture(tmp_path, AUDIT)
    monkeypatch.setenv("EPISODES_DIR", str(ed.parent))
    m, client = _api(monkeypatch, tmp_path, RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
    m.DATABASE_PATH = str(db)

    async def fake_parse(path, ep, live):
        return PARSED
    monkeypatch.setattr("hfvg.activities.workflow_support.parse_beatmap_activity", fake_parse)
    hdr = {"Authorization": "Bearer " + "a" * 32}
    tc = TestClient(m.app)
    assert tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": " "}, headers=hdr).status_code == 422
    r = tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": "Studio v11"}, headers=hdr)
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "reconstructed"
    kw = client.start_workflow.call_args.kwargs
    args = kw["args"]
    assert kw["id"] == "ep79" and args[0] == "ep79" and args[2] is False
    assert args[6]["source"] == "saved_state:reconstructed" and args[6]["from_run_id"] is None
    assert args[6]["snapshot"]["attrs"]["clip_retries"] == {"A01": 2}
    row = sqlite3.connect(db).execute("SELECT details FROM audit_log WHERE action='reopen_from_saved'").fetchone()
    assert row and "Studio v11" in row[0]
    # a not-approved state is refused before anything starts
    client.start_workflow.reset_mock()
    c = sqlite3.connect(db); c.execute("DELETE FROM audit_log WHERE action='approve_g_final_cut'"); c.commit(); c.close()
    r = tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": "again"}, headers=hdr)
    assert r.status_code == 409
    client.start_workflow.assert_not_called()


def _supersede_setup(monkeypatch, tmp_path, extra_audit=(), state=None, status="RUNNING"):
    run, ed, db = _fixture(tmp_path, AUDIT + [("reopen_from_saved", "reopened from saved state (reconstructed) as "
                                                                     "r1: first")] + list(extra_audit))
    monkeypatch.setenv("EPISODES_DIR", str(ed.parent))
    state = state if state is not None else {
        "reopen_log": [{"source": "saved_state:reconstructed", "run_id": "r1"}], "recompose": {"pending": {}}}
    m, client = _api(monkeypatch, tmp_path, state=state, status=status)
    m.DATABASE_PATH = str(db)

    async def fake_parse(path, ep, live):
        return PARSED
    monkeypatch.setattr("hfvg.activities.workflow_support.parse_beatmap_activity", fake_parse)
    # a persisted file from the superseded run must NOT be used
    write_snapshot(ed, {"version": 1, "episode_id": EP, "state": {}, "attrs": {"stale": 1}}, "2026-10-29 23:59:00")
    from fastapi.testclient import TestClient
    return m, client, TestClient(m.app), db


HDR = {"Authorization": "Bearer " + "a" * 32}


def test_supersede_own_unchanged_saved_state_run(monkeypatch, tmp_path):
    m, client, tc, db = _supersede_setup(monkeypatch, tmp_path)
    r = tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": "fix", "supersede_run_id": "r1"}, headers=HDR)
    assert r.status_code == 200, r.text
    client.handle.terminate.assert_awaited_once()
    snap = client.start_workflow.call_args.kwargs["args"][6]["snapshot"]
    assert snap.get("reconstructed") and "stale" not in snap["attrs"]
    row = sqlite3.connect(db).execute(
        "SELECT details FROM audit_log WHERE action='reopen_from_saved' ORDER BY id DESC").fetchone()
    assert "superseding r1" in row[0]


def test_supersede_a_closed_failed_run_without_query(monkeypatch, tmp_path):
    m, client, tc, db = _supersede_setup(monkeypatch, tmp_path, status="COMPLETED")
    client.handle.query = AsyncMock(side_effect=RuntimeError("no state"))
    r = tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": "fix", "supersede_run_id": "r1"}, headers=HDR)
    assert r.status_code == 200, r.text
    client.handle.terminate.assert_not_called()


def test_snapshot_route_saves_only_a_reopenable_state(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    run, ed, db = _fixture(tmp_path, AUDIT)
    monkeypatch.setenv("EPISODES_DIR", str(ed.parent))
    m, client = _api(monkeypatch, tmp_path)
    client.handle.query = AsyncMock(return_value={"version": 1, "episode_id": EP, "state": {"pending_retries": {}},
                                                  "attrs": {"passed_gates": [], "approved_g_final_cut": False}})
    r = TestClient(m.app).post("/api/episodes/ep79/snapshot", headers=HDR)
    assert r.status_code == 409 and not snapshot_path(ed).exists()
    ok = {"version": 1, "episode_id": EP, "state": {"pending_retries": {}},
          "attrs": {"passed_gates": ["G_FINAL_CUT"], "approved_g_final_cut": True, "pending_clip_retries": {},
                    "pending_recompose": {}}}
    client.handle.query = AsyncMock(return_value=ok)
    assert TestClient(m.app).post("/api/episodes/ep79/snapshot", headers=HDR).status_code == 200
    assert read_persisted(ed) == ok


@pytest.mark.parametrize("body,extra,state,why", [
    ({"supersede_run_id": "other"}, (), None, "exists"),
    ({}, (), None, "exists"),
    ({"supersede_run_id": "r1"}, (("retry_clip", "Shot A01 clip retry 3 (j): x"),), None, "recorded changes"),
    ({"supersede_run_id": "r1"}, (("reopen_from_saved", "reopened from saved state (x) as r0: y"),), None,
     "not started by"),
    ({"supersede_run_id": "r1"}, (), {"reopen_log": [{"source": "saved_state:x", "run_id": "r1"}],
                                      "recompose": {"pending": {"A01": {}}}}, "in flight"),
])
def test_supersede_refusals(monkeypatch, tmp_path, body, extra, state, why):
    m, client, tc, db = _supersede_setup(monkeypatch, tmp_path, extra, state)
    r = tc.post("/api/episodes/ep79/reopen-from-saved", json={"reason": "fix", **body}, headers=HDR)
    assert r.status_code == 409 and why in r.json()["detail"], r.text
    client.handle.terminate.assert_not_called()
    client.start_workflow.assert_not_called()


# ------------------------------------------------------------- workflow: history gone, state continues
@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(600)
async def test_reopen_without_history_parks_and_continues(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "rowan.png"
    ref.write_bytes(b"png")
    new_prompt = lint_ok("Tighter wide shot. EXACTLY THREE ducks on the boardwalk")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep94")
            await handle.execute_update("recompose_shot", args=["A01", "first", new_prompt, [str(ref)], ["rowan"]])
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="parked")
            await handle.execute_update("recompose_decision", args=["A01", False, "no"])
            before = await _wait(handle, lambda s: not s["recompose"]["pending"], what="closed")
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=90)
            snap = await handle.query("reopen_snapshot")
    write_snapshot(tmp_path / "ep94", snap, "2026-10-31 00:00:00")
    persisted = read_persisted(tmp_path / "ep94")
    # what the saved-state reconstruction yields: no signatures, recomputed by the workflow
    for k in ("assembly_sig", "delivery_sig"):
        persisted["attrs"].pop(k, None)
    persisted["recompute_signatures"] = True
    stills, clips_n = len(h.still_calls), len(h.clip_calls)
    async with await WorkflowEnvironment.start_local() as env2:   # a fresh server: the history is gone
        async with _worker(env2, h, clips):
            reopen_from = {"snapshot": persisted, "reason": "Studio v11", "from_run_id": None,
                           "source": "saved_state:persisted"}
            assert EpisodeWorkflowV2.reopen_problem("ep94", reopen_from) is None
            attrs = persisted["attrs"]
            await env2.client.start_workflow(
                "EpisodeWorkflowV2",
                args=["ep94", attrs["_beatmap_path"], attrs["_dry_run"], None, None, None, reopen_from],
                id="ep94", task_queue=TASK_QUEUE, id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
            latest = env2.client.get_workflow_handle("ep94")
            state = await _wait(latest, lambda s: s["current_gate"] == "G_FINAL_CUT" and s["reopen_log"],
                                what="reopened")
            assert state["reopen_log"][0]["source"] == "saved_state:persisted"
            assert not state["approvals"]["g_final_cut"]
            assert state["shot_outcomes"] == before["shot_outcomes"]
            assert state["assembly"]["stale"] is False and state["delivery"] == before["delivery"]
            await asyncio.sleep(1.0)
            assert (len(h.still_calls), len(h.clip_calls)) == (stills, clips_n)
            assert state["assembly"]["edit_version"] == before["assembly"]["edit_version"]
            out = await latest.execute_update(
                "recompose_shot", args=["A01", "v11", new_prompt, [str(ref)], ["rowan"]])
            assert out["workflow_id"] == "ep94-shot-A01-recompose2", out
            await _wait(latest, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="parked after reopen")
            await latest.execute_update("recompose_decision", args=["A01", True, "ok"])
            landed = await _child_result(env2, "ep94-shot-A01-recompose2", timeout=90)
            await _wait(latest, lambda s: s["shot_outcomes"]["A01"]["clip_url"] == landed["clip_url"],
                        what="replaced")
            await latest.execute_update("accept_clip_review", args=["A01", "ok", None])
            # recomputed signatures: the changed clip DOES rebuild the edit (None would never go stale)
            await _wait(latest, lambda s: s["assembly"]["edit_version"] > before["assembly"]["edit_version"],
                        timeout=120, what="re-assembled")
            await latest.terminate("test done")
    # the reconstruction only emits attrs the workflow snapshot knows
