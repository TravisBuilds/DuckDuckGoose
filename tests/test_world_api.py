"""Universe API (api/world_routes.py, docs/WORLDS.md): who sees a Universe (everyone else gets the
404 a missing one gets) and who changes it, the list and page views, the profile, library writes
that mirror the episode routes, saved voices, the creation flow (references, a mood board behind its
estimate, approval, the bible) without a provider call or a ledger row, an episode's Universe view
and the locked looks a Universe episode's shot plan waits for. Every image is generated (flat
shapes); every name is original."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from hfvg import cast_store, paths, prop_store, world_store
from tests.cast_helpers import complete_sheet, image_bytes

ADMIN = "world-api-admin-secret-0123456789abcdef-xyz"
OWNER = ("wren.owner@world.test", "owner-pass-123")
OTHER = ("moss.other@world.test", "other-pass-123")
EP = "ep91wld"
OTHER_EP = "ep92wld"
REFUSED = "belongs to another member; only its owner or an admin can change it"
MISSING = "sr_ffffffffffff"
MONEY_TABLES = ("budget_lines", "budget_transactions", "job_ledger", "round_holds", "credit_lines",
                "credit_transactions", "provider_jobs", "jobs", "credits", "episode_runs", "episode_spend_owner",
                "cast_sheet_jobs")
SECRET_WORDS = "The lighthouse keeper hides a map in the brass bell"
STYLE_WORDS = "chalk pastel, foggy teal shadows"
NOTE_WORDS = "Salt-crusted lanterns that hum at low tide"
PRICE = 390_000
HF_BASE = "https://api.higgsfield.ai"


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
    handle.query = AsyncMock(return_value={})
    handle.execute_update = AsyncMock(return_value={"status": "approved"})
    handle.signal = AsyncMock()
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.list_workflows = MagicMock(side_effect=lambda *_a, **_k: _Workflows([EP]))
    client.start_workflow = AsyncMock(return_value=MagicMock(id=EP, result_run_id="run-1"))
    client.handle = handle
    return client


@pytest.fixture
async def studio(tmp_path, monkeypatch):
    """Two members and an admin; EP is owned by OWNER, OTHER_EP by OTHER."""
    import bcrypt
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import create_episode, init_studio_db
    from hfvg.users import UserManager

    gensalt = bcrypt.gensalt
    monkeypatch.setattr(bcrypt, "gensalt", lambda *_a, **_k: gensalt(rounds=4))
    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("ELEVENLABS_DEFAULT_VOICE_ID", "CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX"):
        monkeypatch.delenv(var, raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    owner = await users.create_user(name="Tamsin Owner", email=OWNER[0], role="member", password=OWNER[1])
    other = await users.create_user(name="Ivo Other", email=OTHER[0], role="member", password=OTHER[1])
    await create_episode(db, EP, user_id=owner["user_id"])
    await create_episode(db, OTHER_EP, user_id=other["user_id"])
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
    api_main.owner_id, api_main.other_id = owner["user_id"], other["user_id"]
    return api_main


def _client(api_main, who: str | None):
    from fastapi.testclient import TestClient
    client = TestClient(api_main.app, raise_server_exceptions=False)
    if who is None:
        return client
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


def _world(client, name: str = "Lantern Bay", **body) -> str:
    res = client.post("/api/worlds", json={"name": name, **body})
    assert res.status_code == 200, res.text
    return res.json()["world"]["series_id"]


def _base(series_id: str) -> str:
    return f"/api/worlds/{series_id}"


def _files(data: bytes | None = None, filename: str = "ref.png", mime: str = "image/png") -> dict:
    return {"image_file": (filename, io.BytesIO(image_bytes() if data is None else data), mime)}


def _character(client, series_id: str, name: str = "Wren", **body) -> dict:
    res = client.post(f"{_base(series_id)}/cast", json={
        "name": name, "role": "lead", "description": f"{name}, a lantern keeper with a teal raincoat", **body})
    assert res.status_code == 200, res.text
    return res.json()["character"]


def _item(client, series_id: str, name: str = "Pier", kind: str = "place") -> dict:
    res = client.post(f"{_base(series_id)}/props", json={"kind": kind, "name": name,
                                                         "description": f"the {name.lower()} at the end of the bay"})
    assert res.status_code == 200, res.text
    return res.json()["item"]


def _link(api_main, episode_id: str, series_id: str) -> None:
    res = _client(api_main, "admin").post(f"/api/episodes/{episode_id}/series", json={"series_id": series_id})
    assert res.status_code == 200, res.text


def _lock(character_id: str, version: int = 1) -> None:
    complete_sheet({"character_id": character_id}, version)
    cast_store.approve_version(character_id, version, "tester")


def _shot(sid: str, **extra) -> dict:
    return {"id": sid, "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": f"A quiet pier at dawn, shot {sid}.", "ref_keys": [], **extra}


def _write_shots(episode_id: str, shots: list[dict]) -> None:
    folder = paths.episode_dir(episode_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": episode_id, "shots": shots}), encoding="utf-8")


def _finish(episode_id: str) -> None:
    from pathlib import Path
    from hfvg.audio_mix import delivery_paths
    phone = Path(delivery_paths(episode_id)["phone"])
    phone.parent.mkdir(parents=True, exist_ok=True)
    phone.write_bytes(b"\x00\x00\x00\x18ftypmp42 finished video")


def _statements(monkeypatch, call) -> list[str]:
    """The SQL statements run while ``call`` runs (every SQLite connection opened meanwhile)."""
    seen: list[str] = []
    real = sqlite3.connect

    def traced(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", traced)
    try:
        call()
    finally:
        monkeypatch.setattr(sqlite3, "connect", real)
    return seen


def _money_rows() -> dict:
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        rows = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MONEY_TABLES if t in names}
    with cast_store.connect() as conn:
        rows["cast_sheet_jobs"] = conn.execute("SELECT COUNT(*) FROM cast_sheet_jobs").fetchone()[0]
    return rows


def _nothing_started(api_main) -> None:
    api_main.temporal_client.start_workflow.assert_not_called()
    api_main.temporal_client.handle.execute_update.assert_not_called()
    api_main.temporal_client.handle.signal.assert_not_called()


def _running(api_main) -> None:
    api_main.temporal_client.handle.describe = AsyncMock(side_effect=None, return_value=MagicMock(
        status=WorkflowExecutionStatus.RUNNING, run_id="run-open"))


# ------------------------------------------------------------------------------------- who may


async def test_universe_routes_need_a_login(studio):
    sid = _world(_client(studio, "owner"))
    anon = _client(studio, None)
    for method, url in [("GET", "/api/worlds"), ("POST", "/api/worlds"), ("GET", _base(sid)),
                        ("POST", f"{_base(sid)}/profile"), ("POST", f"{_base(sid)}/cast"),
                        ("GET", "/api/worlds/images/ui_000000000000"), ("GET", f"/api/episodes/{EP}/universe")]:
        res = anon.request(method, url, json={"name": "Moss"} if method == "POST" else None)
        assert res.status_code == 401, (url, res.text)


async def test_a_new_universe_belongs_to_its_maker_and_starts_empty(studio):
    owner = _client(studio, "owner")
    before = _money_rows()
    with respx.mock(assert_all_called=False) as network:
        res = owner.post("/api/worlds", json={"name": "Lantern Bay", "logline": "A bay where lanterns keep secrets",
                                              "preset_id": "cozy-hangout"})
    assert res.status_code == 200, res.text
    world = res.json()["world"]
    assert world["owner_user_id"] == studio.owner_id
    assert (world["profile"]["logline"], world["profile"]["preset_id"]) == ("A bay where lanterns keep secrets",
                                                                            "cozy-hangout")
    assert network.calls.call_count == 0 and _money_rows() == before
    _nothing_started(studio)
    [card] = owner.get("/api/worlds").json()["worlds"]
    assert (card["series_id"], card["name"], card["can_edit"], card["owner_name"]) == (
        world["series_id"], "Lantern Bay", True, "Tamsin Owner")
    assert card["counts"] == {"characters": 0, "locked_characters": 0, "locked_versions": 0, "places": 0,
                              "props": 0, "episodes": 0, "finished_episodes": 0}
    assert card["cover"] is None and card["episodes"] == []
    # the series is the one the cast library knows
    assert cast_store.get_series(world["series_id"])["owner_user_id"] == studio.owner_id
    for bad, status in [({"name": ""}, 422), ({"name": "Bay", "preset_id": "no-such-card"}, 400),
                        ({"name": "Bay", "logline": "x" * (world_store.LOGLINE_MAX + 1)}, 422)]:
        assert owner.post("/api/worlds", json=bad).status_code == status, bad
    assert len(owner.get("/api/worlds").json()["worlds"]) == 1


async def test_another_member_gets_the_404_a_missing_universe_gets(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    sid = _world(owner)
    wren = _character(owner, sid)
    missing = other.get(_base(MISSING))
    hidden = other.get(_base(sid))
    assert (missing.status_code, hidden.status_code) == (404, 404)
    assert missing.json() == {"detail": f"Universe {MISSING} not found"}
    assert hidden.json() == {"detail": f"Universe {sid} not found"}
    assert other.get("/api/worlds").json()["worlds"] == []
    for url, body in [(f"{_base(sid)}/profile", {"logline": "mine now"}), (f"{_base(sid)}/cast", {"name": "Moss"}),
                      (f"{_base(sid)}/cast/{wren['character_id']}", {"name": "Moss"}),
                      (f"{_base(sid)}/cast/{wren['character_id']}/voice", {"voice_id": "fake-voice-1"}),
                      (f"{_base(sid)}/narrator-voice", {"voice_id": "fake-voice-1"}),
                      (f"{_base(sid)}/notes", {"topic": "premise", "body": "a takeover"})]:
        res = other.post(url, json=body)
        assert res.json() == {"detail": f"Universe {sid} not found"} and res.status_code == 404, url
        assert other.post(url.replace(sid, MISSING), json=body).json() == {"detail": f"Universe {MISSING} not found"}
    res = other.post(f"{_base(sid)}/cast/with-picture", data={"name": "Moss", "rights": "original"}, files=_files())
    assert res.status_code == 404 and res.json()["detail"] == f"Universe {sid} not found"
    assert [c["name"] for c in cast_store.list_characters(("series", sid))] == ["Wren"]
    assert world_store.get_profile(sid)["logline"] == ""
    assert "Wren" not in other.get(f"/api/episodes/{EP}/universe").text


async def test_episode_editors_see_the_universe_but_do_not_change_it(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    sid = _world(owner)
    owner.post(f"{_base(sid)}/profile", json={"bible": SECRET_WORDS, "style_notes": STYLE_WORDS})
    owner.post(f"{_base(sid)}/notes", json={"topic": "tone", "body": NOTE_WORDS})
    wren = _character(owner, sid)
    owner.post(f"{_base(sid)}/cast/{wren['character_id']}/voice", json={
        "voice_id": "fake-voice-1", "name": "Calm Keeper", "description": "a calm low voice"})
    _link(studio, OTHER_EP, sid)
    view = other.get(_base(sid))
    assert view.status_code == 200, view.text
    body = view.json()
    assert body["can_edit"] is False and body["world"]["can_edit"] is False
    assert [c["name"] for c in body["characters"]] == ["Wren"]
    assert "voice" not in body["characters"][0] and "creation" not in body
    assert set(body["world"]["profile"]) == {"logline", "preset_id", "cover_image_id", "updated_at"}
    for private in (SECRET_WORDS, STYLE_WORDS, NOTE_WORDS, "fake-voice-1", "Calm Keeper"):
        assert private not in view.text, private
    [card] = other.get("/api/worlds").json()["worlds"]
    assert card["can_edit"] is False and card["board_status"] is None and SECRET_WORDS not in json.dumps(card)
    for url, body in [(f"{_base(sid)}/profile", {"logline": "mine now"}), (f"{_base(sid)}/cast", {"name": "Moss"}),
                      (f"{_base(sid)}/cast/{wren['character_id']}/voice", {"voice_id": "fake-voice-2"}),
                      (f"{_base(sid)}/narrator-voice", {"voice_id": "fake-voice-2"}),
                      (f"{_base(sid)}/props", {"kind": "prop", "name": "Oar"})]:
        res = other.post(url, json=body)
        assert (res.status_code, res.json().get("detail")) == (403, f"Universe Lantern Bay {REFUSED}"), url
    assert world_store.character_voices([wren["character_id"]])[wren["character_id"]]["voice_id"] == "fake-voice-1"
    assert prop_store.list_items(("series", sid)) == []


@pytest.mark.parametrize("who", ["admin", "bearer"])
async def test_admins_see_and_change_every_universe(studio, who):
    sid = _world(_client(studio, "owner"))
    admin = _client(studio, who)
    assert admin.get(_base(sid)).json()["can_edit"] is True
    assert [w["series_id"] for w in admin.get("/api/worlds").json()["worlds"]] == [sid]
    assert admin.post(f"{_base(sid)}/profile", json={"logline": "Fixed by an admin"}).status_code == 200
    _character(admin, sid, "Moss")
    assert world_store.get_profile(sid)["logline"] == "Fixed by an admin"
    assert cast_store.get_series(sid)["owner_user_id"] == studio.owner_id


async def test_a_universe_without_an_owner_changes_only_through_an_admin(studio):
    bearer = _client(studio, "bearer")
    sid = _world(bearer, "Shared Shore")
    assert cast_store.get_series(sid)["owner_user_id"] is None
    other = _client(studio, "other")
    assert other.get(_base(sid)).status_code == 404
    _link(studio, OTHER_EP, sid)
    assert other.get(_base(sid)).json()["can_edit"] is False
    res = other.post(f"{_base(sid)}/profile", json={"logline": "claimed"})
    assert (res.status_code, res.json()["detail"]) == (403, "Universe Shared Shore has no owner; only an admin "
                                                            "can change it")
    assert bearer.post(f"{_base(sid)}/profile", json={"logline": "kept by admins"}).status_code == 200


async def test_an_episode_without_an_owner_shows_its_universe_to_every_member(studio):
    """The cast library's series rule: an episode without an owner may be changed by any member."""
    from hfvg.studio_db import create_episode
    await create_episode(os.environ["DATABASE_PATH"], "ep93old")
    sid = _world(_client(studio, "owner"))
    _link(studio, "ep93old", sid)
    other = _client(studio, "other")
    assert other.get(_base(sid)).status_code == 200
    assert [w["series_id"] for w in other.get("/api/worlds").json()["worlds"]] == [sid]


