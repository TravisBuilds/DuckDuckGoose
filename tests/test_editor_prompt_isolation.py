"""An episode's prompts reach only whoever may read them (docs/AUTHENTICATION.md, docs/WORLDS.md):
admins, the owner of its Universe and whoever may change the episode. Here a Universe has two
episodes made by two other members. The editor of one sees the Universe, its cast and the other
episode's shots, cast and shot plan, but none of the other episode's prompts or the text that can
quote them (end and clip prompts, fail-if lines, prompt edits and their notes, errors, moderation and
lint messages, G2 notes, audit details, why its shots.json cannot be read). The Universe's owner and
admins read them for every episode, each editor for their own. Every text is invented.

The editor of one episode no longer sees another episode of the Universe at all
(tests/test_episode_visibility.py). These tests run with every member seeing every episode (the rule
before), so the prompts left out here stay pinned as the second line behind that."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from hfvg import paths, shot_plan_edits

pytestmark = pytest.mark.usefixtures("every_member_sees_every_episode")

ADMIN = "editor-prompts-admin-secret-0123456789abcdef"
USERS = {  # who: (name, email, password, role)
    "universe_owner": ("Remy Owner", "remy.owner@prompts.test", "owner-pass-123", "member"),
    "editor": ("Ada Editor", "ada.editor@prompts.test", "editor-pass-123", "member"),
    "maker": ("Bo Maker", "bo.maker@prompts.test", "maker-pass-123", "member"),
    "outsider": ("Cy Outsider", "cy.outsider@prompts.test", "outsider-pass-123", "member"),
    "staff": ("Sol Staff", "sol.staff@prompts.test", "staff-pass-123", "admin"),
}
EP_A = "ep81edit"  # made by the editor
EP_B = "ep82edit"  # made by the maker, in the same Universe
WORDS = {EP_A: "kite", EP_B: "submarine"}  # only the episode's prompts, and what quotes them, use its word
READ_B = ("universe_owner", "maker", "staff", "admin", "bearer")  # who reads EP_B's prompts
CAST_HIDDEN = "Only the people working on this episode (and admins) can see its cast"
SHOT_PLAN_HIDDEN = "Only the people working on this episode (and admins) can see its shot plan"


def _text(ep: str) -> dict:
    """The episode's prompts and the texts that quote them."""
    word = WORDS[ep]
    return {
        "prompt": f"A brass {word} drifts past the harbour wall at dawn",
        "end_prompt": f"The {word} comes to rest against the harbour wall",
        "clip_prompt": f"The {word} rises slowly over the harbour",
        "old_prompt": f"A {word} waits by the harbour wall",
        "note": f"the {word} needs more brass",
        "fail_if": f"the {word} is missing",
        "rejected_for": f"the {word} should face the lighthouse",
        "redo": f"S01 redo: prompt_append='the {word} glints in the sun'",
        "error": f"Content blocked (still); escalated to human review. Provider: flagged 'a brass {word}'",
        "lint": f"lint: 'brass {word}' is not in the kit",
    }


def _shot(ep: str, **extra) -> dict:
    text = _text(ep)
    return {"id": "S01", "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16", "ref_keys": [],
            "prompt": text["prompt"], "end_prompt": text["end_prompt"], "clip_prompt": text["clip_prompt"],
            "cast": ["nobody"], **extra}


def _write_shots(ep: str, shots: list[dict]) -> None:
    folder = paths.episode_dir(ep)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": ep, "shots": shots}), encoding="utf-8")


def _qc(ep: str) -> dict:
    text = _text(ep)
    return {"still_qc": {"pre_approved": True, "verdict": "PASS", "notes": f"{text['prompt']}: reads well"},
            "clip_qc": {"verdict": "FAIL", "constraints": [text["fail_if"]],
                        "results": [{"constraint": text["fail_if"], "failed": True}]},
            "moderation": {"prompt": text["prompt"], "provider_message": text["error"]}}


