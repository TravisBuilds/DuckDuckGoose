"""Member reads for the creator-first episode page and the studio shell.

The episode page shows a member the scene pictures they approve, the run's credits, stall alerts and
model limits, so those GET routes allow members (read only). GET /api/me tells the shell who is signed
in, and GET /api/episodes names each episode's owner. The admin-only writes (balance, cap, G1.08,
canary) stay admin-only, and the shot states leave the free text that can quote a prompt (and with it
the cast) out for a member who may not see the episode's cast (docs/AUTHENTICATION.md); so do the
shots (the prompt itself and the QC results), the clip plans (the clip prompt and what is read from
it), the audit trail (each entry's details) and the run state (G2 notes, failed shot errors and all
of a shot's outcome but its status, picture and clip).
"""
from __future__ import annotations

import copy
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


PROMPT = "S01: a tall woman in a mustard raincoat waits at the bus stop"


def _record_shots() -> None:
    """Each episode has a recorded shot: its still and the prompt it was made from."""
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        for ep in (EP, LEGACY, NO_OWNER):
            conn.execute(
                "INSERT INTO shots (id, episode_id, shot_id, status, still_url, retries, prompt) "
                "VALUES (?, ?, 'S01', 'still_complete', ?, 0, ?)", (f"{ep}-S01", ep, f"/stills/{ep}/S01.png", PROMPT))


async def test_shots_leave_the_prompt_out_for_who_may_not_see_the_cast(studio):
    _record_shots()
    for who in ("owner", "staff", "admin", "bearer"):
        body = _client(studio, who).get(f"/api/episodes/{EP}/shots").json()
        assert body["cast_hidden"] is False, who
        assert body["shots"][0]["prompt"] == PROMPT, who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}/shots").json()
    shot = hidden["shots"][0]
    assert hidden["cast_hidden"] is True
    assert (shot["shot_id"], shot["status"], shot["still_url"]) == ("S01", "still_complete", f"/stills/{EP}/S01.png")
    assert shot["prompt"] is None
    assert "raincoat" not in json.dumps(hidden)

    # an episode without an owner: every member may change it, so every member sees its prompts
    for ep in (LEGACY, NO_OWNER):
        body = _client(studio, "other").get(f"/api/episodes/{ep}/shots").json()
        assert body["cast_hidden"] is False and body["shots"][0]["prompt"] == PROMPT


REJECTED_FOR = "the woman in the mustard raincoat should face the bus"


def _run_at_g2():
    """Each episode run is parked at G2 with S01 turned down, for a reason that names the cast."""
    def handle_for(workflow_id, *_a, **_k):
        handle = MagicMock()
        handle.query = AsyncMock(return_value={
            "episode_id": workflow_id, "stage": "stills", "current_gate": "G2_SCENE_STILLS",
            "approvals": {"g1_shot_plan": True}, "shots": 2, "shot_decisions": {"S01": "rejected"},
            "rejection_reasons": {"S01": REJECTED_FOR}})
        handle.describe = AsyncMock(side_effect=RuntimeError("not described in this test"))
        return handle

    client = MagicMock()
    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    return client


async def test_the_run_state_leaves_out_why_a_picture_was_turned_down_for_who_may_not_see_the_cast(studio):
    studio.temporal_client = _run_at_g2()
    for who in ("owner", "staff", "admin", "bearer"):
        body = _client(studio, who).get(f"/api/episodes/{EP}").json()
        assert (body["shot_decisions"], body["rejection_reasons"]) == ({"S01": "rejected"}, {"S01": REJECTED_FOR}), who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}").json()
    assert (hidden["shot_decisions"], hidden["rejection_reasons"]) == ({"S01": "rejected"}, {})
    assert "raincoat" not in json.dumps(hidden)

    for ep in (LEGACY, NO_OWNER):
        body = _client(studio, "other").get(f"/api/episodes/{ep}").json()
        assert body["rejection_reasons"] == {"S01": REJECTED_FOR}, ep


CLIP_PLANS = {"S01": {"version": 2, "clip_prompt": "The woman in the mustard raincoat steps onto the bus",
                      "clip_prompt_is_still_prompt": False, "qc_fail_if": ["the raincoat is not mustard"],
                      "resynced": ["clip_prompt"]}}