# ------------------------------------------------------------------------------------- profile


async def test_the_profile_saves_only_what_is_sent(studio):
    owner = _client(studio, "owner")
    sid = _world(owner, logline="First line")
    res = owner.post(f"{_base(sid)}/profile", json={"bible": "Para one.\n\nPara two.", "style_notes": STYLE_WORDS})
    assert res.status_code == 200, res.text
    profile = res.json()["profile"]
    assert (profile["logline"], profile["bible"], profile["style_notes"]) == ("First line", "Para one.\n\nPara two.",
                                                                              STYLE_WORDS)
    res = owner.post(f"{_base(sid)}/profile", json={"logline": None, "preset_id": "creature-legend"})
    assert (res.json()["profile"]["logline"], res.json()["profile"]["preset_id"]) == ("", "creature-legend")
    assert world_store.get_profile(sid)["bible"] == "Para one.\n\nPara two."
    for body, status in [({"bible": "x" * (world_store.BIBLE_MAX + 1)}, 422), ({"preset_id": "no-such-card"}, 400),
                         ({"cover_image_id": "ci_000000000000"}, 404)]:
        assert owner.post(f"{_base(sid)}/profile", json=body).status_code == status, body
    from hfvg.content_policy import hard_stop_terms
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))
    res = owner.post(f"{_base(sid)}/profile", json={"bible": f"covered in {bad}"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword")
    # the cover is one of the Universe's own pictures
    elsewhere = _world(owner, "Elsewhere")
    stranger = owner.post(f"{_base(elsewhere)}/cast/with-picture", data={"name": "Gull", "rights": "original"},
                          files=_files(image_bytes(seed=9))).json()["character"]
    stranger_image = stranger["versions"][0]["images"][0]["image_id"]
    res = owner.post(f"{_base(sid)}/profile", json={"cover_image_id": stranger_image})
    assert res.status_code == 404 and res.json()["detail"] == "That picture is not part of this Universe"
    mine = owner.post(f"{_base(sid)}/cast/with-picture", data={"name": "Wren", "rights": "original"},
                      files=_files()).json()["character"]["versions"][0]["images"][0]["image_id"]
    assert owner.post(f"{_base(sid)}/profile", json={"cover_image_id": mine}).status_code == 200
    card = next(w for w in owner.get("/api/worlds").json()["worlds"] if w["series_id"] == sid)
    assert card["cover"] == {"image_id": mine, "kind": "cast", "url": f"/api/cast/images/{mine}",
                             "thumb_url": f"/api/cast/images/{mine}/thumb"}


# ------------------------------------------------------------------------------------- library


async def test_universe_library_writes_mirror_the_episode_routes(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)
    base = _base(sid)
    wren = _character(owner, sid)
    assert (wren["library_kind"], wren["library_id"]) == ("series", sid)
    cid = wren["character_id"]
    res = owner.post(f"{base}/cast/{cid}", json={"description": "Wren, a keeper with a yellow scarf"})
    assert res.status_code == 200 and res.json()["character"]["description"] == "Wren, a keeper with a yellow scarf"
    up = owner.post(f"{base}/cast/{cid}/versions/1/images", data={"rights": "original"}, files=_files())
    assert up.status_code == 200, up.text
    first = up.json()["image"]
    again = owner.post(f"{base}/cast/{cid}/versions/1/images", data={"rights": "original"}, files=_files())
    assert again.json()["duplicate"] is True
    second = owner.post(f"{base}/cast/{cid}/versions/1/images", data={"rights": "original"},
                        files=_files(image_bytes(seed=3))).json()["image"]
    res = owner.post(f"{base}/cast/{cid}/versions/1/images/{second['image_id']}/primary")
    assert res.status_code == 200 and res.json()["image"]["is_primary"]
    assert owner.get(first["url"]).status_code == 200
    res = owner.post(f"{base}/cast/{cid}/versions/1/images/{first['image_id']}/delete")
    assert res.status_code == 200, res.text
    # pictures need the rights attestation; a real person needs consent; small or odd files are refused
    for form, data, status in [({}, image_bytes(seed=5), 400),
                               ({"rights": "original", "depicts_real_person": "true"}, image_bytes(seed=6), 400),
                               ({"rights": "original"}, image_bytes(size=(100, 100), seed=7), 400),
                               ({"rights": "original"}, b"not an image", 400)]:
        res = owner.post(f"{base}/cast/{cid}/versions/1/images", data=form, files=_files(data))
        assert res.status_code == status, (form, res.text)
    moss = owner.post(f"{base}/cast/with-picture", data={"name": "Moss", "role": "friend", "rights": "original"},
                      files=_files(image_bytes(seed=11)))
    assert moss.status_code == 200, moss.text
    pier = _item(owner, sid)
    assert (pier["library_kind"], pier["library_id"], pier["kind"]) == ("series", sid, "place")
    pid = pier["prop_id"]
    photo = owner.post(f"{base}/props/{pid}/photos", data={"rights": "original"},
                       files=_files(image_bytes(seed=12, color=(30, 90, 160))))
    assert photo.status_code == 200, photo.text
    photo2 = owner.post(f"{base}/props/{pid}/photos", data={"rights": "original"},
                        files=_files(image_bytes(seed=13, color=(30, 90, 160)))).json()["photo"]
    assert owner.post(f"{base}/props/{pid}/photos/{photo2['image_id']}/primary").status_code == 200
    assert owner.post(f"{base}/props/{pid}/photos/{photo.json()['photo']['image_id']}/delete").status_code == 200
    assert owner.post(f"{base}/props/{pid}", json={"name": "Long pier"}).json()["item"]["name"] == "Long pier"
    lantern = owner.post(f"{base}/props/with-photo", data={"kind": "prop", "name": "Lantern", "rights": "original"},
                         files=_files(image_bytes(seed=14, color=(220, 180, 40))))
    assert lantern.status_code == 200, lantern.text
    assert owner.post(f"{base}/props/{lantern.json()['item']['prop_id']}/delete").status_code == 200
    assert owner.post(f"{base}/cast/{moss.json()['character']['character_id']}/delete", json={}).status_code == 200
    view = owner.get(base).json()
    assert [c["name"] for c in view["characters"]] == ["Wren"]
    assert [(i["name"], len(i["photos"])) for i in view["items"]] == [("Long pier", 1)]
    # an id from another library is not in this Universe
    elsewhere = _world(owner, "Elsewhere")
    gull = _character(owner, elsewhere, "Gull")
    res = owner.post(f"{base}/cast/{gull['character_id']}", json={"name": "Taken"})
    assert res.status_code == 404 and res.json()["detail"] == f"Character {gull['character_id']} is not in this Universe"
    episode_pier = owner.post(f"/api/episodes/{EP}/props", json={"kind": "place", "name": "Dock"}).json()["item"]
    res = owner.post(f"{base}/props/{episode_pier['prop_id']}/delete")
    assert res.status_code == 404 and "is not in this Universe" in res.json()["detail"]
    # no character sheets are made in a Universe: a look is locked from an episode in it
    for tail in ("sheet/estimate", "sheet/generate", "approve"):
        assert owner.post(f"{base}/cast/{cid}/versions/1/{tail}", json={}).status_code in (404, 405), tail
    assert _money_rows()["cast_sheet_jobs"] == 0


async def test_a_photo_limit_and_a_locked_character_are_kept(studio, monkeypatch):
    owner, admin = _client(studio, "owner"), _client(studio, "admin")
    sid = _world(owner)
    pier = _item(owner, sid)
    monkeypatch.setattr(prop_store, "MAX_PHOTOS_PER_ITEM", 1)
    assert owner.post(f"{_base(sid)}/props/{pier['prop_id']}/photos", data={"rights": "original"},
                      files=_files()).status_code == 200
    res = owner.post(f"{_base(sid)}/props/{pier['prop_id']}/photos", data={"rights": "original"},
                     files=_files(image_bytes(seed=2)))
    assert res.status_code == 409 and "the limit is 1" in res.json()["detail"]
    wren = _character(owner, sid)
    _lock(wren["character_id"])
    res = owner.post(f"{_base(sid)}/cast/{wren['character_id']}/delete", json={"force": True, "reason": "redo"})
    assert (res.status_code, res.json()["detail"]) == (403, "Only an admin can remove a character with locked sheets")
    assert owner.post(f"{_base(sid)}/cast/{wren['character_id']}/delete", json={}).status_code == 409
    res = admin.post(f"{_base(sid)}/cast/{wren['character_id']}/delete", json={"force": True, "reason": "a redo"})
    assert res.status_code == 200, res.text


@pytest.mark.parametrize("tail,form", [
    ("cast/ch_000000000000/versions/1/images", {}), ("cast/with-picture", {"name": "Moss"}),
    ("props/pr_000000000000/photos", {}), ("props/with-photo", {"kind": "prop", "name": "Oar"}),
    ("photos", {})])
async def test_oversize_universe_uploads_get_413_before_anything_is_stored(studio, monkeypatch, tail, form):
    from PIL import Image
    owner = _client(studio, "owner")
    sid = _world(owner)
    wren, oar = _character(owner, sid), _item(owner, sid, "Oar", "prop")
    url = f"{_base(sid)}/{tail}".replace("ch_000000000000", wren["character_id"]).replace(
        "pr_000000000000", oar["prop_id"])
    noise = io.BytesIO()
    Image.frombytes("RGB", (640, 960), os.urandom(640 * 960 * 3)).save(noise, "PNG")
    data = noise.getvalue()
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) // 2))
    res = owner.post(url, data={"rights": "original", **form}, files=_files(data))  # refused by the declared length
    assert res.status_code == 413 and res.json()["detail"].startswith("That file is too large"), res.text
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) - 10))
    res = owner.post(url, data={"rights": "original", **form}, files=_files(data))  # refused by the handler
    assert res.status_code == 413 and "too large" in res.json()["detail"], res.text
    stored = cast_store.cast_root() / "images"
    assert not stored.exists() or not any(p.is_file() for p in stored.rglob("*"))
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data)))
    assert owner.post(url, data={"rights": "original", **form}, files=_files(data)).status_code == 200