def _run(ep: str) -> dict:
    """The episode's run: S01 turned down at G2, then failed, its outcome carrying its QC lines."""
    text = _text(ep)
    return {"episode_id": ep, "stage": "clips", "current_gate": "G2_SCENE_STILLS", "approvals": {"g1_shot_plan": True},
            "shots": 1, "shot_decisions": {"S01": "rejected"}, "rejection_reasons": {"S01": text["rejected_for"]},
            "failed_shots": [{"shot_id": "S01", "status": "needs_review", "reason": text["error"]}],
            "shot_outcomes": {"S01": {"shot_id": "S01", "status": "needs_review", "still_url": f"/stills/{ep}/S01.png",
                                      "clip_url": None, "pre_approved": False,
                                      "qc_constraints": {"fail_if": [text["fail_if"]]},
                                      "clip_plan_update": {"clip_prompt": text["clip_prompt"]}}},
            "shot_workflow_ids": {"S01": f"{ep}-shot-S01"}}


def _child(ep: str) -> dict:
    """S01's child workflow: a still and a clip that failed for reasons quoting the prompt."""
    text = _text(ep)
    return {"still_status": "failed", "clip_status": "failed", "still_error": text["error"], "clip_error": text["error"],
            "moderation": {"still": {"steps": ["reword"], "retry": {"n": 1, "step": "reword"}, "log": [text["prompt"]]}},
            "moderation_escalation": {"kind": "still", "provider_message": text["error"], "reason": text["rejected_for"],
                                      "steps": ["reword"], "blocked_job_id": f"job-{ep}"},
            "pre_approved": False, "moderation_lint": {"decision": "advisory", "advisory_violations": [text["lint"]]}}


def _clip_plans(ep: str) -> dict:
    text = _text(ep)
    return {"S01": {"version": 2, "clip_prompt": text["clip_prompt"], "clip_prompt_is_still_prompt": False,
                    "qc_fail_if": [text["fail_if"]], "resynced": ["clip_prompt"]}}


class _NoRuns:
    """temporal_client.list_workflows(...): no other runs."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _temporal():
    """Each episode's run (its state, clip plans and still versions) and its shot's child workflow;
    none of them is open, so the shot plan can change."""
    def handle_for(workflow_id, *_a, **_k):
        ep, _, shot = workflow_id.partition("-shot-")
        if ep not in WORDS:
            answers = {}
        elif shot:
            answers = {"get_state": _child(ep)}
        else:
            answers = {"get_state": _run(ep), "clip_plans": _clip_plans(ep),
                       "still_versions": {"S01": {"current_version": 2}},
                       "still_version_plan": {"shot_meta": {"count": 1}}}

        def query(name, *args, **kwargs):
            if name not in answers:
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            return copy.deepcopy(answers[name])

        handle = MagicMock()
        handle.query = AsyncMock(side_effect=query)
        handle.describe = AsyncMock(side_effect=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
        return handle

    client = MagicMock()
    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    client.list_workflows = MagicMock(side_effect=lambda *_a, **_k: _NoRuns())
    return client


def _record(user_ids: dict) -> None:
    """Each episode's recorded shot (its prompt and QC results), an audit entry quoting a redo note,
    and its maker's shot plan edit of the prompt and the place, with a note."""
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        for ep, who in ((EP_A, "editor"), (EP_B, "maker")):
            conn.execute("INSERT INTO shots (id, episode_id, shot_id, status, still_url, retries, prompt, qc_results) "
                         "VALUES (?, ?, 'S01', 'still_complete', ?, 0, ?, ?)",
                         (f"{ep}-S01", ep, f"/stills/{ep}/S01.png", _text(ep)["prompt"], json.dumps(_qc(ep))))
            conn.execute("INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, 'retry_shot', ?, ?)",
                         (ep, _text(ep)["redo"], USERS[who][0]))
    for ep, who in ((EP_A, "editor"), (EP_B, "maker")):
        text = _text(ep)
        shot_plan_edits.record(ep, [{"shot_id": "S01", "field": "prompt", "old": text["old_prompt"], "new": text["prompt"]},
                                    {"shot_id": "S01", "field": "place", "old": None, "new": "harbour-wall"}],
                               actor=user_ids[who], actor_name=USERS[who][0], note=text["note"])


