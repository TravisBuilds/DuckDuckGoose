"""Episodes are hidden from the members not working on them (docs/AUTHENTICATION.md).

An episode and everything about it (its page and run state, story shape, shots, clip plans, audit
trail, shot states, preflight, cast, props and places, shot plan, Universe strip, files and finished
video, budget and credits) is seen only by admins (admin sessions, admin users, the Bearer
ADMIN_SECRET), whoever may change it (the member who made it, or every member while it has no owner:
nobody can be invited to edit an episode yet) and the owner of the Universe it is in: the people who
may read its prompts. Anyone else gets the 404 an episode that does not exist gets, on every route
about it, and finds it in no list, Universe page or count.

Una owns a Universe with two episodes in it: EP, made by Mo, and SIB, made by Ed (the editor of a
sibling episode). Pat, a plain member, made MINE outside it; Oz, an outsider, has made nothing.
SHARED has no owner, so every member edits it. DONE was made and finished by the shared admin login,
as ep98cle was. Every text is invented.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from hfvg import paths

ADMIN = "visibility-admin-secret-0123456789abcdef-xyz"
USERS = {  # who: (name, email, password, role)
    "owner": ("Mo Maker", "mo@visibility.test", "maker-pass-123", "member"),
    "sibling": ("Ed Editor", "ed@visibility.test", "editor-pass-123", "member"),
    "universe_owner": ("Una Owner", "una@visibility.test", "owner-pass-123", "member"),
    "member": ("Pat Plain", "pat@visibility.test", "plain-pass-123", "member"),
    "outsider": ("Oz Outsider", "oz@visibility.test", "outsider-pass-123", "member"),
    "staff": ("Sol Staff", "sol@visibility.test", "staff-pass-123", "admin"),
}
EP, SIB, MINE, SHARED, DONE, MISSING = "ep61vis", "ep62vis", "ep63vis", "ep64vis", "ep65vis", "ep69vis"
MADE_BY = {EP: "owner", SIB: "sibling", MINE: "member", SHARED: None, DONE: "admin"}  # "admin": the shared login
WORDS = {EP: "zeppelin", SIB: "harmonica", MINE: "gondola", SHARED: "abacus", DONE: "lanternfish"}
EVERY_EPISODE = set(WORDS)
SEES = {  # who sees which episode
    "admin": EVERY_EPISODE, "staff": EVERY_EPISODE, "bearer": EVERY_EPISODE,
    "owner": {EP, SHARED},
    "sibling": {SIB, SHARED},
    "universe_owner": {EP, SIB, SHARED},
    "member": {MINE, SHARED},
    "outsider": {SHARED},
}
SEE_EP = ("admin", "staff", "bearer", "owner", "universe_owner")
NOT_EP = ("sibling", "member", "outsider")  # the editor of a sibling episode, a plain member, an outsider
REFUSED = "belongs to another member; only its owner or an admin can change it"

# Every member route that names an episode in its path (test_the_matrix_names_every_member_route).
READS = [  # GET, and the reports (read_only)
    ("GET", "/api/episodes/{episode_id}"),
    ("GET", "/api/episodes/{episode_id}/story-shape"),
    ("GET", "/api/episodes/{episode_id}/budget"),
    ("GET", "/api/episodes/{episode_id}/credits"),
    ("GET", "/api/episodes/{episode_id}/stalls"),
    ("GET", "/api/episodes/{episode_id}/shots"),
    ("GET", "/api/episodes/{episode_id}/shot-states"),
    ("GET", "/api/episodes/{episode_id}/still-versions"),
    ("GET", "/api/episodes/{episode_id}/clip-plans"),
    ("GET", "/api/episodes/{episode_id}/gates"),
    ("GET", "/api/episodes/{episode_id}/audit"),
    ("POST", "/api/episodes/{episode_id}/preflight"),
    ("GET", "/api/episodes/{episode_id}/files"),
    ("GET", "/api/episodes/{episode_id}/files/{file_type}"),
    ("GET", "/api/episodes/{episode_id}/shots/{shot_id}/still"),
    ("GET", "/api/episodes/{episode_id}/cast"),
    ("GET", "/api/episodes/{episode_id}/cast/{character_id}"),
    ("GET", "/api/episodes/{episode_id}/props"),
    ("GET", "/api/episodes/{episode_id}/shot-plan"),
    ("GET", "/api/episodes/{episode_id}/universe"),
]
PATH_WRITES = [
    ("POST", "/api/episodes/{episode_id}/upload-prompt-kit"),
    ("POST", "/api/episodes/{episode_id}/upload-shots"),
    ("POST", "/api/episodes/{episode_id}/upload-audio-plan"),
    ("POST", "/api/episodes/{episode_id}/approve"),
    ("PUT", "/api/episodes/{episode_id}/story-shape"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/approve"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/reject"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/accept-clip"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/select-still"),
    ("POST", "/api/episodes/{episode_id}/resync-plans"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/retry-clip"),
    ("POST", "/api/episodes/{episode_id}/reopen"),
    ("POST", "/api/episodes/{episode_id}/reopen-from-saved"),
    ("POST", "/api/episodes/{episode_id}/set-live"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/retry"),
    ("POST", "/api/episodes/{episode_id}/shots/{shot_id}/retry/estimate"),
    ("POST", "/api/episodes/{episode_id}/set-dry"),
    ("POST", "/api/episodes/{episode_id}/final-cut/reject"),
    ("POST", "/api/episodes/{episode_id}/start-again"),
    ("POST", "/api/episodes/{episode_id}/series"),
    ("POST", "/api/episodes/{episode_id}/cast"),
    ("POST", "/api/episodes/{episode_id}/cast/with-picture"),
    ("POST", "/api/episodes/{episode_id}/cast/import"),
    ("POST", "/api/episodes/{episode_id}/cast/shots"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/delete"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images/{image_id}/primary"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/images/{image_id}/delete"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/estimate"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/generate"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/pick"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/sheet/reject"),
    ("POST", "/api/episodes/{episode_id}/cast/{character_id}/versions/{version}/approve"),
    ("POST", "/api/episodes/{episode_id}/props"),
    ("POST", "/api/episodes/{episode_id}/props/with-photo"),
    ("POST", "/api/episodes/{episode_id}/props/{prop_id}"),
    ("POST", "/api/episodes/{episode_id}/props/{prop_id}/delete"),
    ("POST", "/api/episodes/{episode_id}/props/{prop_id}/photos"),
    ("POST", "/api/episodes/{episode_id}/props/{prop_id}/photos/{image_id}/primary"),
    ("POST", "/api/episodes/{episode_id}/props/{prop_id}/photos/{image_id}/delete"),
    ("POST", "/api/episodes/{episode_id}/shot-plan"),
]
BODY_WRITES = [("POST", "/api/episodes/upload"), ("POST", "/api/episodes")]  # the episode id in the form / body
BEATMAP = b"# BEATMAP\n\n## S01\nA quiet harbour at dawn.\n"
UPLOAD_TOKEN = "upload-token-for-the-visibility-tests-0123456789"


def _prompt(ep: str) -> str:
    return f"A brass {WORDS[ep]} drifts past the harbour wall at dawn"


def _shot(ep: str) -> dict:
    in_universe = ep in (EP, SIB)
    return {"id": "S01", "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16", "ref_keys": [],
            "prompt": _prompt(ep), **({"cast": ["wren"], "place": "pier"} if in_universe else {})}


def _write_shots(ep: str) -> None:
    folder = paths.episode_dir(ep)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": ep, "shots": [_shot(ep)]}), encoding="utf-8")


def _finish(ep: str) -> None:
    from hfvg.audio_mix import delivery_paths
    phone = Path(delivery_paths(ep)["phone"])
    phone.parent.mkdir(parents=True, exist_ok=True)
    phone.write_bytes(b"\x00\x00\x00\x18ftypmp42 " + f"the finished {WORDS[ep]}".encode())


def _record() -> None:
    """Each episode's recorded shot (its prompt) and an audit entry quoting it."""
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        for ep in WORDS:
            conn.execute("INSERT INTO shots (id, episode_id, shot_id, status, still_url, retries, prompt) "
                         "VALUES (?, ?, 'S01', 'still_complete', ?, 0, ?)", (f"{ep}-S01", ep, f"/stills/{ep}/S01.png",
                                                                          _prompt(ep)))
            conn.execute("INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, 'retry_shot', ?, ?)",
                         (ep, f"S01 redo: prompt_append='the {WORDS[ep]} glints'", "someone"))