@pytest.mark.parametrize("path,limited", [
    ("/api/worlds/sr_000000000000/cast/ch_000000000000/versions/1/images", True),
    ("/api/worlds/sr_000000000000/cast/with-picture", True),
    ("/api/worlds/sr_000000000000/props/pr_000000000000/photos", True),
    ("/api/worlds/sr_000000000000/props/with-photo", True),
    ("/api/worlds/sr_000000000000/photos", True),
    ("/api/worlds/sr_000000000000/cast", False), ("/api/worlds/sr_000000000000/props", False),
    ("/api/worlds/sr_000000000000/profile", False), ("/api/worlds/sr_000000000000/photos/ui_000000000000/delete", False),
])
async def test_the_size_limit_counts_a_universe_upload_without_a_declared_length(monkeypatch, path, limited):
    from api.cast_routes import CastUploadLimit, _is_upload
    assert _is_upload(path) is limited
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", "1000")
    called, sent = [], []

    async def app(scope, receive, send):
        called.append(await receive())

    chunks = [{"type": "http.request", "body": b"x" * 40_000, "more_body": True} for _ in range(3)]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    await CastUploadLimit(app)({"type": "http", "method": "POST", "headers": [], "path": path}, receive, send)
    if limited:
        assert called == [] and sent[0]["status"] == 413
    else:
        assert sent == [] and len(called) == 1


async def test_deletes_wait_while_any_episode_of_the_universe_is_being_made(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)
    wren = _character(owner, sid)
    image = owner.post(f"{_base(sid)}/cast/{wren['character_id']}/versions/1/images", data={"rights": "original"},
                       files=_files()).json()["image"]
    pier = _item(owner, sid)
    photo = owner.post(f"{_base(sid)}/props/{pier['prop_id']}/photos", data={"rights": "original"},
                       files=_files(image_bytes(seed=4))).json()["photo"]
    _link(studio, OTHER_EP, sid)
    _running(studio)
    for url in [f"{_base(sid)}/cast/{wren['character_id']}/delete",
                f"{_base(sid)}/cast/{wren['character_id']}/versions/1/images/{image['image_id']}/delete",
                f"{_base(sid)}/props/{pier['prop_id']}/delete",
                f"{_base(sid)}/props/{pier['prop_id']}/photos/{photo['image_id']}/delete"]:
        res = owner.post(url, json={})
        assert res.status_code == 409, (url, res.text)
        assert res.json()["detail"].startswith(f"Episode {OTHER_EP} is being made right now (run run-open); ")
    assert _character(owner, sid, "Moss")["name"] == "Moss"  # adding is fine
    assert len(cast_store.list_characters(("series", sid))) == 2 and len(prop_store.list_items(("series", sid))) == 1


# ------------------------------------------------------------------------------------- views