@pytest.fixture
async def universe(tmp_path, monkeypatch):
    """The Universe's owner made the Universe; the editor made EP_A and the maker EP_B, both in it
    (put there by an admin). The outsider has nothing in it; staff is an admin user."""
    import bcrypt

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX"):
        monkeypatch.delenv(var, raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    user_ids = {}
    for who, (name, email, password, role) in USERS.items():
        user_ids[who] = (await users.create_user(name=name, email=email, role=role, password=password))["user_id"]
    await create_episode(db, EP_A, user_id=user_ids["editor"])
    await create_episode(db, EP_B, user_id=user_ids["maker"])
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
    made = _client(api_main, "universe_owner").post("/api/worlds", json={"name": "Copper Harbour"})
    assert made.status_code == 200, made.text
    api_main.series_id = made.json()["world"]["series_id"]
    admin = _client(api_main, "admin")
    for ep in (EP_A, EP_B):
        linked = admin.post(f"/api/episodes/{ep}/series", json={"series_id": api_main.series_id})
        assert linked.status_code == 200, linked.text
        _write_shots(ep, [_shot(ep)])
    _record(user_ids)
    return api_main


def _client(api_main, who: str):
    from fastapi.testclient import TestClient
    client = TestClient(api_main.app, raise_server_exceptions=False)
    if who == "bearer":
        client.headers["Authorization"] = f"Bearer {ADMIN}"
        return client
    body = {"admin_secret": ADMIN} if who == "admin" else {"email": USERS[who][1], "password": USERS[who][2]}
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _clients(api_main) -> dict:
    return {who: _client(api_main, who) for who in (*USERS, "admin", "bearer")}


def _read(client, path: str, method: str = "GET"):
    res = client.request(method, path)
    assert res.status_code == 200, f"{method} {path}: {res.status_code} {res.text}"
    return res


# ------------------------------------------------------------------------------------- the cast


async def test_the_cast_gives_the_editor_of_another_episode_its_shots_without_their_prompts(universe):
    clients = _clients(universe)
    for who in READ_B:
        body = _read(clients[who], f"/api/episodes/{EP_B}/cast").json()
        assert body["prompts_hidden"] is False, who
        assert body["shots"]["shots"][0]["prompt"] == _text(EP_B)["prompt"], who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/cast")
    body = res.json()
    shot = body["shots"]["shots"][0]
    assert (body["prompts_hidden"], body["can_edit"]) == (True, False)
    assert body["library"] == {"kind": "series", "id": universe.series_id}
    assert (shot["shot_id"], shot["prompt"]) == ("S01", None)
    assert shot["cast"] == _read(clients["maker"], f"/api/episodes/{EP_B}/cast").json()["shots"]["shots"][0]["cast"]
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/cast").json()
    assert (own["prompts_hidden"], own["shots"]["shots"][0]["prompt"]) == (False, _text(EP_A)["prompt"])

    res = clients["outsider"].get(f"/api/episodes/{EP_B}/cast")
    assert (res.status_code, res.json()["detail"]) == (404, CAST_HIDDEN)


async def test_the_cast_does_not_quote_another_episode_s_shots_json_that_cannot_be_read(universe):
    _write_shots(EP_B, [_shot(EP_B, clip_prompt=[_text(EP_B)["clip_prompt"]])])
    clients = _clients(universe)
    for who in READ_B:
        error = _read(clients[who], f"/api/episodes/{EP_B}/cast").json()["shots"]["error"]
        assert error.startswith("shots.json cannot be read: ") and WORDS[EP_B] in error, who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/cast")
    assert res.json()["shots"]["error"] == "shots.json cannot be read"
    assert WORDS[EP_B] not in res.text


# ------------------------------------------------------------------------------------- the shot plan


async def test_the_shot_plan_gives_the_editor_of_another_episode_no_prompts_prompt_edits_or_notes(universe):
    clients = _clients(universe)
    text = _text(EP_B)
    for who in READ_B:
        plan = _read(clients[who], f"/api/episodes/{EP_B}/shot-plan").json()
        shot = plan["shots"][0]
        assert plan["prompts_hidden"] is False, who
        assert (shot["prompt"], shot["end_prompt"], shot["clip_prompt"]) == (
            text["prompt"], text["end_prompt"], text["clip_prompt"]), who
        assert [(h["field"], h["old"], h["new"], h["note"]) for h in shot["history"]] == [
            ("place", None, "harbour-wall", text["note"]), ("prompt", text["old_prompt"], text["prompt"], text["note"])], who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/shot-plan")
    plan = res.json()
    shot = plan["shots"][0]
    assert (plan["prompts_hidden"], plan["may_change"], plan["can_edit"]) == (True, False, False)
    assert (shot["shot_id"], shot["prompt"], shot["end_prompt"], shot["clip_prompt"]) == ("S01", None, None, None)
    assert [(h["field"], h["old"], h["new"], h["note"], h["actor_name"]) for h in shot["history"]] == [
        ("place", None, "harbour-wall", None, "Bo Maker"), ("prompt", None, None, None, "Bo Maker")]
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/shot-plan").json()
    assert (own["prompts_hidden"], own["can_edit"], own["shots"][0]["prompt"]) == (False, True, _text(EP_A)["prompt"])
    assert own["shots"][0]["history"][1]["new"] == _text(EP_A)["prompt"]

    res = clients["outsider"].get(f"/api/episodes/{EP_B}/shot-plan")
    assert (res.status_code, res.json()["detail"]) == (404, SHOT_PLAN_HIDDEN)


async def test_the_shot_plan_does_not_quote_another_episode_s_shots_json_that_cannot_be_planned(universe):
    _write_shots(EP_B, [_shot(EP_B, clip_prompt=[_text(EP_B)["clip_prompt"]])])
    clients = _clients(universe)
    for who in READ_B:
        errors = _read(clients[who], f"/api/episodes/{EP_B}/shot-plan").json()["errors"]
        assert errors[0].startswith("shots.json cannot be planned: ") and WORDS[EP_B] in errors[0], who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/shot-plan")
    assert res.json()["errors"] == ["shots.json cannot be planned"]
    assert WORDS[EP_B] not in res.text


# ------------------------------------------------------------------------------------- the run's reads


async def test_the_shots_leave_another_episode_s_prompts_and_qc_results_out_for_its_universe_editors(universe):
    clients = _clients(universe)
    for who in READ_B:
        shot = _read(clients[who], f"/api/episodes/{EP_B}/shots").json()["shots"][0]
        assert (shot["prompt"], shot["qc_results"]) == (_text(EP_B)["prompt"], _qc(EP_B)), who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/shots")
    body = res.json()
    shot = body["shots"][0]
    assert (body["cast_hidden"], shot["shot_id"], shot["prompt"], shot["qc_results"]) == (True, "S01", None, {})
    assert (shot["status"], shot["still_url"], shot["pre_approved"]) == ("still_complete", f"/stills/{EP_B}/S01.png", True)
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/shots").json()
    assert (own["cast_hidden"], own["shots"][0]["prompt"]) == (False, _text(EP_A)["prompt"])
    # the rule goes both ways: the maker of EP_B does not read EP_A's prompts
    res = _read(clients["maker"], f"/api/episodes/{EP_A}/shots")
    assert (res.json()["cast_hidden"], res.json()["shots"][0]["prompt"]) == (True, None)
    assert WORDS[EP_A] not in res.text


async def test_the_clip_plans_leave_another_episode_s_clip_prompts_out_for_its_universe_editors(universe, monkeypatch):
    from hfvg import still_selection
    monkeypatch.setattr(still_selection, "lint_problems", lambda episode_id, *a, **k: [_text(episode_id)["lint"]])
    clients = _clients(universe)
    text = _text(EP_B)
    for who in READ_B:
        plan = _read(clients[who], f"/api/episodes/{EP_B}/clip-plans").json()["S01"]
        assert (plan["clip_prompt"], plan["qc_fail_if"], plan["lint_problems"], plan["cast_hidden"]) == (
            text["clip_prompt"], [text["fail_if"]], [text["lint"]], False), who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/clip-plans")
    assert res.json() == {"S01": {"version": 2, "resynced": ["clip_prompt"], "clip_prompt": None,
                                  "clip_prompt_is_still_prompt": None, "qc_fail_if": [], "lint_problems": [],
                                  "cast_hidden": True}}
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/clip-plans").json()["S01"]
    assert (own["clip_prompt"], own["cast_hidden"]) == (_text(EP_A)["clip_prompt"], False)


async def test_the_audit_trail_leaves_another_episode_s_details_out_for_its_universe_editors(universe):
    clients = _clients(universe)
    for who in READ_B:
        entries = _read(clients[who], f"/api/episodes/{EP_B}/audit").json()["entries"]
        assert [(e["action"], e["details"]) for e in entries] == [("retry_shot", _text(EP_B)["redo"])], who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/audit")
    body = res.json()
    assert body["cast_hidden"] is True
    assert [(e["action"], e["user"], e["details"]) for e in body["entries"]] == [("retry_shot", "Bo Maker", None)]
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/audit").json()
    assert (own["cast_hidden"], own["entries"][0]["details"]) == (False, _text(EP_A)["redo"])


async def test_the_shot_states_leave_another_episode_s_free_text_out_for_its_universe_editors(universe):
    clients = _clients(universe)
    for who in READ_B:
        shot = _read(clients[who], f"/api/episodes/{EP_B}/shot-states").json()["shots"]["S01"]
        assert (shot["still_error"], shot["clip_error"], shot["moderation_lint"]) == (
            _text(EP_B)["error"], _text(EP_B)["error"], _child(EP_B)["moderation_lint"]), who
        assert shot["moderation_escalation"] == _child(EP_B)["moderation_escalation"], who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}/shot-states")
    body = res.json()
    shot = body["shots"]["S01"]
    assert body["cast_hidden"] is True
    assert (shot["still_status"], shot["clip_status"]) == ("failed", "failed")
    assert (shot["still_error"], shot["clip_error"], shot["moderation_lint"]) == (None, None, {})
    assert shot["moderation_escalation"] == {"kind": "still", "steps": ["reword"], "blocked_job_id": f"job-{EP_B}"}
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/shot-states").json()
    assert (own["cast_hidden"], own["shots"]["S01"]["still_error"]) == (False, _text(EP_A)["error"])