def _run(ep: str) -> dict:
    """The episode's run state: its story shape (logline and beats) names the episode's word."""
    word = WORDS[ep]
    return {"episode_id": ep, "stage": "stills", "current_gate": "G2_SCENE_STILLS",
            "approvals": {"story_shape": True, "g1_shot_plan": True}, "shots": 1,
            "story_shape": {"logline": f"A {word} learns to fly", "beats": [f"The {word} wakes"], "tone": "warm"},
            "story_shape_critique": {"notes": f"the {word} needs a want"}, "story_shape_revision": 1,
            "story_shape_log": [f"revised the {word}"], "shot_workflow_ids": {"S01": f"{ep}-shot-S01"}}


class _Workflows:
    """temporal_client.list_workflows(...): an async iterator over workflow infos."""

    def __init__(self, ids):
        self.ids = list(ids)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.ids:
            raise StopAsyncIteration
        return MagicMock(id=self.ids.pop(0))


def _temporal():
    """Each episode's run (state, clip plans, still versions) and its shot's child workflow, none of
    them open. ``changes`` records every update, signal and start."""
    client = MagicMock()
    client.changes = []

    def change(what: str, workflow_id: str | None):
        def record(*_a, **_k):
            client.changes.append((what, workflow_id or _k.get("id")))
            return MagicMock(id=workflow_id or _k.get("id"), result_run_id="run-new")
        return AsyncMock(side_effect=record)

    def handle_for(workflow_id, *_a, **_k):
        ep, _, shot = workflow_id.partition("-shot-")
        if ep not in WORDS:
            answers = {}
        elif shot:
            answers = {"get_state": {"still_status": "approved", "clip_status": None}}
        else:
            answers = {"get_state": _run(ep), "clip_plans": {"S01": {"version": 1, "clip_prompt": _prompt(ep)}},
                       "still_versions": {"S01": {"current_version": 1}},
                       "still_version_plan": {"shot_meta": {"count": 1}}}

        def query(name, *args, **kwargs):
            if name not in answers:
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            return copy.deepcopy(answers[name])

        handle = MagicMock(id=workflow_id)
        handle.query = AsyncMock(side_effect=query)
        handle.describe = AsyncMock(side_effect=RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b""))
        for what in ("execute_update", "signal", "cancel", "terminate"):
            setattr(handle, what, change(what, workflow_id))
        return handle

    def listed(query="", *_a, **_k):
        named = query.split('"')[1] if '"' in query else ""
        return _Workflows([named] if named in WORDS else [])

    client.get_workflow_handle = MagicMock(side_effect=handle_for)
    client.list_workflows = MagicMock(side_effect=listed)
    client.start_workflow = change("start_workflow", None)
    return client