CLIP_LINT = "lint: 'mustard raincoat' is not in the kit"


def _clip_plans():
    """Each episode run has launched S01, whose clip prompt and fail-if line name the cast."""
    answers = {"clip_plans": CLIP_PLANS, "still_versions": {"S01": {"current_version": 2}},
               "still_version_plan": {"shot_meta": {"count": 1}}}

    def handle_for(workflow_id, *_a, **_k):
        handle = MagicMock()
        handle.query = AsyncMock(side_effect=lambda name, *a, **k: copy.deepcopy(answers[name]))
        return handle

    client = MagicMock()
    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    return client


async def test_clip_plans_leave_the_prompt_out_for_who_may_not_see_the_cast(studio, monkeypatch):
    from hfvg import still_selection
    monkeypatch.setattr(still_selection, "lint_problems", lambda *a, **k: [CLIP_LINT])
    studio.temporal_client = _clip_plans()
    for who in ("owner", "staff", "admin", "bearer"):
        res = _client(studio, who).get(f"/api/episodes/{EP}/clip-plans")
        assert res.status_code == 200, f"{who}: {res.text}"
        assert res.json() == {"S01": {**CLIP_PLANS["S01"], "lint_problems": [CLIP_LINT], "cast_hidden": False}}, who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}/clip-plans")
    assert hidden.status_code == 200, hidden.text
    assert hidden.json() == {"S01": {"version": 2, "resynced": ["clip_prompt"], "clip_prompt": None,
                                     "clip_prompt_is_still_prompt": None, "qc_fail_if": [], "lint_problems": [],
                                     "cast_hidden": True}}
    assert "raincoat" not in hidden.text

    # an episode without an owner: every member may change it, so every member sees its clip prompts
    for ep in (LEGACY, NO_OWNER):
        plan = _client(studio, "other").get(f"/api/episodes/{ep}/clip-plans").json()["S01"]
        assert (plan["clip_prompt"], plan["qc_fail_if"], plan["cast_hidden"]) == (
            CLIP_PLANS["S01"]["clip_prompt"], ["the raincoat is not mustard"], False), ep


async def test_clip_plans_of_an_episode_without_a_run_answer_404(studio):
    from temporalio.service import RPCError, RPCStatusCode
    gone = MagicMock(query=AsyncMock(side_effect=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")))
    studio.temporal_client = MagicMock(get_workflow_handle=MagicMock(return_value=gone))
    for who in ("owner", "other", "staff"):
        res = _client(studio, who).get(f"/api/episodes/{EP}/clip-plans")
        assert (res.status_code, res.json()["detail"]) == (404, f"No workflow found for episode {EP}"), who

    # a run that cannot answer right now is not a missing one
    down = MagicMock(query=AsyncMock(side_effect=RuntimeError("worker down")))
    studio.temporal_client = MagicMock(get_workflow_handle=MagicMock(return_value=down))
    res = _client(studio, "owner").get(f"/api/episodes/{EP}/clip-plans")
    assert res.status_code == 503 and res.json()["detail"].startswith("Could not read clip plans")


REDO_NOTE = "S01 redo: prompt_append='the mustard raincoat flaps in the wind'"


async def test_the_audit_trail_leaves_the_details_out_for_who_may_not_see_the_cast(studio):
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        for ep in (EP, LEGACY, NO_OWNER):
            conn.execute("INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, 'retry_shot', ?, 'Owner')",
                         (ep, REDO_NOTE))
    for who in ("owner", "staff", "admin", "bearer"):
        body = _client(studio, who).get(f"/api/episodes/{EP}/audit").json()
        assert body["cast_hidden"] is False, who
        assert [(e["action"], e["details"]) for e in body["entries"]] == [("retry_shot", REDO_NOTE)], who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}/audit").json()
    assert hidden["cast_hidden"] is True
    assert [(e["action"], e["user"], e["details"]) for e in hidden["entries"]] == [("retry_shot", "Owner", None)]
    assert hidden["entries"][0]["timestamp"]
    assert "raincoat" not in json.dumps(hidden)

    for ep in (LEGACY, NO_OWNER):
        body = _client(studio, "other").get(f"/api/episodes/{ep}/audit").json()
        assert body["cast_hidden"] is False and body["entries"][0]["details"] == REDO_NOTE, ep


QC = {"still_qc": {"pre_approved": True, "verdict": "PASS", "notes": "the raincoat reads as mustard"},
      "clip_qc": {"verdict": "FAIL", "constraints": ["the raincoat is not mustard"],
                  "results": [{"constraint": "the raincoat is not mustard", "failed": True}]},
      "moderation": {"prompt": PROMPT, "provider_message": "flagged 'a mustard raincoat'"}}


async def test_shots_leave_the_qc_results_out_for_who_may_not_see_the_cast(studio):
    _record_shots()
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        conn.execute("UPDATE shots SET qc_results = ?", (json.dumps(QC),))
    for who in ("owner", "staff", "admin", "bearer"):
        shot = _client(studio, who).get(f"/api/episodes/{EP}/shots").json()["shots"][0]
        assert (shot["qc_results"], shot["pre_approved"]) == (QC, True), who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}/shots").json()
    shot = hidden["shots"][0]
    assert (hidden["cast_hidden"], shot["qc_results"], shot["prompt"]) == (True, {}, None)
    assert shot["pre_approved"] is True  # read from the QC results before they are left out
    assert "raincoat" not in json.dumps(hidden)

    for ep in (LEGACY, NO_OWNER):
        shot = _client(studio, "other").get(f"/api/episodes/{ep}/shots").json()["shots"][0]
        assert shot["qc_results"] == QC, ep