async def test_the_run_state_leaves_another_episode_s_notes_and_errors_out_for_its_universe_editors(universe):
    clients = _clients(universe)
    run = _run(EP_B)
    for who in READ_B:
        body = _read(clients[who], f"/api/episodes/{EP_B}").json()
        assert (body["rejection_reasons"], body["failed_shots"], body["shot_outcomes"]) == (
            run["rejection_reasons"], run["failed_shots"], run["shot_outcomes"]), who

    res = _read(clients["editor"], f"/api/episodes/{EP_B}")
    body = res.json()
    assert (body["shot_decisions"], body["rejection_reasons"]) == ({"S01": "rejected"}, {})
    assert body["failed_shots"] == [{"shot_id": "S01", "status": "needs_review"}]
    assert body["shot_outcomes"] == {"S01": {"shot_id": "S01", "status": "needs_review",
                                             "still_url": f"/stills/{EP_B}/S01.png", "clip_url": None,
                                             "pre_approved": False}}
    assert WORDS[EP_B] not in res.text

    own = _read(clients["editor"], f"/api/episodes/{EP_A}").json()
    assert own["rejection_reasons"] == {"S01": _text(EP_A)["rejected_for"]}


async def test_the_preflight_checks_another_episode_s_cast_only_for_who_may_read_its_prompts(universe):
    clients = _clients(universe)
    for who in READ_B:
        body = _read(clients[who], f"/api/episodes/{EP_B}/preflight", "POST").json()
        assert body["cast_hidden"] is False and len(body["cast_errors"]) == 1, who
        assert "nobody" in body["cast_errors"][0], who

    body = _read(clients["editor"], f"/api/episodes/{EP_B}/preflight", "POST").json()
    assert (body["cast_hidden"], body["cast_errors"]) == (True, [])
    assert list(body["shots"]) == ["S01"]

    own = _read(clients["editor"], f"/api/episodes/{EP_A}/preflight", "POST").json()
    assert own["cast_hidden"] is False and len(own["cast_errors"]) == 1