@pytest.fixture
async def studio(tmp_path, monkeypatch):
    """Una's Universe (a character, Wren, and a place, the Pier) with EP and SIB in it; the rest as
    the module docstring says."""
    import bcrypt

    from hfvg import cast_store, prop_store, world_store
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
    for var in ("CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX", "ELEVENLABS_DEFAULT_VOICE_ID", "UPLOAD_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    ids = {who: (await users.create_user(name=name, email=email, role=role, password=password))["user_id"]
           for who, (name, email, password, role) in USERS.items()}
    for ep, maker in MADE_BY.items():
        await create_episode(db, ep, user_id=ids.get(maker, maker))
    una = ids["universe_owner"]
    sid = world_store.create_universe("Brass Harbour", una, actor=una)["series"]["series_id"]
    wren = cast_store.create_character(("series", sid), "Wren", "lead", "a lantern keeper", actor=una)
    pier = prop_store.create_item(("series", sid), "place", "Pier", "wet planks", actor=una)
    for ep in (EP, SIB):
        cast_store.link_episode(ep, sid, "admin")
    for ep in WORDS:
        _write_shots(ep)
    _record()
    for ep in (SIB, DONE):
        _finish(ep)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
    api_main.series_id, api_main.user_ids = sid, ids
    api_main.wren_id, api_main.pier_id = wren["character_id"], pier["prop_id"]
    return api_main


@pytest.fixture
def hidden(monkeypatch):
    """The episode ids the rule answered with the 404 of an episode that does not exist."""
    from api import cast_routes
    log: list[str] = []
    real = cast_routes.episode_hidden

    def noted(episode_id):
        log.append(episode_id)
        return real(episode_id)

    monkeypatch.setattr(cast_routes, "episode_hidden", noted)
    return log


def _client(api_main, who: str):
    from fastapi.testclient import TestClient
    client = TestClient(api_main.app, raise_server_exceptions=False)
    if who == "none":
        return client
    if who in ("bearer", "upload_token"):
        client.headers["Authorization"] = f"Bearer {ADMIN if who == 'bearer' else UPLOAD_TOKEN}"
        return client
    body = {"admin_secret": ADMIN} if who == "admin" else {"email": USERS[who][1], "password": USERS[who][2]}
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _url(api_main, path: str, ep: str) -> str:
    return path.format(episode_id=ep, file_type="phone", shot_id="S01", character_id=api_main.wren_id, version=1,
                       image_id="ci_000000000000", prop_id=api_main.pier_id)