async def test_the_universe_page_shows_locks_problems_use_and_episodes(studio):
    owner = _client(studio, "owner")
    sid = _world(owner, logline="Lanterns keep the bay's secrets")
    wren = _character(owner, sid)
    _character(owner, sid, "Moss")
    _lock(wren["character_id"])
    pier = _item(owner, sid)
    _item(owner, sid, "Lantern", "prop")
    owner.post(f"{_base(sid)}/props/{pier['prop_id']}/photos", data={"rights": "original"},
               files=_files(image_bytes(seed=21, color=(40, 120, 90))))
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _link(studio, OTHER_EP, sid)
    _write_shots(EP, [_shot("A01", cast=["wren"], place="pier", props=["lantern"]),
                      _shot("A02", cast=[{"character": "wren", "version": 1}, "moss", "ghost"], place="pier")])
    _write_shots(OTHER_EP, [_shot("B01", cast=["wren"], props=["lantern", "unknown-prop"])])
    _finish(OTHER_EP)
    view = owner.get(_base(sid))
    assert view.status_code == 200, view.text
    body = view.json()
    chars = {c["name"]: c for c in body["characters"]}
    assert chars["Wren"]["locked_versions"] == [1] and chars["Moss"]["locked_versions"] == []
    assert chars["Wren"]["used_in"] == {"episodes": 2, "versions": {"1": 1}}
    assert chars["Moss"]["used_in"] == {"episodes": 1, "versions": {}}
    assert chars["Wren"]["versions"][0]["problems"] == []
    assert chars["Wren"]["primary_image"]["url"].startswith("/api/cast/images/ci_")
    assert chars["Moss"]["primary_image"] is None and chars["Wren"]["voice"] is None
    items = {i["name"]: i for i in body["items"]}
    assert items["Pier"]["used_in"] == {"episodes": 1} and items["Lantern"]["used_in"] == {"episodes": 2}
    assert items["Pier"]["primary_photo"]["url"].startswith("/api/props/photos/pi_")
    assert items["Lantern"]["primary_photo"] is None
    episodes = {e["episode_id"]: e for e in body["episodes"]}
    assert set(episodes) == {EP, OTHER_EP}
    assert (episodes[OTHER_EP]["finished"], episodes[OTHER_EP]["primary_file"], episodes[OTHER_EP]["owner_name"],
            episodes[OTHER_EP]["can_change"]) == (True, "phone", "Ivo Other", False)
    assert (episodes[EP]["finished"], episodes[EP]["owner_name"], episodes[EP]["can_change"]) == (False, "Tamsin Owner",
                                                                                                 True)
    counts = body["world"]["counts"]
    assert counts == {"characters": 2, "locked_characters": 1, "locked_versions": 1, "places": 1, "props": 1,
                      "episodes": 2, "finished_episodes": 1}
    # drift of a locked sheet shows on the page
    master = next(i for i in cast_store.get_character(wren["character_id"])["versions"][0]["images"]
                  if i["role"] == cast_store.MASTER_ROLE)
    cast_store.image_file(master["sha256"], master["ext"]).unlink()
    problems = next(c for c in owner.get(_base(sid)).json()["characters"] if c["name"] == "Wren")["versions"][0][
        "problems"]
    assert problems == [f"Wren v1: locked {cast_store.MASTER_ROLE} image is missing on disk"]
    # a missing or broken shots.json counts as nothing
    (paths.episode_dir(OTHER_EP) / "shots.json").write_text("{not json", encoding="utf-8")
    again = {c["name"]: c for c in owner.get(_base(sid)).json()["characters"]}
    assert again["Wren"]["used_in"]["episodes"] == 1


async def test_the_list_takes_the_same_queries_for_one_universe_or_many(studio, monkeypatch):
    """No query per Universe or per episode: five furnished Universes cost what one costs."""
    from hfvg.studio_db import create_episode
    owner = _client(studio, "owner")

    async def furnished(n: int) -> str:
        episode_id = f"ep{60 + n}lst"
        await create_episode(os.environ["DATABASE_PATH"], episode_id, user_id=studio.owner_id)
        sid = _world(owner, f"Bay {n}")
        wren = _character(owner, sid, f"Keeper{n}")
        _lock(wren["character_id"])
        place = _item(owner, sid)
        owner.post(f"{_base(sid)}/props/{place['prop_id']}/photos", data={"rights": "original"},
                   files=_files(image_bytes(seed=30 + n)))
        owner.post(f"{_base(sid)}/profile", json={"logline": f"Bay number {n}"})
        assert owner.post(f"/api/episodes/{episode_id}/series", json={"series_id": sid}).status_code == 200
        _write_shots(episode_id, [_shot("A01", cast=[f"keeper{n}"], place="pier")])
        _finish(episode_id)
        return sid

    def library_statements() -> tuple[list, list[str]]:
        cards: list = []
        seen = _statements(monkeypatch, lambda: cards.extend(owner.get("/api/worlds").json()["worlds"]))
        return cards, [s for s in seen if any(t in s for t in ("series", "cast_", "prop_", "universe_", "episodes",
                                                                "users"))]

    await furnished(0)
    one_card, one = library_statements()
    for n in range(1, 5):
        await furnished(n)
    cards, many = library_statements()
    assert len(one_card) == 1 and len(cards) == 5
    assert len(many) == len(one), "\n".join(many)
    for card in cards:
        assert card["counts"] == {"characters": 1, "locked_characters": 1, "locked_versions": 1, "places": 1,
                                  "props": 0, "episodes": 1, "finished_episodes": 1}
        assert card["cover"]["kind"] == "cast" and card["cover"]["url"].startswith("/api/cast/images/ci_")