FAILED_SHOTS = [{"shot_id": "S02", "status": "needs_review", "reason": CLIP_ERROR}, "S03"]
OUTCOMES = {
    "S01": {"shot_id": "S01", "status": "completed", "still_url": "/stills/S01.png", "clip_url": "/clips/S01.mp4",
            "pre_approved": False, "qc_constraints": {"fail_if": ["the raincoat is not mustard"]},
            "moderation_escalation": ESCALATION, "clip_plan_update": {"clip_prompt": PROMPT}},
    "S02": {"status": "needs_review", "error": CLIP_ERROR, "still_url": "/stills/S02.png"},
}


def _run_with_outcomes():
    """Each episode run is making clips: S01's outcome carries its QC lines, S02 failed for a reason
    that quotes its prompt."""
    def handle_for(workflow_id, *_a, **_k):
        handle = MagicMock()
        handle.query = AsyncMock(return_value={
            "episode_id": workflow_id, "stage": "clips", "approvals": {"g1_shot_plan": True, "g2_scene_stills": True},
            "shots": 3, "failed_shots": FAILED_SHOTS, "shot_outcomes": OUTCOMES})
        handle.describe = AsyncMock(side_effect=RuntimeError("not described in this test"))
        return handle

    client = MagicMock()
    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    return client


async def test_the_run_state_leaves_out_failed_shot_errors_and_qc_lines_for_who_may_not_see_the_cast(studio):
    studio.temporal_client = _run_with_outcomes()
    for who in ("owner", "staff", "admin", "bearer"):
        body = _client(studio, who).get(f"/api/episodes/{EP}").json()
        assert (body["failed_shots"], body["shot_outcomes"]) == (FAILED_SHOTS, OUTCOMES), who

    hidden = _client(studio, "other").get(f"/api/episodes/{EP}").json()
    assert hidden["failed_shots"] == [{"shot_id": "S02", "status": "needs_review"}, "S03"]
    assert hidden["shot_outcomes"] == {
        "S01": {"shot_id": "S01", "status": "completed", "still_url": "/stills/S01.png", "clip_url": "/clips/S01.mp4",
                "pre_approved": False},
        "S02": {"status": "needs_review", "still_url": "/stills/S02.png"}}
    assert "raincoat" not in json.dumps(hidden)

    for ep in (LEGACY, NO_OWNER):
        body = _client(studio, "other").get(f"/api/episodes/{ep}").json()
        assert (body["failed_shots"], body["shot_outcomes"]) == (FAILED_SHOTS, OUTCOMES), ep