def _read(client, api_main, method: str, path: str, ep: str):
    return client.request(method, _url(api_main, path, ep))


def _write(client, api_main, method: str, path: str, ep: str):
    """A request to a write route; past the rule it answers whatever its body earns."""
    if path == "/api/episodes/upload":
        return client.post(path, data={"episode_id": ep},
                           files={"beatmap_file": ("BEATMAP.md", io.BytesIO(BEATMAP), "text/markdown")})
    if path == "/api/episodes":
        return client.post(path, json={"episode_id": ep, "dry_run": True, "beatmap_content": BEATMAP.decode()})
    return client.request(method, _url(api_main, path, ep), json={})


def _gone(ep: str) -> tuple:
    return 404, {"detail": f"No workflow found for episode {ep}"}


def _state() -> tuple:
    """Every file of every episode and every row an episode write could change."""
    root = paths.episodes_dir()
    files = {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        rows = {t: sorted(map(repr, conn.execute(f"SELECT * FROM {t}").fetchall())) for t in tables
                if t not in ("users", "user_activity", "password_reset_tokens", "sqlite_sequence")}
    return files, rows


def test_the_matrix_names_every_member_route_about_an_episode(monkeypatch):
    """A new member route with the episode in its path must be added to READS or PATH_WRITES."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    sys.modules.pop("api.main", None)
    from fastapi.routing import APIRoute

    import api.main as api_main

    routes = [r for r in api_main.app.routes if isinstance(r, APIRoute) and "{episode_id}" in r.path
              and getattr(r.endpoint, "allow_members", False)]
    reads = {(m, r.path) for r in routes for m in r.methods if m == "GET" or getattr(r.endpoint, "read_only", False)}
    writes = {(m, r.path) for r in routes for m in r.methods} - reads
    assert reads == set(READS)
    assert writes == set(PATH_WRITES)


# ------------------------------------------------------------------------------------- an episode's routes


@pytest.mark.parametrize("method,path", READS)
async def test_a_read_answers_who_may_not_see_the_episode_as_for_one_that_does_not_exist(studio, hidden, method, path):
    for who in NOT_EP:
        client = _client(studio, who)
        res = _read(client, studio, method, path, EP)
        assert (res.status_code, res.json()) == _gone(EP), (who, res.text)
        gone = _read(client, studio, method, path, MISSING)
        assert (gone.status_code, gone.json()) == _gone(MISSING), (who, gone.text)
        assert {k: v for k, v in res.headers.items() if k != "content-length"} == {
            k: v for k, v in gone.headers.items() if k != "content-length"}, who
    assert hidden == [EP, MISSING] * len(NOT_EP)
    assert _read(_client(studio, "none"), studio, method, path, EP).status_code == 401


@pytest.mark.parametrize("method,path", READS)
async def test_a_read_answers_whoever_may_see_the_episode(studio, hidden, method, path):
    for who in SEE_EP:
        res = _read(_client(studio, who), studio, method, path, EP)
        assert res.status_code not in (401, 403, 422), (who, res.status_code, res.text)
    # an episode without an owner: every member edits it, so every member sees it
    for who in ("owner", "sibling", "member", "outsider"):
        res = _read(_client(studio, who), studio, method, path, SHARED)
        assert res.status_code not in (401, 403, 422), (who, res.status_code, res.text)
    assert hidden == []


@pytest.mark.parametrize("method,path", PATH_WRITES + BODY_WRITES)
async def test_a_write_answers_who_may_not_see_the_episode_as_for_one_that_does_not_exist(studio, hidden, method,
                                                                                           path):
    """Nothing changes; the owner of its Universe sees it but may not change it either."""
    clients = {who: _client(studio, who) for who in (*NOT_EP, "universe_owner")}
    before = _state()
    for who in NOT_EP:
        res = _write(clients[who], studio, method, path, EP)
        assert (res.status_code, res.json()) == _gone(EP), (who, res.text)
    assert hidden == [EP] * len(NOT_EP)
    res = _write(clients["universe_owner"], studio, method, path, EP)
    assert (res.status_code, res.json()) == (403, {"detail": f"Episode {EP} {REFUSED}"}), res.text
    assert _state() == before
    assert studio.temporal_client.changes == []


@pytest.mark.parametrize("method,path", PATH_WRITES + BODY_WRITES)
async def test_a_write_by_the_owner_or_an_admin_gets_past_the_rule(studio, hidden, method, path):
    """Past it, whatever comes next answers (Mo may not change Una's Universe library, for one)."""
    for who in ("owner", "admin", "staff", "bearer"):
        res = _write(_client(studio, who), studio, method, path, EP)
        assert res.status_code != 401, (who, res.text)
        assert not (res.status_code == 403 and res.json() == {"detail": f"Episode {EP} {REFUSED}"}), (who, res.text)
    assert hidden == []


async def test_the_story_and_the_prompts_reach_only_who_may_see_the_episode(studio):
    """The logline, beats, critique, revision and log of the story shape, and the shots' prompts that
    feed generation."""
    word = WORDS[EP]
    for who in SEE_EP:
        client = _client(studio, who)
        story = client.get(f"/api/episodes/{EP}/story-shape")
        assert story.status_code == 200, (who, story.text)
        assert story.json()["story_shape"]["logline"] == f"A {word} learns to fly", who
        assert story.json()["critique"] == {"notes": f"the {word} needs a want"}, who
        shots = client.get(f"/api/episodes/{EP}/shots")
        assert shots.json()["shots"][0]["prompt"] == _prompt(EP), who
    for who in NOT_EP:
        client = _client(studio, who)
        for url in (f"/api/episodes/{EP}/story-shape", f"/api/episodes/{EP}/shots", f"/api/episodes/{EP}"):
            res = client.get(url)
            assert res.status_code == 404 and word not in res.text, (who, url, res.text)


async def test_the_upload_token_sees_only_episodes_without_an_owner(studio, hidden, monkeypatch):
    """The scoped upload token is member-level and names no user (docs/AUTHENTICATION.md)."""
    draft = _client(studio, "owner").post("/api/episodes/draft-from-idea", json={
        "idea": "A paper kite drifts over a quiet harbor at dusk", "archetype_id": None, "target_duration": 12})
    beatmap = draft.json()["beatmap"]
    monkeypatch.setenv("UPLOAD_TOKEN", UPLOAD_TOKEN)
    client = _client(studio, "upload_token")
    before = _state()
    res = client.post("/api/episodes/upload", data={"episode_id": EP},
                      files={"beatmap_file": ("BEATMAP.md", io.BytesIO(beatmap.encode()), "text/markdown")})
    assert (res.status_code, res.json()) == _gone(EP), res.text
    assert _state() == before and hidden == [EP]
    res = client.post("/api/episodes/upload", data={"episode_id": SHARED},
                      files={"beatmap_file": ("BEATMAP.md", io.BytesIO(beatmap.encode()), "text/markdown")})
    assert res.status_code == 200, res.text
    assert (paths.episode_dir(SHARED) / "BEATMAP.md").read_text() == beatmap


async def test_a_finished_episode_of_the_shared_admin_login_is_seen_only_by_admins(studio):
    """As ep98cle: made and finished by the shared admin login, in no Universe. Admins (the login
    that made it among them) see it, its page and its finished video; no member does."""
    for who in ("admin", "staff", "bearer"):
        client = _client(studio, who)
        assert DONE in [e["episode_id"] for e in client.get("/api/episodes").json()["episodes"]], who
        assert client.get(f"/api/episodes/{DONE}").status_code == 200, who
        video = client.get(f"/api/episodes/{DONE}/files/phone")
        assert video.status_code == 200 and WORDS[DONE].encode() in video.content, who
    for who in ("owner", "sibling", "universe_owner", "member", "outsider"):
        client = _client(studio, who)
        listed = client.get("/api/episodes")
        assert DONE not in listed.text, who
        for url in (f"/api/episodes/{DONE}", f"/api/episodes/{DONE}/files", f"/api/episodes/{DONE}/files/phone",
                    f"/api/episodes/{DONE}/story-shape"):
            res = client.get(url)
            assert (res.status_code, res.json()) == _gone(DONE), (who, url, res.text)


# ------------------------------------------------------------------------------------- lists and counts


async def test_the_episode_list_has_only_the_episodes_the_caller_may_see(studio):
    for who, seen in SEES.items():
        listed = _client(studio, who).get("/api/episodes")
        assert listed.status_code == 200, (who, listed.text)
        assert {e["episode_id"] for e in listed.json()["episodes"]} == seen, who
        assert not any(ep in listed.text for ep in EVERY_EPISODE - seen), who


UNIVERSE_SEES = {"admin": [EP, SIB], "staff": [EP, SIB], "bearer": [EP, SIB], "universe_owner": [EP, SIB],
                 "owner": [EP], "sibling": [SIB]}


async def test_the_universe_lists_and_counts_only_the_episodes_the_caller_may_see(studio):
    """Its page (episodes, counts, finished count, the "used in" counts of its characters and places),
    its card, the series routes and the episode cast view; the editor of one episode finds no trace of
    the other. Whoever has no episode in it still gets the 404 of a Universe that does not exist."""
    sid = studio.series_id
    for who, eps in UNIVERSE_SEES.items():
        client = _client(studio, who)
        hidden_ids = [e for e in (EP, SIB) if e not in eps]
        page = client.get(f"/api/worlds/{sid}")
        assert page.status_code == 200, (who, page.text)
        view = page.json()
        assert sorted(e["episode_id"] for e in view["episodes"]) == eps, who
        assert sorted(view["world"]["episodes"]) == eps, who
        assert view["world"]["counts"]["episodes"] == len(eps), who
        assert view["world"]["counts"]["finished_episodes"] == (1 if SIB in eps else 0), who
        [wren] = view["characters"]
        [pier] = view["items"]
        assert (wren["used_in"]["episodes"], pier["used_in"]["episodes"]) == (len(eps), len(eps)), who
        card = next(w for w in client.get("/api/worlds").json()["worlds"] if w["series_id"] == sid)
        assert (sorted(card["episodes"]), card["counts"]["episodes"]) == (eps, len(eps)), who
        series = client.get(f"/api/series/{sid}")
        assert sorted(series.json()["series"]["episodes"]) == eps, who
        row = next(s for s in client.get("/api/series").json()["series"] if s["series_id"] == sid)
        assert (sorted(row["episodes"]), row["episode_count"]) == (eps, len(eps)), who
        cast = client.get(f"/api/episodes/{eps[0]}/cast")
        assert sorted(cast.json()["series"]["episodes"]) == eps, who
        for res in (page, series, cast, client.get("/api/worlds"), client.get("/api/series"),
                    client.get(f"/api/episodes/{eps[0]}/universe"),
                    client.get(f"/api/episodes/{eps[0]}/cast/{studio.wren_id}")):
            assert not any(e in res.text or WORDS[e] in res.text for e in hidden_ids), (who, res.url, res.text)
    for who in ("member", "outsider"):
        client = _client(studio, who)
        res = client.get(f"/api/worlds/{sid}")
        assert (res.status_code, res.json()) == (404, {"detail": f"Universe {sid} not found"}), who
        assert sid not in client.get("/api/worlds").text and sid not in client.get("/api/series").text, who


def _asked_from(studio, *episodes: str) -> None:
    """An audit entry and a (finished) sheet job on Wren's first version asked for from each episode."""
    from hfvg import cast_store
    sid, wren, una = studio.series_id, studio.wren_id, studio.user_ids["universe_owner"]
    for n, ep in enumerate(episodes, start=1):
        cast_store.log_event(una, "sheet_request", series_id=sid, episode_id=ep, character_id=wren,
                             details={"note": f"asked from {ep}"})
        job_id = f"castsheet-{wren}-v1-front-a{n}"
        cast_store.record_sheet_job({"job_id": job_id, "character_id": wren, "version": 1, "stage": "sheet",
                                     "view": "front", "attempt": n, "episode_id": ep, "requested_by": una})
        cast_store.update_sheet_job(job_id, status="completed")


def _jobs(character: dict) -> list:
    """The episode each of a character's sheet jobs names."""
    return [job["episode_id"] for version in character["versions"] for job in version["sheet_jobs"]]


async def test_a_universe_character_leaves_out_the_episodes_the_caller_may_not_see(studio):
    """Its audit trail and its sheet jobs name the episode each was asked for from."""
    sid, wren = studio.series_id, studio.wren_id
    _asked_from(studio, EP, SIB)
    for who, eps in UNIVERSE_SEES.items():
        client = _client(studio, who)
        res = client.get(f"/api/episodes/{eps[0]}/cast/{wren}")
        assert res.status_code == 200, (who, res.text)
        body = res.json()
        assert sorted({row["episode_id"] for row in body["audit"]} - {None}) == eps, who
        assert any(row["episode_id"] is None for row in body["audit"]), who  # the character's own rows stay
        named = [ep if ep in eps else None for ep in (EP, SIB)]
        assert _jobs(body["character"]) == named, who
        [character] = client.get(f"/api/worlds/{sid}").json()["characters"]
        assert _jobs(character) == named, who
        [character] = client.get(f"/api/series/{sid}").json()["characters"]
        assert _jobs(character) == named, who
        [character] = client.get(f"/api/episodes/{eps[0]}/cast").json()["characters"]
        assert _jobs(character) == named, who


async def test_the_props_view_names_only_the_episodes_of_its_universe_the_caller_may_see(studio):
    """GET /api/episodes/{id}/props names the Universe the episode is in with its episodes: the editor
    of a sibling episode is told of theirs alone, the owner of the Universe and admins of both."""
    cases = {  # who: (the episode whose props and places they open, the Universe's episodes they are told of)
        "sibling": (SIB, [SIB]),
        "owner": (EP, [EP]),
        "universe_owner": (EP, [EP, SIB]),
        "admin": (EP, [EP, SIB]),
        "staff": (SIB, [EP, SIB]),
        "bearer": (EP, [EP, SIB]),
    }
    for who, (ep, eps) in cases.items():
        res = _client(studio, who).get(f"/api/episodes/{ep}/props")
        assert res.status_code == 200, (who, res.text)
        body = res.json()
        assert (body["series"]["series_id"], sorted(body["series"]["episodes"])) == (studio.series_id, eps), who
        assert [item["prop_id"] for item in body["items"]] == [studio.pier_id], who
        hidden_ids = {EP, SIB} - set(eps)
        assert not any(e in res.text or WORDS[e] in res.text for e in hidden_ids), (who, res.text)
    member = _client(studio, "member")
    res = member.get(f"/api/episodes/{EP}/props")
    assert (res.status_code, res.json()) == _gone(EP), res.text
    mine = member.get(f"/api/episodes/{MINE}/props").json()
    assert (mine["library"], mine["series"]) == ({"kind": "episode", "id": MINE}, None)


READ_LISTS = ["/api/episodes", "/api/worlds", "/api/series", "/api/worlds/{series_id}", "/api/series/{series_id}",
              "/api/me"]


async def test_no_read_names_an_episode_the_caller_may_not_see(studio):
    """Every member read about every episode the caller may see, and the lists and Universe views:
    none carries the id or the words of an episode they may not see, with sheet jobs and audit entries
    asked for from both episodes of the Universe."""
    _asked_from(studio, EP, SIB)
    for who in ("owner", "sibling", "universe_owner", "member", "outsider"):
        client = _client(studio, who)
        hidden_ids = EVERY_EPISODE - SEES[who]
        urls = [(method, _url(studio, path, ep)) for ep in sorted(SEES[who]) for method, path in READS]
        urls += [("GET", path.format(series_id=studio.series_id)) for path in READ_LISTS]
        for method, url in urls:
            res = client.request(method, url)
            leaked = [word for ep in sorted(hidden_ids) for word in (ep, WORDS[ep]) if word in res.text]
            assert not leaked, (who, method, url, leaked, res.text[:300])


UNA_EP = "ep66vis"


async def test_a_change_to_a_universe_character_names_no_episode_the_caller_may_not_see(studio):
    """Mo takes EP out of Una's Universe: Wren keeps the sheet job asked for from EP, and EP is no
    longer Una's to see. What her changes to Wren answer, from the Universe page or from an episode of
    her own in it, names SIB and her episode but not EP; an admin's names all three."""
    from PIL import Image

    from hfvg import cast_store
    from hfvg.studio_db import create_episode
    sid, wren = studio.series_id, studio.wren_id
    await create_episode(os.environ["DATABASE_PATH"], UNA_EP, user_id=studio.user_ids["universe_owner"])
    cast_store.link_episode(UNA_EP, sid, "admin")
    _asked_from(studio, EP, SIB, UNA_EP)
    res = _client(studio, "owner").post(f"/api/episodes/{EP}/series", json={"series_id": None})
    assert (res.status_code, res.json()["series_id"]) == (200, None), res.text
    for n, role in enumerate((cast_store.MASTER_ROLE, *cast_store.TURNAROUND_ROLES, cast_store.HEAD_ROLE)):
        png = io.BytesIO()
        Image.new("RGB", (512, 768), (30 * n, 90, 120)).save(png, "PNG")
        cast_store.add_generated_image(wren, 1, png.getvalue(), role=role, job_id=f"sheet-{role}", placeholder=True)
    episode_cast = f"/api/episodes/{UNA_EP}/cast/{wren}"
    for who, named in (("admin", [EP, SIB, UNA_EP]), ("universe_owner", [None, SIB, UNA_EP])):
        client = _client(studio, who)
        res = client.post(f"/api/worlds/{sid}/cast/{wren}", json={"description": "a lantern keeper by the pier"})
        assert (res.status_code, _jobs(res.json()["character"])) == (200, named), (who, res.text)
        res = client.post(episode_cast, json={"role": "the lead"})
        assert (res.status_code, _jobs(res.json()["character"])) == (200, named), (who, res.text)
        res = client.post(f"{episode_cast}/versions/1", json={"label": f"harbour coat for {who}"})
        assert (res.status_code, _jobs({"versions": [res.json()["version"]]})) == (200, named), (who, res.text)
        res = client.post(f"{episode_cast}/versions", json={"label": f"storm coat for {who}"})
        assert (res.status_code, _jobs(res.json()["character"])) == (200, named), (who, res.text)
        assert res.json()["version"]["sheet_jobs"] == [], (who, res.text)
    res = _client(studio, "universe_owner").post(f"{episode_cast}/versions/1/approve", json={"allow_placeholder": True})
    assert res.status_code == 200, res.text
    assert res.json()["version"]["locked"] is True
    assert _jobs({"versions": [res.json()["version"]]}) == _jobs(res.json()["character"]) == [None, SIB, UNA_EP]
    assert EP not in res.text


async def test_a_redo_on_an_episode_without_a_run_answers_as_for_one_that_does_not_exist(studio):
    """Temporal has no run for a made-up id ("workflow not found"): the redo answers the 404 of an
    episode that does not exist, which is also what a hidden episode answers, not a 500."""
    from temporalio.service import RPCError, RPCStatusCode
    real = studio.temporal_client.get_workflow_handle.side_effect

    def handle_for(workflow_id, *args, **kwargs):
        handle = real(workflow_id, *args, **kwargs)
        if workflow_id == MISSING:
            handle.execute_update = AsyncMock(side_effect=RPCError(
                f"workflow not found for ID: {MISSING}", RPCStatusCode.NOT_FOUND, b""))
        return handle

    studio.temporal_client.get_workflow_handle.side_effect = handle_for
    for who in ("member", "owner", "admin"):
        client = _client(studio, who)
        for body in ({}, {"prompt_append": "warmer light"}):
            res = client.post(f"/api/episodes/{MISSING}/shots/S01/retry", json=body)
            assert (res.status_code, res.json()) == _gone(MISSING), (who, body, res.text)
    res = _client(studio, "member").post(f"/api/episodes/{EP}/shots/S01/retry", json={})
    assert (res.status_code, res.json()) == _gone(EP), res.text


async def test_unlinking_does_not_name_the_run_of_an_episode_the_caller_may_not_see(studio):
    """Ed's episode is being made: Mo cannot take his own episode out of the Universe meanwhile, and
    is not told which episode or run; an admin is."""
    from temporalio.client import WorkflowExecutionStatus

    real = studio.temporal_client.get_workflow_handle.side_effect

    def handle_for(workflow_id, *args, **kwargs):
        handle = real(workflow_id, *args, **kwargs)
        if workflow_id == SIB:
            handle.describe = AsyncMock(return_value=MagicMock(run_id="run-sib", status=WorkflowExecutionStatus.RUNNING))
        return handle

    studio.temporal_client.get_workflow_handle.side_effect = handle_for
    res = _client(studio, "owner").post(f"/api/episodes/{EP}/series", json={"series_id": None})
    assert (res.status_code, res.json()) == (409, {"detail": (
        "Another video of this Universe is being made right now; its series can change after that run ends")})
    assert SIB not in res.text and "run-sib" not in res.text
    res = _client(studio, "admin").post(f"/api/episodes/{EP}/series", json={"series_id": None})
    assert res.status_code == 409 and res.json()["detail"].startswith(
        f"Episode {SIB} is being made right now (run run-sib); ")
    from hfvg import cast_store
    assert cast_store.library_for_episode(EP) == ("series", studio.series_id)


# ------------------------------------------------------------------------------------- one rule


async def test_whoever_sees_an_episode_is_whoever_may_read_its_prompts(studio):
    """visible_episodes is the rule of PR #46's _may_see_prompts, for every caller and episode."""
    from api import cast_routes
    callers = {who: (studio.user_ids.get(who), False) for who in USERS if who != "staff"}
    callers.update({"staff": (studio.user_ids["staff"], True), "admin": ("admin", True), "bearer": (None, True),
                    "upload_token": (None, False)})
    for who, (user_id, is_admin) in callers.items():
        seen = await cast_routes.visible_episodes([*EVERY_EPISODE, MISSING], user_id, is_admin)
        for ep in EVERY_EPISODE:
            assert (ep in seen) == await cast_routes._may_see_prompts(ep, user_id, is_admin), (who, ep)
        assert seen - {MISSING} == SEES.get(who, {SHARED}), who