async def test_the_card_cover_is_the_chosen_one_else_a_locked_look_else_a_place_else_the_board(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)

    def cover():
        return next(c for c in owner.get("/api/worlds").json()["worlds"] if c["series_id"] == sid)["cover"]

    assert cover() is None
    owner.post(f"{_base(sid)}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    total = owner.post(f"{_base(sid)}/board/estimate", json={}).json()["total_usd_micros"]
    board = owner.post(f"{_base(sid)}/board", json={"approved_total_usd_micros": total}).json()["board"]
    assert cover() is None  # a board that is not approved is not shown
    owner.post(f"{_base(sid)}/board/{board['board_id']}/approve")
    assert cover() == {"image_id": board["images"][0]["image_id"], "kind": "board",
                       "url": f"/api/worlds/images/{board['images'][0]['image_id']}",
                       "thumb_url": f"/api/worlds/images/{board['images'][0]['image_id']}/thumb"}
    place = _item(owner, sid)
    photo = owner.post(f"{_base(sid)}/props/{place['prop_id']}/photos", data={"rights": "original"},
                       files=_files(image_bytes(seed=40))).json()["photo"]
    assert cover() == {"image_id": photo["image_id"], "kind": "prop", "url": f"/api/props/photos/{photo['image_id']}",
                       "thumb_url": f"/api/props/photos/{photo['image_id']}/thumb"}
    wren = owner.post(f"{_base(sid)}/cast/with-picture", data={"name": "Wren", "rights": "original"},
                      files=_files(image_bytes(seed=41))).json()["character"]
    assert cover()["kind"] == "prop"  # an unlocked look is not a cover
    _lock(wren["character_id"])
    master = next(i for i in cast_store.get_character(wren["character_id"])["versions"][0]["images"]
                  if i["role"] == cast_store.MASTER_ROLE)
    assert cover()["image_id"] == master["image_id"]
    ref = owner.post(f"{_base(sid)}/photos", data={"rights": "original"}, files=_files(image_bytes(seed=42))).json()
    owner.post(f"{_base(sid)}/profile", json={"cover_image_id": ref["photo"]["image_id"]})
    assert cover()["kind"] == "reference" and cover()["image_id"] == ref["photo"]["image_id"]
    owner.post(f"{_base(sid)}/photos/{ref['photo']['image_id']}/delete")
    assert cover()["image_id"] == master["image_id"]  # a deleted cover falls back


# ------------------------------------------------------------------------------------- saved voices


async def test_saving_a_picked_voice_calls_no_provider_and_only_owners_see_it(studio):
    owner, other, admin = _client(studio, "owner"), _client(studio, "other"), _client(studio, "admin")
    sid = _world(owner)
    wren = _character(owner, sid)
    base = f"{_base(sid)}/cast/{wren['character_id']}/voice"
    picked = owner.post("/api/voices/pick", json={"description": "a calm low voice for a lantern keeper"})
    assert picked.status_code == 200, picked.text
    voice = picked.json()
    before = _money_rows()
    with respx.mock(assert_all_called=False) as network:
        res = owner.post(base, json={"voice_id": voice["voice_id"], "name": voice["name"],
                                     "description": "a calm low voice", "preview_url": voice["preview_url"]})
        assert res.status_code == 200, res.text
        assert res.json()["voice"]["voice_id"] == voice["voice_id"]
        narrator = owner.post(f"{_base(sid)}/narrator-voice", json={
            "voice_id": voice["voice_id"], "name": voice["name"], "preview_url": voice["preview_url"]})
        assert narrator.status_code == 200 and narrator.json()["voice"]["name"] == voice["name"]
    assert network.calls.call_count == 0 and _money_rows() == before
    view = owner.get(_base(sid)).json()
    assert view["characters"][0]["voice"]["voice_id"] == voice["voice_id"]
    assert view["characters"][0]["voice"]["preview_url"] == voice["preview_url"]
    assert view["world"]["profile"]["narrator_voice"]["voice_id"] == voice["voice_id"]
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        stored = json.dumps([list(r) for r in conn.execute("SELECT * FROM character_voices")])
    assert "xi-api-key" not in stored.lower() and "api_key" not in stored.lower()
    # another member: the 404 of a missing Universe; an episode editor: the owner refusal
    assert other.post(base, json={"voice_id": "fake-voice-2"}).status_code == 404
    _link(studio, OTHER_EP, sid)
    res = other.post(base, json={"voice_id": "fake-voice-2"})
    assert (res.status_code, res.json().get("detail")) == (403, f"Universe Lantern Bay {REFUSED}")
    assert voice["voice_id"] not in other.get(_base(sid)).text
    # admins may; clearing (null or an empty id) removes it; bad picks are refused
    assert admin.post(base, json={"voice_id": None}).json()["voice"] is None
    assert world_store.character_voices([wren["character_id"]]) == {}
    owner.post(base, json={"voice_id": "fake-voice-1", "name": "Calm"})
    assert owner.post(base, json={"voice_id": ""}).json()["voice"] is None
    assert world_store.character_voices([wren["character_id"]]) == {}
    for body, detail in [({"voice_id": "fake-voice-1", "preview_url": "http://example.test/a.mp3"},
                          "The voice preview must be the link the voice pick returned"),
                         ({"voice_id": "fake-voice-1", "preview_url": "javascript:alert(1)"},
                          "The voice preview must be the link the voice pick returned"),
                         ({"voice_id": "bad id!"}, "Pick a voice first (Find a voice), then save it")]:
        res = owner.post(base, json=body)
        assert (res.status_code, res.json()["detail"]) == (400, detail), body
    assert world_store.character_voices([wren["character_id"]]) == {}
    assert owner.post(f"{_base(sid)}/narrator-voice", json={"voice_id": None}).json()["voice"] is None
    # a deleted character takes its saved voice along
    owner.post(base, json={"voice_id": "fake-voice-3", "name": "Bright"})
    owner.post(f"{_base(sid)}/cast/{wren['character_id']}/delete", json={})
    assert world_store.character_voices([wren["character_id"]]) == {}


# ------------------------------------------------------------------------------------- creation


async def test_the_creation_flow_runs_on_placeholders_without_a_provider_or_a_ledger_row(studio):
    owner = _client(studio, "owner")
    before = _money_rows()
    with respx.mock(assert_all_called=False) as network:
        sid = _world(owner, logline="Lanterns keep the bay's secrets")
        base = _base(sid)
        res = owner.post(f"{base}/board/estimate", json={})
        assert res.status_code == 409 and res.json()["detail"].startswith("Add a reference first")
        note = owner.post(f"{base}/notes", json={"topic": "premise", "body": NOTE_WORDS})
        assert note.status_code == 200, note.text
        owner.post(f"{base}/notes", json={"topic": "characters", "body": "Wren: a keeper\nMoss: a mapmaker"})
        owner.post(f"{base}/notes", json={"topic": "places", "body": "The Pier: wet planks\nThe Bell Tower"})
        bad = owner.post(f"{base}/notes", json={"topic": "plot", "body": "nope"})
        assert bad.status_code == 400
        edited = owner.post(f"{base}/notes/{note.json()['note']['note_id']}", json={"body": NOTE_WORDS + " at night"})
        assert edited.json()["note"]["body"] == NOTE_WORDS + " at night"
        photo = owner.post(f"{base}/photos", data={"rights": "original", "caption": "foggy pier"}, files=_files())
        assert photo.status_code == 200, photo.text
        assert owner.post(f"{base}/photos", data={"rights": "original"}, files=_files()).json()["duplicate"] is True
        assert owner.post(f"{base}/photos", data={}, files=_files(image_bytes(seed=8))).status_code == 400

        estimate = owner.post(f"{base}/board/estimate", json={})
        assert estimate.status_code == 200, estimate.text
        from hfvg.estimates import still_call_usd_micros
        total = estimate.json()["total_usd_micros"]
        assert total == world_store.BOARD_IMAGES * still_call_usd_micros() and estimate.json()["dry_run"] is True
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total - 1})
        assert res.status_code == 409 and res.json()["detail"].startswith("The estimate is now")
        board = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert board.status_code == 200, board.text
        first = board.json()["board"]
        assert (first["version"], first["status"], len(first["images"])) == (1, "ready", world_store.BOARD_IMAGES)
        assert owner.get(first["images"][0]["url"]).headers["content-type"] == "image/png"
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert res.status_code == 409 and res.json()["detail"].startswith("Approve this mood board, or say what")
        redo = owner.post(f"{base}/board", json={"approved_total_usd_micros": total, "feedback": "warmer light"})
        assert redo.status_code == 200 and redo.json()["board"]["version"] == 2
        assert redo.json()["estimate"]["redo"] is True
        res = owner.post(f"{base}/board/{first['board_id']}/approve")
        assert res.status_code == 409 and res.json()["detail"] == "Only the latest mood board can be approved"
        res = owner.post(f"{base}/bible")
        assert res.status_code == 409 and res.json()["detail"].startswith("Approve a mood board first")
        approved = owner.post(f"{base}/board/{redo.json()['board']['board_id']}/approve")
        assert approved.status_code == 200 and approved.json()["board"]["status"] == "approved"
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total, "feedback": "again"})
        assert res.status_code == 409 and "approved" in res.json()["detail"]
        bible = owner.post(f"{base}/bible")
        assert bible.status_code == 200, bible.text
        made = bible.json()["bible"]
        assert made["created"]["characters"] and made["created"]["places"]
        assert owner.post(f"{base}/bible").status_code == 409
    assert network.calls.call_count == 0 and _money_rows() == before
    _nothing_started(studio)
    view = owner.get(base).json()
    assert view["look"]["version"] == 2 and len(view["look"]["images"]) == world_store.BOARD_IMAGES
    assert {"Wren", "Moss"} <= {c["name"] for c in view["characters"]}
    assert view["world"]["profile"]["bible"]
    creation = view["creation"]
    assert [b["version"] for b in creation["boards"]] == [1, 2] and creation["boards"][1]["prompt"]
    assert creation["board"] == {"available": True, "image_count": world_store.BOARD_IMAGES, "dry_run": True,
                                 "unavailable": None}
    assert owner.post(f"{base}/notes/{note.json()['note']['note_id']}/delete").status_code == 200
    assert owner.post(f"{base}/photos/{photo.json()['photo']['image_id']}/delete").status_code == 200


class _PricedProvider:
    """HiggsfieldStillProvider stand-in for the API: its price for each picture, and nothing else
    (the API never uploads or submits; the worker does, picture by picture)."""

    prompts: list[str] = []

    def __init__(self, model_path=None, **_kw):
        self.model_path = model_path

    async def estimate_usd_and_credits(self, **params):
        _PricedProvider.prompts.append(params["prompt"])
        return PRICE, 6.5

    async def close(self):
        pass


def _live(api_main, monkeypatch, *, pollers: int = 1) -> None:
    """Live mode, with ``pollers`` workers polling the mood board task queue."""
    from hfvg.providers import higgsfield_still
    for key, value in {"DRY_RUN": "false", "HIGGSFIELD_API_KEY": "test_id:test_secret",
                       "HIGGSFIELD_BASE_URL": HF_BASE}.items():
        monkeypatch.setenv(key, value)
    _PricedProvider.prompts = []
    monkeypatch.setattr(higgsfield_still, "HiggsfieldStillProvider", _PricedProvider)
    api_main.temporal_client.namespace = "default"
    api_main.temporal_client.workflow_service.describe_task_queue = AsyncMock(
        return_value=MagicMock(pollers=[MagicMock()] * pollers))


