"""Item 5: episode ownership.

The owner of an episode is episodes.user_id, the session user who created its row (BEATMAP upload,
start or start again). Members write, approve and start again only their own episodes; admins (admin
session, Bearer ADMIN_SECRET) any. Episodes without an owner (no row yet, NULL or 'legacy/shared')
keep the old rule: any member may change them. Reads are unchanged.
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from hfvg import audio_mix

ADMIN = "ownership-admin-secret-0123456789abcdef-xyz"
OWNER = ("owner@ownership.test", "owner-pass-123")
OTHER = ("other@ownership.test", "other-pass-123")
EP = "ep81own"
LEGACY = "ep82old"
IDEA = "A paper kite drifts over a quiet harbor at dusk"

# Every member-allowed write that names the episode in its path: refused by the app-wide guard unless
# the caller owns the episode (or is an admin).
PATH_WRITES = [
    ("POST", "/api/episodes/{ep}/upload-prompt-kit"),
    ("POST", "/api/episodes/{ep}/upload-shots"),
    ("POST", "/api/episodes/{ep}/upload-audio-plan"),
    ("POST", "/api/episodes/{ep}/approve"),
    ("PUT", "/api/episodes/{ep}/story-shape"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/approve"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/reject"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/accept-clip"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/select-still"),
    ("POST", "/api/episodes/{ep}/resync-plans"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/retry-clip"),
    ("POST", "/api/episodes/{ep}/reopen"),
    ("POST", "/api/episodes/{ep}/reopen-from-saved"),
    ("POST", "/api/episodes/{ep}/set-live"),
    ("POST", "/api/episodes/{ep}/shots/{shot_id}/retry"),
    ("POST", "/api/episodes/{ep}/set-dry"),
    ("POST", "/api/episodes/{ep}/final-cut/reject"),
    ("POST", "/api/episodes/{ep}/start-again"),
    # cast library (api/cast_routes.py): a series cast also needs the series' owner (test_cast_api.py)
    ("POST", "/api/episodes/{ep}/series"),
    ("POST", "/api/episodes/{ep}/cast"),
    ("POST", "/api/episodes/{ep}/cast/import"),
    ("POST", "/api/episodes/{ep}/cast/shots"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/delete"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/images"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/images/{image_id}/primary"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/images/{image_id}/delete"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/sheet/estimate"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/sheet/generate"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/sheet/pick"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/sheet/reject"),
    ("POST", "/api/episodes/{ep}/cast/{character_id}/versions/{version}/approve"),
]
# The writes whose episode id is in the form / body check ownership in the handler.
BODY_WRITES = [("POST", "/api/episodes/upload"), ("POST", "/api/episodes")]
# Member-allowed POSTs that change no episode.
NOT_EPISODE_WRITES = [
    ("POST", "/api/episodes/upload/validate"),   # validates files, stores nothing
    ("POST", "/api/episodes/{episode_id}/preflight"),  # report only (read_only)
    ("POST", "/api/episodes/draft-from-idea"),
    ("POST", "/api/voices/pick"),
    ("POST", "/api/users/me/change-password"),
    ("POST", "/api/series"),  # a new series owned by the caller
]
REFUSED = "belongs to another member; only its owner or an admin can change it"


class _Workflows:
    """temporal_client.list_workflows(...) result: an async iterator over workflow infos."""

    def __init__(self, ids):
        self.ids = list(ids)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.ids:
            raise StopAsyncIteration
        return MagicMock(id=self.ids.pop(0))


def _temporal():
    client = MagicMock()
    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    handle.execute_update = AsyncMock(return_value={"status": "approved"})
    handle.signal = AsyncMock()
    handle.query = AsyncMock(return_value={})
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.list_workflows = MagicMock(side_effect=lambda *_a, **_k: _Workflows([EP]))
    client.start_workflow = AsyncMock(return_value=MagicMock(id=EP, result_run_id="run-1"))
    client.handle = handle
    return client


@pytest.fixture
async def studio(monkeypatch):
    """Two members and an admin; EP is owned by OWNER, LEGACY has the pre-ownership owner."""
    import os
    import bcrypt
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    # ~100 parametrized cases each hash two passwords and log in: the cheapest bcrypt cost keeps the
    # module inside CI's time budget (a login checks at the cost stored in the hash)
    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    owner = await users.create_user(name="Owner", email=OWNER[0], role="member", password=OWNER[1])
    await users.create_user(name="Other", email=OTHER[0], role="member", password=OTHER[1])
    await create_episode(db, EP, user_id=owner["user_id"])
    await create_episode(db, LEGACY)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
    api_main.owner_id = owner["user_id"]
    return api_main


def _client(api_main, who: str):
    from fastapi.testclient import TestClient
    # a route that trips over the Temporal stand-in answers 500: still not an ownership refusal
    client = TestClient(api_main.app, raise_server_exceptions=False)
    if who == "bearer":
        client.headers["Authorization"] = f"Bearer {ADMIN}"
        return client
    body = {"admin_secret": ADMIN} if who == "admin" else {
        "owner": {"email": OWNER[0], "password": OWNER[1]},
        "other": {"email": OTHER[0], "password": OTHER[1]}}[who]
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _owner_of(ep: str):
    import os
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as c:
        row = c.execute("SELECT user_id FROM episodes WHERE episode_id = ?", (ep,)).fetchone()
    return row[0] if row else None


def _json_part(name: str, data) -> tuple:
    return (name, io.BytesIO(json.dumps(data).encode()), "application/json")


def _draft(api_main) -> dict:
    res = _client(api_main, "owner").post("/api/episodes/draft-from-idea", json={
        "idea": IDEA, "archetype_id": None, "target_duration": 12})
    assert res.status_code == 200, res.text
    return res.json()


def _write(client, method: str, path: str, ep: str, draft: dict | None = None):
    """A request to a write route with a body good enough to get past validation."""
    url = path.format(ep=ep, shot_id="A01", character_id="ch_000000000000", version=1, image_id="ci_000000000000")
    if path == "/api/episodes/upload":
        return client.post(url, data={"episode_id": ep}, files={
            "beatmap_file": ("BEATMAP.md", io.BytesIO(draft["beatmap"].encode()), "text/markdown")})
    if path == "/api/episodes":
        return client.post(url, json={"episode_id": ep, "dry_run": True, "beatmap_content": draft["beatmap"]})
    if path.endswith("/upload-prompt-kit"):
        return client.post(url, files={"prompt_kit_file": _json_part("prompt_kit.json", draft["prompt_kit_json"])})
    if path.endswith("/upload-shots"):
        return client.post(url, files={"shots_file": _json_part("shots.json", draft["shots_json"]),
                                       "refs_file": _json_part("refs.json", draft["refs_json"])})
    if path.endswith("/upload-audio-plan"):
        return client.post(url, files={"audio_plan_file": _json_part("audio_plan.json", {"music": {"prompt": "calm"}})})
    if path.endswith("/approve") and "/shots/" not in path:
        return client.post(url, json={"gate_id": "G1_SHOT_PLAN"})
    if path.endswith("/set-live"):
        return client.post(url, json={"confirmation": "ENABLE_LIVE_MODE"})
    if path.endswith("/reopen") or path.endswith("/reopen-from-saved"):
        return client.post(url, json={"reason": "Studio"})
    return client.request(method, url, json={})


FILES = {  # route -> the file it writes
    "/api/episodes/upload": lambda ep: audio_mix.episode_dir(ep) / "BEATMAP.md",
    "/api/episodes/{ep}/upload-prompt-kit": lambda ep: audio_mix.episode_dir(ep) / "prompt_kit.json",
    "/api/episodes/{ep}/upload-shots": lambda ep: audio_mix.episode_dir(ep) / "shots.json",
    "/api/episodes/{ep}/upload-audio-plan": lambda ep: audio_mix.audio_plan_path(ep),
}


def test_every_member_write_route_is_classified():
    """A new member route must be added here: owner-checked by the guard, in its handler, or no write."""
    import os
    os.environ.setdefault("ADMIN_SECRET", ADMIN)
    sys.modules.pop("api.main", None)
    from fastapi.routing import APIRoute
    import api.main as api_main

    member_writes = {(m, r.path) for r in api_main.app.routes if isinstance(r, APIRoute)
                     and getattr(r.endpoint, "allow_members", False) for m in r.methods if m != "GET"}
    guarded = {(m, r.path) for r in api_main.app.routes if isinstance(r, APIRoute)
               and getattr(r.endpoint, "allow_members", False) and "{episode_id}" in r.path
               and not getattr(r.endpoint, "read_only", False) for m in r.methods if m != "GET"}
    as_routes = {(m, p.replace("{ep}", "{episode_id}")) for m, p in PATH_WRITES}
    assert guarded == as_routes
    assert member_writes == as_routes | set(BODY_WRITES) | set(NOT_EPISODE_WRITES)


@pytest.mark.parametrize("method,path", PATH_WRITES + BODY_WRITES)
async def test_another_member_cannot_change_the_episode(studio, method, path):
    draft = _draft(studio)
    stored = FILES.get(path, lambda ep: None)(EP)
    if stored is not None:
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_text("the owner's file")
    res = _write(_client(studio, "other"), method, path, EP, draft)
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == f"Episode {EP} {REFUSED}"
    if stored is not None:
        assert stored.read_text() == "the owner's file"
    studio.temporal_client.start_workflow.assert_not_called()
    studio.temporal_client.handle.execute_update.assert_not_called()
    studio.temporal_client.handle.signal.assert_not_called()
    assert _owner_of(EP) == studio.owner_id


@pytest.mark.parametrize("who", ["owner", "admin", "bearer"])
@pytest.mark.parametrize("method,path", PATH_WRITES + BODY_WRITES)
async def test_owner_and_admins_are_not_refused(studio, method, path, who):
    res = _write(_client(studio, who), method, path, EP, _draft(studio))
    assert res.status_code != 401, res.text
    assert not (res.status_code == 403 and REFUSED in res.text), res.text
    assert _owner_of(EP) == studio.owner_id


@pytest.mark.parametrize("who", ["owner", "admin", "bearer"])
async def test_owner_and_admins_upload_start_and_approve(studio, who):
    """The routes of the original report go all the way through for the owner and for admins."""
    draft = _draft(studio)
    client = _client(studio, who)
    for path in FILES:
        res = _write(client, "POST", path, EP, draft)
        assert res.status_code == 200, (path, res.text)
    assert (audio_mix.episode_dir(EP) / "BEATMAP.md").read_text() == draft["beatmap"]
    res = _write(client, "POST", "/api/episodes", EP, draft)
    assert res.status_code == 200, res.text
    assert studio.temporal_client.start_workflow.call_args.kwargs["id"] == EP
    res = _write(client, "POST", "/api/episodes/{ep}/approve", EP)
    assert res.status_code == 200, res.text
    studio.temporal_client.handle.execute_update.assert_awaited_once_with("approve_g1_shot_plan", False)
    assert _owner_of(EP) == studio.owner_id


async def test_the_workflow_id_spelling_is_the_same_episode(studio):
    res = _client(studio, "other").post(f"/api/episodes/{EP}-episode/approve", json={"gate_id": "G1_SHOT_PLAN"})
    assert res.status_code == 403 and REFUSED in res.json()["detail"]
    studio.temporal_client.handle.execute_update.assert_not_called()


@pytest.mark.parametrize("method,path", PATH_WRITES + BODY_WRITES)
async def test_members_still_change_episodes_without_an_owner(studio, method, path):
    res = _write(_client(studio, "other"), method, path, LEGACY, _draft(studio))
    assert not (res.status_code == 403 and REFUSED in res.text), res.text
    assert _owner_of(LEGACY) == "legacy/shared"


async def test_a_new_episode_belongs_to_the_member_who_uploads_its_beatmap(studio):
    draft = _draft(studio)
    other = _client(studio, "other")
    assert _write(other, "POST", "/api/episodes/upload", "ep83new", draft).status_code == 200
    other_id = _owner_of("ep83new")
    assert other_id not in (None, "legacy/shared", studio.owner_id)
    owner = _client(studio, "owner")
    for path in ["/api/episodes/upload", "/api/episodes", *[p for _m, p in PATH_WRITES]]:
        res = _write(owner, "POST" if path != "/api/episodes/{ep}/story-shape" else "PUT", path, "ep83new", draft)
        assert res.status_code == 403 and REFUSED in res.text, (path, res.text)
    assert _write(other, "POST", "/api/episodes/{ep}/upload-prompt-kit", "ep83new", draft).status_code == 200
    assert _write(_client(studio, "admin"), "POST", "/api/episodes/{ep}/upload-prompt-kit", "ep83new",
                  draft).status_code == 200
    assert _owner_of("ep83new") == other_id


async def test_starting_a_new_episode_records_the_member_as_its_owner(studio):
    draft = _draft(studio)
    res = _write(_client(studio, "owner"), "POST", "/api/episodes", "ep84api", draft)
    assert res.status_code == 200, res.text
    assert _owner_of("ep84api") == studio.owner_id
    studio.temporal_client.start_workflow.reset_mock()
    res = _write(_client(studio, "other"), "POST", "/api/episodes", "ep84api", draft)
    assert res.status_code == 403 and REFUSED in res.text
    studio.temporal_client.start_workflow.assert_not_called()


async def test_a_start_whose_owner_cannot_be_recorded_does_not_start(studio, monkeypatch):
    import aiosqlite

    async def locked(*_a, **_k):
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(studio, "create_episode", locked)
    res = _write(_client(studio, "owner"), "POST", "/api/episodes", "ep87lck", _draft(studio))
    assert res.status_code == 503 and "database is locked" in res.json()["detail"]
    studio.temporal_client.start_workflow.assert_not_called()
    assert _owner_of("ep87lck") is None


async def test_bearer_secret_starts_without_claiming_an_owner(studio):
    assert _write(_client(studio, "bearer"), "POST", "/api/episodes", "ep85cli", _draft(studio)).status_code == 200
    assert _owner_of("ep85cli") == "legacy/shared"
    assert _write(_client(studio, "other"), "POST", "/api/episodes/{ep}/upload-prompt-kit", "ep85cli",
                  _draft(studio)).status_code == 200


@pytest.mark.parametrize("authorization,cookie", [
    (None, None), ("Bearer not-the-admin-secret", None), ("Basic b3duZXI6cGFzcw==", None),
    (None, "no-such-session"), (f"Bearer {ADMIN}x", "expired-session"),
    ("Bearer upload-token-for-the-ownership-tests-0123456789", None)])
async def test_the_owner_check_on_its_own_takes_no_credential_for_an_admin(studio, monkeypatch,
                                                                          authorization, cookie):
    """require_episode_owner checks the credential itself rather than trusting its caller."""
    from fastapi import HTTPException
    monkeypatch.setenv("UPLOAD_TOKEN", "upload-token-for-the-ownership-tests-0123456789")
    with pytest.raises(HTTPException) as err:
        await studio.require_episode_owner(EP, authorization, cookie)
    assert err.value.status_code == 403 and REFUSED in err.value.detail
    await studio.require_episode_owner(LEGACY, authorization, cookie)
    await studio.require_episode_owner("ep86none", authorization, cookie)


async def test_the_upload_token_uploads_only_beatmaps_of_episodes_without_an_owner(studio, monkeypatch):
    """The scoped upload token is member-level and names no user (docs/AUTHENTICATION.md)."""
    token = "upload-token-for-the-ownership-tests-0123456789"
    monkeypatch.setenv("UPLOAD_TOKEN", token)
    from fastapi.testclient import TestClient
    client = TestClient(studio.app)
    client.headers["Authorization"] = f"Bearer {token}"
    draft = _draft(studio)
    stored = audio_mix.episode_dir(EP) / "BEATMAP.md"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_text("the owner's file")
    res = _write(client, "POST", "/api/episodes/upload", EP, draft)
    assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}"
    assert stored.read_text() == "the owner's file"
    for ep in (LEGACY, "ep86tok"):
        assert _write(client, "POST", "/api/episodes/upload", ep, draft).status_code == 200
        assert (audio_mix.episode_dir(ep) / "BEATMAP.md").read_text() == draft["beatmap"]
    assert _owner_of("ep86tok") == "legacy/shared"
    assert _owner_of(EP) == studio.owner_id


async def test_reads_follow_the_existing_visibility_rules(studio):
    other = _client(studio, "other")
    listed = other.get("/api/episodes")
    assert listed.status_code == 200 and EP in [e["episode_id"] for e in listed.json()["episodes"]]
    assert other.get(f"/api/episodes/{EP}/audit").status_code == 200
    res = other.post(f"/api/episodes/{EP}/preflight", json={"shots": _draft(studio)["shots_json"]["shots"]})
    assert res.status_code == 200, res.text


# ------------------------------------------------------------------ start again
def _ended_run(api_main, ep: str, dry_run: bool = True) -> list:
    """The episode's last run (run-0) failed; returns the inputs it was started with, which start-again
    reads from its history and starts the new run with."""
    from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
    from temporalio.exceptions import ApplicationError

    beatmap = audio_mix.episode_dir(ep) / "BEATMAP.md"
    beatmap.parent.mkdir(parents=True, exist_ok=True)
    beatmap.write_text("# the beatmap the run was started with\n")
    inputs = [ep, str(beatmap), dry_run, None, None, None, None, True]
    client = api_main.temporal_client
    client.handle.describe = AsyncMock(return_value=MagicMock(
        id=ep, run_id="run-0", status=WorkflowExecutionStatus.FAILED, workflow_type="EpisodeWorkflowV2"))
    client.handle.result = AsyncMock(side_effect=WorkflowFailureError(
        cause=ApplicationError("Missing required file: shots.json")))
    started = MagicMock()
    started.workflow_execution_started_event_attributes.input.payloads = ["the start inputs"]

    async def history(page_size=None):
        yield started

    client.handle.fetch_history_events = history
    client.data_converter.decode = AsyncMock(return_value=list(inputs))
    return inputs


def _audited(ep: str, action: str) -> list[str]:
    import os
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as c:
        return [r[0] for r in c.execute(
            "SELECT details FROM audit_log WHERE episode_id = ? AND action = ?", (ep, action))]


@pytest.mark.parametrize("who", ["owner", "admin", "bearer"])
async def test_the_owner_and_admins_start_the_ended_run_again(studio, who):
    inputs = _ended_run(studio, EP)
    res = _client(studio, who).post(f"/api/episodes/{EP}/start-again")
    assert res.status_code == 200, res.text
    assert (res.json()["from_run_id"], res.json()["run_id"]) == ("run-0", "run-1")
    start = studio.temporal_client.start_workflow
    start.assert_awaited_once()
    assert (start.call_args.kwargs["args"], start.call_args.kwargs["id"]) == (inputs, EP)
    [audited] = _audited(EP, "start_again")
    assert "Missing required file: shots.json" in audited
    assert _owner_of(EP) == studio.owner_id


async def test_another_member_cannot_start_the_episode_again(studio):
    _ended_run(studio, EP)
    res = _client(studio, "other").post(f"/api/episodes/{EP}/start-again")
    assert (res.status_code, res.json()["detail"]) == (403, f"Episode {EP} {REFUSED}")
    studio.temporal_client.handle.describe.assert_not_called()  # refused before the run is looked up
    studio.temporal_client.start_workflow.assert_not_called()
    assert _audited(EP, "start_again") == []
    assert _owner_of(EP) == studio.owner_id
    res = _client(studio, "other").post(f"/api/episodes/{EP}-episode/start-again")
    assert res.status_code == 403 and REFUSED in res.json()["detail"]
    studio.temporal_client.start_workflow.assert_not_called()


@pytest.mark.parametrize("who", ["other", "owner"])
@pytest.mark.parametrize("owner", ["legacy/shared", None, ""], ids=["legacy", "null", "empty"])
async def test_any_member_starts_an_episode_without_an_owner_again(studio, owner, who):
    import os
    ep = "ep88any"
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as c:
        c.execute("INSERT INTO episodes (episode_id, user_id) VALUES (?, ?)", (ep, owner))
    inputs = _ended_run(studio, ep)
    res = _client(studio, who).post(f"/api/episodes/{ep}/start-again")
    assert res.status_code == 200, res.text
    assert studio.temporal_client.start_workflow.call_args.kwargs["args"] == inputs
    assert _owner_of(ep) == owner  # starting it again claims nothing


@pytest.mark.parametrize("who,owner", [("other", "the member"), ("admin", "admin"), ("bearer", "legacy/shared")])
async def test_starting_an_episode_without_a_row_again_records_it_as_a_start_does(studio, who, owner):
    ep = "ep89row"
    _ended_run(studio, ep)
    assert _owner_of(ep) is None
    res = _client(studio, who).post(f"/api/episodes/{ep}/start-again")
    assert res.status_code == 200, res.text
    recorded = _owner_of(ep)
    if owner == "the member":
        assert recorded not in (None, "", "legacy/shared", "admin", studio.owner_id)
        studio.temporal_client.start_workflow.reset_mock()
        res = _client(studio, "owner").post(f"/api/episodes/{ep}/start-again")
        assert (res.status_code, res.json()["detail"]) == (403, f"Episode {ep} {REFUSED}")
        studio.temporal_client.start_workflow.assert_not_called()
    else:
        assert recorded == owner


async def test_a_start_again_whose_row_cannot_be_recorded_does_not_start(studio, monkeypatch):
    import aiosqlite

    async def locked(*_a, **_k):
        raise aiosqlite.OperationalError("database is locked")

    _ended_run(studio, EP)
    monkeypatch.setattr(studio, "create_episode", locked)
    res = _client(studio, "owner").post(f"/api/episodes/{EP}/start-again")
    assert res.status_code == 503 and "database is locked" in res.json()["detail"]
    studio.temporal_client.start_workflow.assert_not_called()
    assert _owner_of(EP) == studio.owner_id


async def test_the_upload_token_cannot_start_or_start_again(studio, monkeypatch):
    """The upload token is scoped to the upload routes, even for an episode without an owner."""
    from fastapi.testclient import TestClient
    token = "upload-token-for-the-ownership-tests-0123456789"
    monkeypatch.setenv("UPLOAD_TOKEN", token)
    _ended_run(studio, LEGACY)
    draft = _draft(studio)
    client = TestClient(studio.app)
    client.headers["Authorization"] = f"Bearer {token}"
    for res in (client.post(f"/api/episodes/{LEGACY}/start-again"),
                _write(client, "POST", "/api/episodes", LEGACY, draft)):
        assert (res.status_code, res.json()["detail"]) == (403, "Invalid admin secret")
    studio.temporal_client.start_workflow.assert_not_called()
    assert _owner_of(LEGACY) == "legacy/shared"


async def test_start_again_on_a_temporal_dev_server(monkeypatch):
    """A member's run stopped by the operator: another member's Start again (and approve, and uploads) is
    refused and leaves the ended run as it was; the owner fixes a file and starts it again, its new run
    refuses uploads; an admin may start it again too. The owner never changes."""
    import os
    import bcrypt
    from httpx import ASGITransport, AsyncClient
    from hfvg.users import UserManager
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from tests.test_story_revise_live import BEATMAP, studio as dev_studio, wait_for

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    ep = "ep88dev"
    plan = {"audio_plan_file": ("audio_plan.json", json.dumps({"music": {"prompt": "calm"}}).encode(),
                                "application/json")}

    def ended(state: dict) -> bool:
        return bool(state.get("run") and state["run"]["ended"])

    def shaped(body: dict) -> bool:
        return bool(body.get("story_shape"))

    async with dev_studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=False) as (env, admin):
        app = sys.modules["api.main"].app
        users = UserManager(os.environ["DATABASE_PATH"])
        await users.init_db()
        owner = await users.create_user(name="Owner", email=OWNER[0], role="member", password=OWNER[1])
        await users.create_user(name="Other", email=OTHER[0], role="member", password=OTHER[1])
        sessions = {}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as anyone:
            for email, password in (OWNER, OTHER):
                login = await anyone.post("/api/login", json={"email": email, "password": password})
                assert login.status_code == 200, login.text
                sessions[email] = login.cookies["studio_admin_token"]

        def member(email: str) -> AsyncClient:
            return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Cookie": f"studio_admin_token={sessions[email]}"})

        async with member(OWNER[0]) as mine, member(OTHER[0]) as theirs:
            res = await mine.post("/api/episodes", json={"episode_id": ep, "beatmap_content": BEATMAP.format(ep=ep),
                                                          "dry_run": True, "enable_story_shaping": True})
            assert res.status_code == 200, res.text
            first = res.json()["run_id"]
            assert _owner_of(ep) == owner["user_id"]
            await wait_for(mine, f"/api/episodes/{ep}/story-shape", shaped, "shaped")
            await env.client.get_workflow_handle(ep).terminate(reason="stopped by the operator")
            state = await wait_for(mine, f"/api/episodes/{ep}", ended, "ended")
            assert (state["run"]["run_id"], state["run"]["can_start_again"]) == (first, True)

            for res in (await theirs.post(f"/api/episodes/{ep}/start-again"),
                        await theirs.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"}),
                        await theirs.post(f"/api/episodes/{ep}/upload-audio-plan", files=plan)):
                assert (res.status_code, res.json()["detail"]) == (403, f"Episode {ep} {REFUSED}")
            assert (await theirs.get(f"/api/episodes/{ep}")).json()["run"] == state["run"]
            res = await mine.post(f"/api/episodes/{ep}/approve", json={"gate_id": "story_shape"})
            assert (res.status_code, res.json()["detail"]) == (
                409, "This run has ended (terminated: stopped by the operator). Start it again.")
            res = await mine.post(f"/api/episodes/{ep}/upload-audio-plan", files=plan)
            assert res.status_code == 200, res.text

            res = await mine.post(f"/api/episodes/{ep}/start-again")
            assert res.status_code == 200, res.text
            again = res.json()["run_id"]
            assert again not in ("", first)
            await wait_for(mine, f"/api/episodes/{ep}/story-shape", shaped, "shaped again")
            assert (await theirs.get(f"/api/episodes/{ep}")).json()["run"]["run_id"] == again
            res = await mine.post(f"/api/episodes/{ep}/upload-audio-plan", files=plan)
            assert res.status_code == 409 and f"has a run in progress (run {again})" in res.json()["detail"]

            await env.client.get_workflow_handle(ep).terminate(reason="stopped again")
            await wait_for(admin, f"/api/episodes/{ep}", ended, "ended again")
            res = await theirs.post(f"/api/episodes/{ep}/start-again")
            assert res.status_code == 403
            res = await admin.post(f"/api/episodes/{ep}/start-again")
            assert res.status_code == 200, res.text
            assert res.json()["from_run_id"] == again
        assert _owner_of(ep) == owner["user_id"]
