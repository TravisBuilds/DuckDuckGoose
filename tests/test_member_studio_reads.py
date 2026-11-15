"""Member reads for the creator-first episode page and the studio shell.

The episode page shows a member the scene pictures they approve, the run's credits, stall alerts and
model limits, so those GET routes allow members (read only). GET /api/me tells the shell who is signed
in, and GET /api/episodes names each episode's owner. The admin-only writes (balance, cap, G1.08,
canary) stay admin-only, and the shot states leave the free text that can quote a prompt (and with it
the cast) out for a member who may not see the episode's cast (docs/AUTHENTICATION.md).
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

ADMIN = "member-reads-admin-secret-0123456789abcdef"
OWNER = ("owner@reads.test", "owner-pass-123")
OTHER = ("other@reads.test", "other-pass-123")
STAFF = ("staff@reads.test", "staff-pass-123")
EP = "ep91own"
LEGACY = "ep92old"
NO_OWNER = "ep93nul"

STILL_ERROR = ("Content blocked (still); escalated to human review. Provider: flagged "
               "'a tall woman in a mustard raincoat'")
ESCALATION = {"kind": "still", "provider_message": "flagged 'a tall woman in a mustard raincoat'",
              "reason": "ladder exhausted for the mustard raincoat prompt", "steps": ["reword"],
              "blocked_job_id": "job-s01"}
LINT = {"decision": "advisory", "advisory_violations": ["Shot S01: the mustard raincoat is wet"]}
CLIP_ERROR = "Clip failed: provider refused 'the mustard raincoat flaps in the wind'"
CHILDREN = {
    "S01": {
        "still_status": "failed",
        "clip_status": None,
        "still_error": STILL_ERROR,
        "clip_error": None,
        "moderation": {"still": {"steps": ["reword"], "retry": {"n": 1, "step": "reword"}, "log": ["x"]}},
        "moderation_escalation": ESCALATION,
        "pre_approved": False,
        "moderation_lint": LINT,
    },
    "S02": {"still_status": "approved", "clip_status": "failed", "still_error": None, "clip_error": CLIP_ERROR},
}


def _temporal():
    """Every episode workflow has two shots whose child states carry cast-quoting free text."""
    def handle_for(workflow_id, *_a, **_k):
        handle = MagicMock()
        if "-shot-" in workflow_id:
            state = CHILDREN[workflow_id.rsplit("-", 1)[1]]
        else:
            state = {"shot_workflow_ids": {sid: f"{workflow_id}-shot-{sid}" for sid in CHILDREN}}
        handle.query = AsyncMock(return_value=state)
        return handle

    client = MagicMock()
    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    return client


@pytest.fixture
async def studio(monkeypatch):
    """Two members, an admin user and three episodes: EP owned by OWNER, LEGACY and NO_OWNER without one."""
    import bcrypt

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    owner = await users.create_user(name="Owner", email=OWNER[0], role="member", password=OWNER[1])
    await users.create_user(name="Other", email=OTHER[0], role="member", password=OTHER[1])
    staff = await users.create_user(name="Staff", email=STAFF[0], role="admin", password=STAFF[1])
    await create_episode(db, EP, user_id=owner["user_id"])
    await create_episode(db, LEGACY)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO episodes (episode_id, user_id) VALUES (?, NULL)", (NO_OWNER,))
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
    api_main.owner_id = owner["user_id"]
    api_main.staff_id = staff["user_id"]
    return api_main


def _client(api_main, who: str):
    from fastapi.testclient import TestClient
    client = TestClient(api_main.app, raise_server_exceptions=False)
    if who == "none":
        return client
    if who == "bearer":
        client.headers["Authorization"] = f"Bearer {ADMIN}"
        return client
    body = {"admin_secret": ADMIN} if who == "admin" else {
        "owner": {"email": OWNER[0], "password": OWNER[1]},
        "other": {"email": OTHER[0], "password": OTHER[1]},
        "staff": {"email": STAFF[0], "password": STAFF[1]}}[who]
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


REVIEW_READS = [f"/api/episodes/{EP}/shot-states", f"/api/episodes/{EP}/credits",
                f"/api/episodes/{EP}/stalls", "/api/models"]


@pytest.mark.parametrize("path", REVIEW_READS)
async def test_members_read_the_episode_page_routes(studio, path):
    for who in ("owner", "other", "staff", "bearer"):
        res = _client(studio, who).get(path)
        assert res.status_code == 200, f"{who} GET {path}: {res.status_code} {res.text}"
    assert _client(studio, "none").get(path).status_code == 401


async def test_the_review_reads_answer_what_the_page_shows(studio):
    member = _client(studio, "owner")
    shots = member.get(f"/api/episodes/{EP}/shot-states").json()["shots"]
    assert shots["S01"]["still_status"] == "failed" and shots["S01"]["available"] is True
    credits = member.get(f"/api/episodes/{EP}/credits").json()
    assert {"cap_credits", "spent_credits", "reserved_credits", "higgsfield_balance"} <= set(credits)
    stalls = member.get(f"/api/episodes/{EP}/stalls").json()
    assert stalls["alerts"] == [] and stalls["active_count"] == 0
    models = member.get("/api/models").json()
    assert models["models"] and all("model_id" in m for m in models["models"])


ADMIN_WRITES = [
    ("/api/budget/balance", {"balance_usd": 25.0, "note": "member try"}),
    (f"/api/episodes/{EP}/raise-cap", {"new_cap": 900}),
    (f"/api/episodes/{EP}/resume-balance", None),
    (f"/api/episodes/{EP}/approve-g108", None),
    (f"/api/episodes/{EP}/canary", None),
]


@pytest.mark.parametrize("path,body", ADMIN_WRITES)
async def test_admin_writes_stay_admin_only_even_for_the_episode_owner(studio, path, body):
    for who in ("owner", "other"):
        client = _client(studio, who)
        res = client.post(path, json=body) if body is not None else client.post(path)
        assert res.status_code == 403, f"{who} POST {path}: {res.status_code} {res.text}"
        assert res.json()["detail"] == "Admin access required."


async def test_all_stalls_stay_admin_only(studio):
    assert _client(studio, "owner").get("/api/stalls").status_code == 403
    assert _client(studio, "staff").get("/api/stalls").status_code == 200


async def test_me_says_who_is_signed_in(studio):
    assert _client(studio, "owner").get("/api/me").json() == {
        "user_id": studio.owner_id, "name": "Owner", "email": OWNER[0], "role": "member"}
    assert _client(studio, "staff").get("/api/me").json() == {
        "user_id": studio.staff_id, "name": "Staff", "email": STAFF[0], "role": "admin"}
    # the admin-secret login is a session without a user row; a Bearer caller has no session at all
    assert _client(studio, "admin").get("/api/me").json() == {
        "user_id": "admin", "name": "Admin", "email": None, "role": "admin"}
    assert _client(studio, "bearer").get("/api/me").json() == {
        "user_id": None, "name": "Admin", "email": None, "role": "admin"}
    assert _client(studio, "none").get("/api/me").status_code == 401


async def test_the_episode_list_names_each_owner(studio):
    listed = {e["episode_id"]: e for e in _client(studio, "other").get("/api/episodes").json()["episodes"]}
    assert (listed[EP]["user_id"], listed[EP]["owner_name"]) == (studio.owner_id, "Owner")
    for ep in (LEGACY, NO_OWNER):
        assert (listed[ep]["user_id"], listed[ep]["owner_name"]) == (None, None)
    assert {"live_mode", "g108_approved", "created_at"} <= set(listed[EP])


async def test_shot_states_leave_cast_details_out_for_who_may_not_see_the_cast(studio):
    for who in ("owner", "staff", "admin", "bearer"):
        body = _client(studio, who).get(f"/api/episodes/{EP}/shot-states").json()
        shot = body["shots"]["S01"]
        assert body["cast_hidden"] is False, who
        assert (shot["still_error"], shot["moderation_escalation"], shot["moderation_lint"]) == (
            STILL_ERROR, ESCALATION, LINT), who
        assert body["shots"]["S02"]["clip_error"] == CLIP_ERROR, who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}/shot-states").json()
    shot, clip_shot = hidden["shots"]["S01"], hidden["shots"]["S02"]
    assert hidden["cast_hidden"] is True
    assert (shot["still_status"], clip_shot["clip_status"]) == ("failed", "failed")
    assert (shot["still_error"], clip_shot["clip_error"], shot["moderation_lint"]) == (None, None, {})
    assert shot["moderation_escalation"] == {"kind": "still", "steps": ["reword"], "blocked_job_id": "job-s01"}
    assert shot["moderation"] == {"still": {"steps": ["reword"], "retry": {"n": 1, "step": "reword"}}}
    assert "raincoat" not in json.dumps(hidden)

    # an episode without an owner: every member may change it, so every member sees its cast
    for ep in (LEGACY, NO_OWNER):
        body = _client(studio, "other").get(f"/api/episodes/{ep}/shot-states").json()
        assert body["cast_hidden"] is False and body["shots"]["S01"]["still_error"] == STILL_ERROR