@contextmanager
def _provider_http():
    """Provider HTTP of a live test: the balance gate's usage lookup is answered (no spend
    reported); any other request is recorded and refused. Yields (usage route, other route)."""
    with respx.mock(assert_all_called=False) as router:
        usage = router.get(f"{HF_BASE}/models/usage").mock(
            return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
        other = router.route().mock(return_value=httpx.Response(599))
        yield usage, other


async def test_a_live_board_without_a_worker_for_it_is_a_plain_409_before_anything_is_asked_or_charged(
        studio, monkeypatch):
    """A worker older than live mood boards does not poll their task queue: the owner gets a plain
    409 (an unreachable Temporal a plain 503) before any price is asked, anything recorded or started."""
    from api import world_routes
    owner = _client(studio, "owner")
    sid = _world(owner)
    owner.post(f"{_base(sid)}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    _live(studio, monkeypatch, pollers=0)
    before = _money_rows()
    with _provider_http() as (usage, other):
        for url, body in [(f"{_base(sid)}/board/estimate", {}),
                          (f"{_base(sid)}/board", {"approved_total_usd_micros": 10**9})]:
            res = owner.post(url, json=body)
            assert (res.status_code, res.json().get("detail")) == (409, world_routes.BOARD_WORKER_MISSING), url
        view = owner.get(_base(sid)).json()
        assert view["creation"]["board"] == {"available": False, "image_count": world_store.BOARD_IMAGES,
                                             "dry_run": False, "unavailable": world_routes.BOARD_WORKER_MISSING}
        assert view["creation"]["boards"] == [] and view["creation"]["board_request"] is None
        asked = studio.temporal_client.workflow_service.describe_task_queue.await_args.args[0]
        assert (asked.namespace, asked.task_queue.name) == ("default", "hfvg-tasks-universe")
        _nothing_started(studio)
        studio.temporal_client.workflow_service.describe_task_queue = AsyncMock(
            side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
        res = owner.post(f"{_base(sid)}/board/estimate", json={})
        assert (res.status_code, res.json()["detail"]) == (503, world_routes.BOARD_WORKER_UNREACHABLE)
        creation = owner.get(_base(sid)).json()["creation"]
        assert creation["board"]["unavailable"] == world_routes.BOARD_WORKER_UNREACHABLE
        studio.temporal_client = None
        res = owner.post(f"{_base(sid)}/board", json={"approved_total_usd_micros": 10**9})
        assert (res.status_code, res.json()["detail"]) == (503, world_routes.BOARD_WORKER_UNREACHABLE)
    assert (usage.call_count, other.call_count) == (0, 0) and _PricedProvider.prompts == []
    assert world_store.list_boards(sid) == [] and _money_rows() == before


async def test_a_live_board_is_priced_by_the_provider_checked_and_handed_to_the_worker(studio, monkeypatch):
    from datetime import timedelta

    from api import world_routes
    from hfvg.budget import BudgetLedger
    from hfvg.pricing import usd_to_micros
    owner = _client(studio, "owner")
    sid = _world(owner)
    base = _base(sid)
    owner.post(f"{base}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    photo = owner.post(f"{base}/photos", data={"rights": "original", "caption": "foggy pier"}, files=_files())
    _live(studio, monkeypatch)
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    count = world_store.BOARD_IMAGES
    total = PRICE * count
    with _provider_http() as (usage, other):
        estimate = owner.post(f"{base}/board/estimate", json={})
        assert estimate.status_code == 200, estimate.text
        est = estimate.json()
        assert (est["dry_run"], est["redo"], est["per_image_usd_micros"], est["total_usd_micros"]) == (
            False, False, [PRICE] * count, total)
        assert est["references_per_picture"] == 1 and est["model_path"]
        assert est["budget"] == {"line": "U1_mood_board", "cap_usd_micros": 4_000_000, "stop_usd_micros": 3_200_000,
                                 "spent_usd_micros": 0, "reserved_usd_micros": 0, "available_usd_micros": 3_200_000}
        assert len(_PricedProvider.prompts) == count  # the provider's own price for each picture
        # refused before anything is recorded: above the approval, the budget line, GC.01, the gate
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total - 1})
        assert res.status_code == 409 and res.json()["detail"].startswith("The estimate is now")
        key = f"universe-{sid}"
        await ledger.ensure_line(key, "U1_mood_board", 4_000_000)
        assert await ledger.reserve(key, "U1_mood_board", 2_000_000, "an earlier board", job_id="uboard-earlier-0")
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert res.status_code == 409 and "mood board budget has $1.20 left" in res.json()["detail"]
        await ledger.release(key, "U1_mood_board", 2_000_000, "test", job_id="uboard-earlier-0")
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert res.status_code == 409 and "GC.01: no manual Higgsfield balance recorded" in res.json()["detail"]
        await ledger.set_manual_balance(usd_to_micros("1.00"), "test", "low")
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert res.status_code == 409 and res.json()["detail"].startswith("Balance check failed")
        _nothing_started(studio)
        assert world_store.open_board_request(sid) is None
        await ledger.set_manual_balance(usd_to_micros("100.00"), "test", "topped up")
        before = _money_rows()
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total + 100_000})
        assert res.status_code == 200, res.text
        assert _money_rows() == before  # the worker reserves and pays picture by picture; the API holds nothing
        _check_started_board(studio, res.json(), sid, total, count)
        # while it is made: no second board, and its references stay
        res = owner.post(f"{base}/board/estimate", json={})
        assert (res.status_code, res.json()["detail"]) == (409, world_store.BOARD_BEING_MADE)
        res = owner.post(f"{base}/photos/{photo.json()['photo']['image_id']}/delete")
        assert res.status_code == 409 and res.json()["detail"].startswith(world_store.BOARD_BEING_MADE)
        _running(studio)
        creation = owner.get(base).json()["creation"]
        assert creation["board_request"]["status"] == "making" and creation["board"]["available"] is True
        # its workflow ended without finishing it (a crashed worker): the page ends it, with the reason
        studio.temporal_client.handle.describe = AsyncMock(
            return_value=MagicMock(status=WorkflowExecutionStatus.FAILED))
        request = owner.get(base).json()["creation"]["board_request"]
        assert (request["status"], request["error"]) == ("failed", world_store.BOARD_STOPPED)
        assert [j["status"] for j in request["jobs"]] == ["skipped"] * count
        # a request Temporal does not know: kept while its start may still be on the way, then ended
        studio.temporal_client.handle.describe = AsyncMock(
            side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
        assert owner.post(f"{base}/board", json={"approved_total_usd_micros": total}).status_code == 200
        assert owner.get(base).json()["creation"]["board_request"]["status"] == "making"
        monkeypatch.setattr(world_routes, "BOARD_START_GRACE", timedelta(0))
        assert owner.get(base).json()["creation"]["board_request"]["status"] == "failed"
        # the worker could not be asked: the request ends at once, nothing was made or charged
        studio.temporal_client.start_workflow = AsyncMock(side_effect=RuntimeError("connection reset by peer"))
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": total})
        assert (res.status_code, res.json()["detail"]) == (503, world_routes.BOARD_NOT_STARTED)
        request = owner.get(base).json()["creation"]["board_request"]
        assert (request["status"], request["error"]) == ("failed", world_routes.BOARD_NOT_STARTED)
    assert other.call_count == 0 and usage.call_count > 0
    assert _money_rows() == before and world_store.list_boards(sid) == []


def _check_started_board(api_main, body: dict, sid: str, total: int, count: int) -> None:
    """The started board: a request being made, its jobs queued, and UniverseBoardWorkflow on the
    mood board task queue with each picture's share of the approved total (100_000 above the estimate)."""
    from api import world_routes
    from hfvg.workflows.universe_board import UniverseBoardWorkflow
    made = body["request"]
    assert body["board"] is None and body["estimate"]["total_usd_micros"] == total
    assert (made["status"], made["made"], made["image_count"]) == ("making", 0, count)
    assert [j["status"] for j in made["jobs"]] == ["queued"] * count and "prompt" not in made
    start = api_main.temporal_client.start_workflow
    start.assert_awaited_once()
    assert start.await_args.args[0] == UniverseBoardWorkflow.run
    assert start.await_args.kwargs == {"id": f"universe-board-{made['request_id']}",
                                       "task_queue": "hfvg-tasks-universe",
                                       "execution_timeout": world_routes.BOARD_RUN_TIMEOUT}
    jobs = start.await_args.args[1]["jobs"]
    # each picture may cost up to its share of the approved total, never more in sum
    assert [j["approved_usd_micros"] for j in jobs] == [PRICE + 100_000 // count] * count
    assert sum(j["approved_usd_micros"] for j in jobs) <= total + 100_000
    assert {j["ledger_key"] for j in jobs} == {f"universe-{sid}"}
    assert {j["owner_user_id"] for j in jobs} == {api_main.owner_id}
    assert [len(j["refs"]) for j in jobs] == [1] * count


async def test_a_live_redo_answers_the_feedback_on_the_latest_board(studio, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.pricing import usd_to_micros
    owner = _client(studio, "owner")
    sid = _world(owner)
    base = _base(sid)
    owner.post(f"{base}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    plan = world_store.plan_board(sid)
    first = world_store.create_board(sid, plan, estimate_usd_micros=0, approved_total_usd_micros=0, dry_run=True,
                                     images=world_store.placeholder_board_images("x", plan["photo_files"]),
                                     placeholder=True, requested_by="tester")
    _live(studio, monkeypatch)
    await BudgetLedger(os.environ["DATABASE_PATH"]).set_manual_balance(usd_to_micros("100.00"), "test", "balance")
    feedback = "warmer light, fewer boats"
    with _provider_http() as (_usage, other):
        res = owner.post(f"{base}/board/estimate", json={})
        assert res.status_code == 409 and res.json()["detail"].startswith("Approve this mood board, or say what")
        estimate = owner.post(f"{base}/board/estimate", json={"feedback": feedback}).json()
        assert estimate["redo"] is True and estimate["version"] == 2
        assert all(f"Change from the last board: {feedback}" in p for p in _PricedProvider.prompts)
        res = owner.post(f"{base}/board", json={"approved_total_usd_micros": estimate["total_usd_micros"],
                                                "feedback": feedback})
        assert res.status_code == 200, res.text
        made = res.json()["request"]
        assert (made["answers_feedback"], made["replaces_board_id"]) == (feedback, first["board_id"])
        jobs = studio.temporal_client.start_workflow.await_args.args[1]["jobs"]
        assert all(f"Change from the last board: {feedback}" in j["prompt"] for j in jobs)
        # the board it answers waits for the redo; it cannot be approved meanwhile
        res = owner.post(f"{base}/board/{first['board_id']}/approve")
        assert res.status_code == 409 and res.json()["detail"].startswith(world_store.BOARD_BEING_MADE)
    assert other.call_count == 0


async def test_the_fake_bible_writer_is_for_dry_run_tests_only(studio, monkeypatch):
    owner = _client(studio, "owner")
    sid = _world(owner)
    owner.post(f"{_base(sid)}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    plan = world_store.plan_board(sid)
    world_store.create_board(sid, plan, estimate_usd_micros=0, approved_total_usd_micros=0, dry_run=True,
                             images=world_store.placeholder_board_images("x", plan["photo_files"]), placeholder=True,
                             requested_by="tester")
    world_store.approve_board(sid, world_store.list_boards(sid)[0]["board_id"], "tester")
    monkeypatch.setenv("DRY_RUN", "false")
    res = owner.post(f"{_base(sid)}/bible")
    assert res.status_code == 500 and "DRAFT_LLM_FAKE" in res.json()["detail"]
    assert world_store.get_profile(sid)["bible_board_id"] is None


async def test_bible_entries_the_studio_cannot_generate_are_left_out(studio, monkeypatch):
    from api import world_routes
    from hfvg.content_policy import hard_stop_terms
    owner = _client(studio, "owner")
    sid = _world(owner)
    owner.post(f"{_base(sid)}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    plan = world_store.plan_board(sid)
    world_store.create_board(sid, plan, estimate_usd_micros=0, approved_total_usd_micros=0, dry_run=True,
                             images=world_store.placeholder_board_images("x", plan["photo_files"]), placeholder=True,
                             requested_by="tester")
    world_store.approve_board(sid, world_store.list_boards(sid)[0]["board_id"], "tester")
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))

    async def model(_inputs):
        return {"bible": "A calm bay.", "style_notes": f"lots of {bad}",
                "characters": [{"name": "Wren", "role": "lead", "description": "a keeper"},
                               {"name": "Grim", "role": "foe", "description": f"covered in {bad}"}],
                "places": [{"name": "Pier", "description": "wet planks"}], "props": []}

    monkeypatch.setattr(world_routes, "_bible_from_model", model)
    res = owner.post(f"{_base(sid)}/bible")
    assert res.status_code == 200, res.text
    assert sorted(res.json()["bible"]["dropped"]) == ["Grim", "style notes"]
    assert [c["name"] for c in cast_store.list_characters(("series", sid))] == ["Wren"]
    assert world_store.get_profile(sid)["style_notes"] == ""


async def test_universe_images_follow_who_may_see_them(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    sid = _world(owner)
    owner.post(f"{_base(sid)}/notes", json={"topic": "premise", "body": NOTE_WORDS})
    ref = owner.post(f"{_base(sid)}/photos", data={"rights": "original"}, files=_files()).json()["photo"]
    total = owner.post(f"{_base(sid)}/board/estimate", json={}).json()["total_usd_micros"]
    board = owner.post(f"{_base(sid)}/board", json={"approved_total_usd_micros": total}).json()["board"]
    image = board["images"][0]
    assert owner.get(ref["url"]).status_code == 200 and owner.get(image["thumb_url"]).status_code == 200
    missing = other.get("/api/worlds/images/ui_ffffffffffff")
    for url in (ref["url"], ref["thumb_url"], image["url"]):
        res = other.get(url)
        assert res.status_code == 404 and res.json()["detail"] == f"Image {url.split('/')[4]} not found", url
    assert missing.json()["detail"] == "Image ui_ffffffffffff not found"
    _link(studio, OTHER_EP, sid)
    # an episode editor sees the cover and an approved board, not the references or an open board
    assert other.get(ref["url"]).status_code == 404 and other.get(image["url"]).status_code == 404
    owner.post(f"{_base(sid)}/board/{board['board_id']}/approve")
    assert other.get(image["url"]).status_code == 200
    owner.post(f"{_base(sid)}/profile", json={"cover_image_id": ref["image_id"]})
    assert other.get(ref["url"]).status_code == 200
    from api.cast_routes import PRIVATE_HEADERS
    res = owner.get(ref["url"])
    assert res.headers["cache-control"] == PRIVATE_HEADERS["Cache-Control"]


# ------------------------------------------------------------------------------------- an episode's Universe


async def test_the_episode_universe_view_shows_the_strip_only_to_who_may_see_the_cast(studio):
    owner, other, admin = _client(studio, "owner"), _client(studio, "other"), _client(studio, "admin")
    view = owner.get(f"/api/episodes/{EP}/universe").json()
    assert view == {"episode_id": EP, "owner": {"user_id": studio.owner_id, "name": "Tamsin Owner"}, "can_act": True,
                    "linked": False, "universe": None, "strip": None, "lock": None}
    sid = _world(owner)
    wren, moss = _character(owner, sid), _character(owner, sid, "Moss")
    _lock(wren["character_id"])
    _item(owner, sid)
    _item(owner, sid, "Lantern", "prop")
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _write_shots(EP, [_shot("A01", cast=["wren", "moss"], place="pier", props=["lantern", "nope"]),
                      _shot("A02", cast=[{"character": "moss", "version": 2}, "ghost"])])
    view = owner.get(f"/api/episodes/{EP}/universe").json()
    assert view["linked"] is True and view["universe"] == {"series_id": sid, "name": "Lantern Bay"}
    strip = view["strip"]
    assert [(c["name"], c["version"], c["exists"], c["locked"]) for c in strip["characters"]] == [
        ("Wren", 1, True, True), ("Moss", 1, True, False), ("Moss", 2, False, False)]
    assert strip["characters"][0]["image"]["url"].startswith("/api/cast/images/ci_")
    assert [p["name"] for p in strip["places"]] == ["Pier"] and [p["name"] for p in strip["props"]] == ["Lantern"]
    assert view["lock"] == {"required": True, "unlocked": [
        {"character": "moss", "character_id": moss["character_id"], "name": "Moss", "version": 1}]}
    # another member who may not see the cast: who made it and that they may not act, nothing else
    theirs = other.get(f"/api/episodes/{EP}/universe")
    assert theirs.json() == {"episode_id": EP, "owner": {"user_id": studio.owner_id, "name": "Tamsin Owner"},
                             "can_act": False, "linked": None, "universe": None, "strip": None, "lock": None}
    for private in ("Wren", "Moss", "Lantern", "Pier", sid, "/api/cast/images"):
        assert private not in theirs.text.replace("Tamsin Owner", ""), private
    assert admin.get(f"/api/episodes/{EP}/universe").json()["can_act"] is True
    assert admin.get(f"/api/episodes/{EP}/universe").json()["strip"] == strip


async def test_a_universe_episode_waits_for_locked_looks_before_its_shot_plan(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)
    wren, moss = _character(owner, sid), _character(owner, sid, "Moss")
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _write_shots(EP, [_shot("A01", cast=["wren"]), _shot("A02", cast=["moss", "ghost"])])
    res = owner.post(f"/api/episodes/{EP}/approve", json={"gate_id": "G1_SHOT_PLAN"})
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == world_store.lock_message([{"name": "Wren", "version": 1},
                                                             {"name": "Moss", "version": 1}])
    assert res.json()["detail"].startswith("Lock the look of Wren and Moss before the shot plan is approved")
    studio.temporal_client.handle.execute_update.assert_not_called()
    _lock(wren["character_id"])
    res = owner.post(f"/api/episodes/{EP}/approve", json={"gate_id": "g1"})
    assert res.status_code == 409 and res.json()["detail"].startswith("Lock the look of Moss before")
    _lock(moss["character_id"])
    res = owner.post(f"/api/episodes/{EP}/approve", json={"gate_id": "G1_SHOT_PLAN"})
    assert res.status_code == 200, res.text
    studio.temporal_client.handle.execute_update.assert_awaited_once_with("approve_g1_shot_plan", False)
    # other gates and episodes outside a Universe are not affected
    _write_shots(OTHER_EP, [_shot("B01", cast=["wren"])])
    studio.temporal_client.handle.execute_update.reset_mock()
    res = _client(studio, "other").post(f"/api/episodes/{OTHER_EP}/approve", json={"gate_id": "G1_SHOT_PLAN"})
    assert res.status_code == 200, res.text
    studio.temporal_client.handle.execute_update.assert_awaited_once_with("approve_g1_shot_plan", False)


START = {"episode_id": EP, "dry_run": True, "beatmap_content": "# Episode ep91wld Beatmap\n\nA01 a pier at dawn\n"}


async def test_a_universe_episode_does_not_start_before_every_look_it_uses_is_locked(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    sid = _world(owner)
    wren, moss = _character(owner, sid), _character(owner, sid, "Moss")
    _lock(wren["character_id"])
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _write_shots(EP, [_shot("A01", cast=["wren"]), _shot("A02", cast=["moss"])])
    res = owner.post("/api/episodes", json=START)
    assert (res.status_code, res.json().get("detail")) == (
        409, world_store.lock_message([{"name": "Moss", "version": 1}], "start"))
    assert res.json()["detail"].startswith("Lock the look of Moss before this video starts: this video is in a Universe")
    studio.temporal_client.start_workflow.assert_not_called()
    _lock(moss["character_id"])
    res = owner.post("/api/episodes", json=START)
    assert res.status_code == 200, res.text
    studio.temporal_client.start_workflow.assert_awaited_once()
    # outside a Universe a character without a locked look does not hold the start
    _write_shots(OTHER_EP, [_shot("B01", cast=["sky"])])
    cast_store.create_character(("episode", OTHER_EP), "Sky", "lead", "a kite flyer", None, "tester")
    res = other.post("/api/episodes", json={**START, "episode_id": OTHER_EP,
                                            "beatmap_content": "# Episode ep92wld Beatmap\n\nB01 a kite\n"})
    assert res.status_code == 200, res.text


async def test_a_universe_episode_does_not_start_again_before_its_looks_are_locked(studio):
    from tests.test_episode_ownership import _ended_run
    from tests.test_run_uploads_voice_recheck import _temporal
    owner = _client(studio, "owner")
    sid = _world(owner)
    wren = _character(owner, sid)
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _write_shots(EP, [_shot("A01", cast=["wren"])])
    studio.temporal_client = _temporal("FAILED")
    _ended_run(studio, EP)
    res = owner.post(f"/api/episodes/{EP}/start-again")
    assert (res.status_code, res.json().get("detail")) == (
        409, world_store.lock_message([{"name": "Wren", "version": 1}], "start"))
    studio.temporal_client.start_workflow.assert_not_called()
    _lock(wren["character_id"])
    res = owner.post(f"/api/episodes/{EP}/start-again")
    assert res.status_code == 200, res.text
    studio.temporal_client.start_workflow.assert_awaited_once()


async def test_at_the_shot_plan_a_universe_episode_takes_only_characters_with_a_locked_look(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)
    wren = _character(owner, sid)
    _character(owner, sid, "Moss")
    _lock(wren["character_id"])
    owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    _write_shots(EP, [_shot("A01", cast=["wren"]), _shot("A02")])
    handle = studio.temporal_client.handle
    handle.describe = AsyncMock(return_value=MagicMock(status=WorkflowExecutionStatus.RUNNING, run_id="run-open"))
    state = {"current_gate": "G1_SHOT_PLAN", "passed_gates": [], "approvals": {"g1_shot_plan": False},
             "replanning": False}
    handle.query = AsyncMock(side_effect=lambda name, *a, **k: {
        "reopen_snapshot": {"attrs": {"reopen_log": [], "_dry_run": True}}, "get_state": state}[name])
    before = (paths.episode_dir(EP) / "shots.json").read_text()
    res = owner.post(f"/api/episodes/{EP}/shot-plan", json={"shots": {"A02": {"cast": ["moss"]}}})
    assert (res.status_code, res.json().get("detail")) == (
        409, world_store.lock_message([{"name": "Moss", "version": 1}], "pick"))
    assert res.json()["detail"].startswith("Lock the look of Moss before they are put in a shot")
    assert (paths.episode_dir(EP) / "shots.json").read_text() == before
    handle.execute_update.assert_not_called()
    # a character with a locked look goes in, through the run's re-plan
    res = owner.post(f"/api/episodes/{EP}/shot-plan", json={"shots": {"A02": {"cast": ["wren"]}}})
    assert res.status_code == 200, res.text
    assert handle.execute_update.call_args.args[0] == "replan_shots"
    # before a run the pick is saved: the start waits for the lock instead
    handle.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    res = owner.post(f"/api/episodes/{EP}/shot-plan", json={"shots": {"A02": {"cast": ["moss"]}}})
    assert res.status_code == 200, res.text
    assert owner.post("/api/episodes", json=START).status_code == 409


# ------------------------------------------------------------------------------------- drafts


def _model_input(api_main, client, body: dict) -> tuple:
    """The draft response and what the (mocked) model was asked."""
    import random
    from unittest.mock import patch

    from tests.test_draft_repair import _files as draft_files
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=json.dumps(draft_files())))]
    mock = MagicMock()
    mock.chat.completions.create = AsyncMock(return_value=completion)
    with patch("openai.AsyncOpenAI", return_value=mock), patch.object(random, "randint", return_value=42), \
            patch.object(random, "choices", return_value=["g", "l", "d"]):
        res = client.post("/api/episodes/draft-from-idea", json=body)
    calls = mock.chat.completions.create.call_args_list
    return res, (calls[0].kwargs if calls else None)


async def test_a_draft_without_a_universe_asks_the_model_exactly_what_it_asked_before(studio, monkeypatch):
    """tests/fixtures/draft_input_golden.json was captured on main e62c9bc, before world_id existed."""
    from hfvg.config import config
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    from pathlib import Path
    golden = json.loads((Path(__file__).parent / "fixtures" / "draft_input_golden.json").read_text("utf-8"))
    assert len(golden["cases"]) == 4
    client = _client(studio, "admin")
    for case in golden["cases"]:
        for body in (case["request"], {**case["request"], "world_id": None}):
            res, asked = _model_input(studio, client, body)
            assert res.status_code == case["status"], res.text
            assert asked["model"] == config.STORY_SHAPING_MODEL
            assert {k: asked[k] for k in ("messages", "temperature", "response_format")} == {
                k: case["model_input"][k] for k in ("messages", "temperature", "response_format")}


async def test_a_draft_in_a_universe_is_written_with_its_context(studio, monkeypatch):
    from hfvg.config import config
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    owner, other = _client(studio, "owner"), _client(studio, "other")
    sid = _world(owner, logline="Lanterns keep the bay's secrets")
    owner.post(f"{_base(sid)}/profile", json={"bible": SECRET_WORDS, "style_notes": STYLE_WORDS})
    _character(owner, sid)
    _item(owner, sid)
    request = {"idea": "The keeper finds a lantern that hums", "archetype_id": "cozy-hangout", "target_duration": 12}
    plain_res, plain = _model_input(studio, owner, request)
    res, asked = _model_input(studio, owner, {**request, "world_id": sid})
    assert (plain_res.status_code, res.status_code) == (200, 200), res.text
    assert asked["messages"][0] == plain["messages"][0]
    context = world_store.draft_context(sid, vo_mode="none", include_private=True)["context"]
    assert asked["messages"][1]["content"] == plain["messages"][1]["content"] + "\n\n" + context
    assert SECRET_WORDS in context and "- Wren (lead)" in context and "- Pier" in context
    # an episode editor drafts with the names and looks, not the story bible or style notes
    _link(studio, OTHER_EP, sid)
    res, asked = _model_input(studio, other, {**request, "world_id": sid})
    assert res.status_code == 200 and "- Wren (lead)" in asked["messages"][1]["content"]
    assert SECRET_WORDS not in json.dumps(asked) and STYLE_WORDS not in json.dumps(asked)
    # a Universe the caller may not see: the 404 of a missing one, and the model is not asked
    hidden = _world(owner, "Hidden Harbor")
    for world_id in (hidden, MISSING):
        res, asked = _model_input(studio, other, {**request, "world_id": world_id})
        assert (res.status_code, res.json()["detail"], asked) == (404, f"Universe {world_id} not found", None)


async def test_a_dialogue_draft_in_a_universe_gives_the_lines_to_its_characters(studio):
    owner = _client(studio, "owner")
    sid = _world(owner)
    _character(owner, sid, "Wren")
    _character(owner, sid, "Moss")
    request = {"idea": "Two keepers argue about which lantern to light first", "archetype_id": "nostalgia-time-travel",
               "target_duration": 30}
    plain = owner.post("/api/episodes/draft-from-idea", json=request)
    res = owner.post("/api/episodes/draft-from-idea", json={**request, "world_id": sid})
    assert (plain.status_code, res.status_code) == (200, 200), res.text
    def speakers(draft: dict) -> list[str]:
        return [line["speaker"] for line in draft["audio_plan_json"]["vo"]["lines"]]

    card = speakers(plain.json())[:2]
    assert len(set(card)) == 2 and not {"Wren", "Moss"} & set(card)
    universe = res.json()
    order = [c["name"] for c in cast_store.list_characters(("series", sid))]  # the Universe page's order
    assert sorted(order) == ["Moss", "Wren"]
    assert speakers(universe)[:2] == order and set(speakers(universe)) == {"Wren", "Moss"}
    assert len(speakers(universe)) == len(speakers(plain.json()))
    assert set(universe["audio_plan_json"]["vo"]["speakers"]) == set(order)
    assert sorted(s["speaker"] for s in universe["voice_slots"] if s["role"] == "speaker") == sorted(order)
    assert [c["name"] for c in universe["cast"]] == order
    assert set(universe) == set(plain.json()) and set(universe["audio_plan_json"]) == set(plain.json()["audio_plan_json"])
    # one character: it leads and the card's second speaker answers
    solo = _world(owner, "Solo Shore")
    _character(owner, solo, "Gull")
    res = owner.post("/api/episodes/draft-from-idea", json={**request, "world_id": solo})
    assert speakers(res.json())[:2] == ["Gull", card[1]]