async def test_the_preflight_does_not_quote_another_episode_s_shots_json_that_cannot_be_read(universe):
    _write_shots(EP_B, [_shot(EP_B, clip_prompt=[_text(EP_B)["clip_prompt"]])])
    clients = _clients(universe)
    for who in READ_B:
        res = clients[who].post(f"/api/episodes/{EP_B}/preflight")
        assert res.status_code == 400 and res.json()["detail"].startswith("Invalid shots.json: "), who
        assert WORDS[EP_B] in res.json()["detail"], who

    res = clients["editor"].post(f"/api/episodes/{EP_B}/preflight")
    assert (res.status_code, res.json()["detail"]) == (400, "Invalid shots.json")


# ------------------------------------------------------------------------------------- every read


EDITOR_READS = [  # (method, path, whether the reader of EP_B's prompts finds its word in the answer)
    ("GET", "/api/episodes/{ep}", True),
    ("GET", "/api/episodes/{ep}/shots", True),
    ("GET", "/api/episodes/{ep}/clip-plans", True),
    ("GET", "/api/episodes/{ep}/audit", True),
    ("GET", "/api/episodes/{ep}/shot-states", True),
    ("GET", "/api/episodes/{ep}/cast", True),
    ("GET", "/api/episodes/{ep}/shot-plan", True),
    ("POST", "/api/episodes/{ep}/preflight", False),
    ("GET", "/api/episodes/{ep}/props", False),
    ("GET", "/api/episodes/{ep}/universe", False),
    ("GET", "/api/episodes/{ep}/still-versions", False),
    ("GET", "/api/episodes/{ep}/budget", False),
    ("GET", "/api/episodes/{ep}/credits", False),
    ("GET", "/api/episodes/{ep}/stalls", False),
    ("GET", "/api/episodes/{ep}/files", False),
    ("GET", "/api/episodes/{ep}/gates", False),
    ("GET", "/api/episodes", False),
    ("GET", "/api/worlds", False),
    ("GET", "/api/worlds/{sid}", False),
    ("GET", "/api/series/{sid}", False),
]


@pytest.mark.parametrize("method,path,quotes", EDITOR_READS)
async def test_no_read_gives_the_editor_of_one_episode_the_words_of_another_s_prompts(universe, method, path, quotes):
    path = path.format(ep=EP_B, sid=universe.series_id)
    res = _read(_client(universe, "editor"), path, method)
    assert WORDS[EP_B] not in res.text, f"{method} {path}"
    if quotes:
        assert WORDS[EP_B] in _read(_client(universe, "universe_owner"), path, method).text, f"{method} {path}"
