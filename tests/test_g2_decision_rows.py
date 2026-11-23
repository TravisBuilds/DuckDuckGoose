"""Bug 29: a person's G2 decision and the shot's row in the studio DB agree.

An approve (POST /shots/{id}/approve) after a reject resets the row as the workflow's decision is
reset: the rejected status and the rejection reason are cleared; selecting a still version does the
same. At G2 approval, approve_g2_shots approves the shots a person kept (the kept ids the workflow
passes) whatever status an earlier decision left on their rows, but never a failed or blocked shot,
and never a still the QC judge failed that no person kept."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from tests.test_gate_correctness_round1 import _api_client

EP = "ep99"


async def _seed(db_path, rows: list[tuple[str, str, str | None, str | None]]) -> None:
    """rows: (shot_id, status, still_url, rejection_reason)"""
    from hfvg.studio_db import create_episode, init_studio_db

    await init_studio_db(db_path)
    await create_episode(db_path, EP)
    async with aiosqlite.connect(db_path) as db:
        for shot_id, status, url, reason in rows:
            await db.execute("INSERT INTO shots (id, episode_id, shot_id, prompt, status, still_url, rejection_reason) "
                             "VALUES (?, ?, ?, 'p', ?, ?, ?)", (f"{EP}-{shot_id}", EP, shot_id, status, url, reason))
        await db.commit()


async def _rows(db_path) -> dict[str, tuple]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT shot_id, status, rejection_reason FROM shots WHERE episode_id = ? "
                              "ORDER BY shot_id", (EP,)) as cur:
            return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


async def _audits(db_path, action) -> int:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM audit_log WHERE episode_id = ? AND action = ?", (EP, action)) as cur:
            return (await cur.fetchone())[0]


def _episode_shot(api_main):
    """No canary for the shot: the approve route takes the per-shot G2 decision (approve_shot update)."""
    canary = MagicMock()
    canary.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    api_main.temporal_client.get_workflow_handle_for = MagicMock(return_value=canary)


@pytest.mark.asyncio
async def test_approve_after_reject_clears_the_rejected_row(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rows.db")
    await _seed(db_path, [("A01", "still_complete", "https://example.com/a01.jpg", None),
                          ("A02", "failed", "https://example.com/a02.jpg", None)])
    client, api_main, wf_handle = await _api_client(db_path, monkeypatch)
    _episode_shot(api_main)

    wf_handle.execute_update = AsyncMock(return_value={"success": True, "action": "rejected"})
    assert client.post(f"/api/episodes/{EP}/shots/A01/reject", data={"reason": "goose off-model"}).status_code == 200
    assert (await _rows(db_path))["A01"] == ("rejected", "goose off-model")

    wf_handle.execute_update = AsyncMock(return_value={"success": True, "message": "Shot A01 approved"})
    res = client.post(f"/api/episodes/{EP}/shots/A01/approve")
    assert res.status_code == 200, res.text
    call = wf_handle.execute_update.await_args
    assert call.args == ("approve_shot",) and call.kwargs == {"args": ["A01"]}, call
    assert (await _rows(db_path))["A01"] == ("approved", None)
    assert await _audits(db_path, "approve_shot") == 1

    # a failed shot keeps its status: an approve never hides a failure
    assert client.post(f"/api/episodes/{EP}/shots/A02/approve").status_code == 200
    assert (await _rows(db_path))["A02"] == ("failed", None)


@pytest.mark.asyncio
async def test_a_refused_approve_leaves_the_row_alone(tmp_path, monkeypatch):
    db_path = str(tmp_path / "rows.db")
    await _seed(db_path, [("A01", "rejected", "https://example.com/a01.jpg", "goose off-model")])
    client, api_main, wf_handle = await _api_client(db_path, monkeypatch)
    _episode_shot(api_main)
    wf_handle.execute_update = AsyncMock(side_effect=ApplicationError("Cannot approve A01: not at G2 gate"))
    res = client.post(f"/api/episodes/{EP}/shots/A01/approve")
    assert res.status_code == 409 and "not at G2" in res.json()["detail"], res.text
    assert (await _rows(db_path))["A01"] == ("rejected", "goose off-model")
    assert await _audits(db_path, "approve_shot") == 0


@pytest.mark.asyncio
async def test_selecting_a_still_version_after_a_reject_clears_the_rejected_row(tmp_path, monkeypatch):
    """A selection keeps a still (G2 still to come): a rejected or judge-failed row is back to
    still_complete without its rejection reason; an approved row stays approved."""
    db_path = str(tmp_path / "rows.db")
    await _seed(db_path, [("A01", "rejected", "https://example.com/a01.jpg", "two ducks"),
                          ("A02", "qc_failed", "https://example.com/a02.jpg", None),
                          ("A03", "approved", "https://example.com/a03.jpg", None)])
    client, api_main, wf_handle = await _api_client(db_path, monkeypatch)
    monkeypatch.setattr(api_main, "_select_still_checked",
                        AsyncMock(return_value=({"workflow_id": f"{EP}-shot-A01"}, [])))
    wf_handle.execute_update = AsyncMock(return_value={"previous_version": 2})
    for shot_id in ("A01", "A02", "A03"):
        res = client.post(f"/api/episodes/{EP}/shots/{shot_id}/select-still",
                          json={"version": 1, "reason": "the first still is right"})
        assert res.status_code == 200, res.text
    assert await _rows(db_path) == {"A01": ("still_complete", None), "A02": ("still_complete", None),
                                    "A03": ("approved", None)}


@pytest.mark.asyncio
async def test_approve_g2_shots_approves_the_rows_people_kept(tmp_path, monkeypatch):
    """Kept by a person: A01 (rejected, then approved: the row still says rejected), A02 (a judge-failed
    still kept), A03 (failed: stays failed), A04 (blocked: stays blocked). Not kept: A05 (judge-failed,
    no decision: never approved), A06 (pending review: approved as before), A07 (rejected now)."""
    from hfvg.studio_db import approve_g2_shots

    db_path = str(tmp_path / "rows.db")
    await _seed(db_path, [
        ("A01", "rejected", "https://example.com/a01.jpg", "goose off-model"),
        ("A02", "qc_failed", "https://example.com/a02.jpg", None),
        ("A03", "failed", "https://example.com/a03.jpg", None),
        ("A04", "blocked", "https://example.com/a04.jpg", None),
        ("A05", "qc_failed", "https://example.com/a05.jpg", None),
        ("A06", "still_complete", "https://example.com/a06.jpg", None),
        ("A07", "still_complete", "https://example.com/a07.jpg", None),
    ])
    monkeypatch.setitem(os.environ, "DATABASE_PATH", db_path)
    count = await approve_g2_shots(EP, ["A07"], ["A01", "A02", "A03", "A04"])
    assert await _rows(db_path) == {
        "A01": ("approved", None), "A02": ("approved", None), "A03": ("failed", None),
        "A04": ("blocked", None), "A05": ("qc_failed", None), "A06": ("approved", None),
        "A07": ("rejected", None)}
    assert count == 3

    # without kept ids (a G2 approval recorded before the workflow passed them) nothing changes
    await approve_g2_shots(EP, ["A07"])
    assert (await _rows(db_path))["A05"] == ("qc_failed", None)
