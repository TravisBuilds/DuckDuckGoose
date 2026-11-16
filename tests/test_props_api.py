"""Props and places API (api/prop_routes.py): the cast library's rules for a second library: logins,
the episode owner and series owner checks, the 404 for whoever may not see it, photo validation and
the rights attestation, the upload size limit before the form is parsed, private photo serving and
deletes refused while a run uses the library."""

from __future__ import annotations

import io
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from hfvg import cast_store, prop_store
from tests.cast_helpers import image_bytes

ADMIN = "props-api-admin-secret-0123456789abcdef-xyz"
OWNER = ("pip.owner@props.test", "owner-pass-123")
OTHER = ("moss.other@props.test", "other-pass-123")
EP = "ep71prop"
OTHER_EP = "ep72prop"
SHARED_EP = "ep74prop"
REFUSED = "belongs to another member; only its owner or an admin can change it"
HIDDEN = "Only the people working on this episode (and admins) can see its props and places"


def _temporal():
    client = MagicMock()
    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    handle.query = AsyncMock(return_value={})
    handle.execute_update = AsyncMock(return_value={})
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.list_workflows = MagicMock(side_effect=lambda *_a, **_k: iter(()))
    client.handle = handle
    return client


def _running(studio):
    studio.temporal_client.handle.describe = AsyncMock(
        return_value=MagicMock(status=WorkflowExecutionStatus.RUNNING, run_id="run-open"))


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
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX"):
        monkeypatch.delenv(var, raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    users = UserManager(db)
    await users.init_db()
    owner = await users.create_user(name="Owner", email=OWNER[0], role="member", password=OWNER[1])
    other = await users.create_user(name="Other", email=OTHER[0], role="member", password=OTHER[1])
    await create_episode(db, EP, user_id=owner["user_id"])
    await create_episode(db, OTHER_EP, user_id=other["user_id"])
    sys.modules.pop("api.main", None)
    import api.main as api_main
    api_main.temporal_client = _temporal()
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


def _base(ep: str = EP) -> str:
    return f"/api/episodes/{ep}/props"


def _add(client, name="Red umbrella", kind="prop", data=None, filename="umbrella.png", mime="image/png", ep=EP,
         **form):
    fields = {"kind": kind, "name": name, "description": f"{name.lower()}, as in the photo", "rights": "original",
              **form}
    body = io.BytesIO(image_bytes() if data is None else data)
    return client.post(f"{_base(ep)}/with-photo", data=fields, files={"image_file": (filename, body, mime)})


def _added(client, name="Red umbrella", kind="prop", ep=EP, **kw) -> dict:
    res = _add(client, name, kind, ep=ep, **kw)
    assert res.status_code == 200, res.text
    return res.json()["item"]


def _photo(client, item, data=None, filename="more.png", mime="image/png", ep=EP, **form):
    body = io.BytesIO(image_bytes(seed=5) if data is None else data)
    return client.post(f"{_base(ep)}/{item['prop_id']}/photos", data={"rights": "original", **form},
                       files={"image_file": (filename, body, mime)})


def _stored() -> list:
    root = cast_store.cast_root() / "images"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _rows() -> tuple[int, int]:
    with cast_store.connect() as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("prop_items", "prop_images"))


# ------------------------------------------------------------------------------------- who may do what


async def test_props_routes_need_a_login(studio):
    anon = _client(studio, None)
    item = _added(_client(studio, "owner"))
    photo = item["photos"][0]
    for method, url in [("GET", _base()), ("POST", _base()), ("POST", f"{_base()}/{item['prop_id']}"),
                        ("POST", f"{_base()}/{item['prop_id']}/delete"), ("GET", photo["url"]),
                        ("GET", photo["thumb_url"])]:
        res = anon.request(method, url, json={"kind": "prop", "name": "Oar"} if method == "POST" else None)
        assert res.status_code == 401, (url, res.text)
    assert _add(anon, "Oar", data=image_bytes(seed=7)).status_code == 401
    assert _photo(anon, item, data=image_bytes(seed=8)).status_code == 401
    assert len(_stored()) == 1 and _rows() == (1, 1)


async def test_another_member_can_neither_change_nor_see_the_props(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    item = _added(owner)
    photo = item["photos"][0]
    res = _add(other, "Oar", data=image_bytes(seed=2))
    assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}"
    res = _photo(other, item)
    assert res.status_code == 403 and REFUSED in res.text
    pid = item["prop_id"]
    for url, body in [(_base(), {"kind": "prop", "name": "Oar"}), (f"{_base()}/{pid}", {"name": "Blue umbrella"}),
                      (f"{_base()}/{pid}/delete", {}), (f"{_base()}/{pid}/photos/{photo['image_id']}/primary", {}),
                      (f"{_base()}/{pid}/photos/{photo['image_id']}/delete", {})]:
        res = other.post(url, json=body)
        assert res.status_code == 403 and REFUSED in res.text, (url, res.text)
    assert _rows() == (1, 1) and prop_store.get_item(pid)["name"] == "Red umbrella"
    res = other.get(_base())
    assert res.status_code == 404 and res.json() == {"detail": HIDDEN}
    for url in (photo["url"], photo["thumb_url"]):
        res = other.get(url)
        assert res.status_code == 404 and res.json() == {"detail": f"Image {photo['image_id']} not found"}, url
    assert other.get("/api/props/photos/pi_ffffffffffff").json() == {"detail": "Image pi_ffffffffffff not found"}
    assert other.get("/api/props/photos/..%2F..%2Fstudio.db").status_code == 404
    assert owner.get(_base()).json()["can_edit"] is True


async def test_admins_see_and_change_every_library(studio):
    item = _added(_client(studio, "owner"))
    for seed, who in enumerate(("admin", "bearer"), start=1):
        client = _client(studio, who)
        view = client.get(_base())
        assert view.status_code == 200 and view.json()["can_edit"] is True
        assert client.get(item["photos"][0]["url"]).status_code == 200
        assert _photo(client, item, data=image_bytes(seed=seed)).status_code == 200
    assert len(prop_store.get_item(item["prop_id"])["photos"]) == 3


async def test_a_series_library_needs_the_series_owner(studio):
    owner, other, admin = _client(studio, "owner"), _client(studio, "other"), _client(studio, "admin")
    sid = owner.post("/api/series", json={"name": "Harbour tales"}).json()["series"]["series_id"]
    item = _added(owner)
    linked = owner.post(f"/api/episodes/{EP}/series", json={"series_id": sid})
    assert linked.status_code == 200 and linked.json()["moved_props"] == ["red-umbrella"]
    assert admin.post(f"/api/episodes/{OTHER_EP}/series", json={"series_id": sid}).status_code == 200
    view = other.get(_base(OTHER_EP)).json()  # the owner of an episode of the series sees its library
    assert [i["slug"] for i in view["items"]] == ["red-umbrella"] and view["can_edit"] is False
    assert view["library"] == {"kind": "series", "id": sid}
    assert other.get(item["photos"][0]["url"]).status_code == 200
    res = _add(other, "Oar", ep=OTHER_EP, data=image_bytes(seed=3))
    assert res.status_code == 403 and res.json()["detail"] == f"Series Harbour tales {REFUSED}"
    res = other.post(f"{_base(OTHER_EP)}/{item['prop_id']}", json={"name": "Blue umbrella"})
    assert res.status_code == 403 and "Series Harbour tales" in res.json()["detail"]
    assert _photo(owner, item).status_code == 200


async def test_a_series_without_an_owner_changes_only_through_an_admin(studio):
    owner, admin = _client(studio, "owner"), _client(studio, "admin")
    sid = _client(studio, "bearer").post("/api/series", json={"name": "Old harbour"}).json()["series"]["series_id"]
    assert admin.post(f"/api/episodes/{EP}/series", json={"series_id": sid}).status_code == 200
    res = _add(owner)
    assert res.status_code == 403 and res.json()["detail"] == "Series Old harbour has no owner; only an admin can change it"
    assert _rows() == (0, 0) and _stored() == []
    assert owner.get(_base()).json()["can_edit"] is False
    assert _add(admin).status_code == 200


async def test_an_episode_without_an_owner_shares_its_props_with_every_member(studio):
    from hfvg.studio_db import create_episode
    await create_episode(os.environ["DATABASE_PATH"], SHARED_EP)
    owner, other = _client(studio, "owner"), _client(studio, "other")
    item = _added(other, "Harbour cabin", "place", ep=SHARED_EP)
    view = owner.get(_base(SHARED_EP))
    assert view.status_code == 200 and view.json()["can_edit"] is True
    assert owner.get(item["photos"][0]["thumb_url"]).status_code == 200
    assert _photo(owner, item, ep=SHARED_EP).status_code == 200


async def test_an_item_of_another_library_is_not_found(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    theirs = _added(other, ep=OTHER_EP)
    for url in (f"{_base()}/{theirs['prop_id']}", f"{_base()}/{theirs['prop_id']}/delete",
                f"{_base()}/{theirs['prop_id']}/photos/{theirs['photos'][0]['image_id']}/primary",
                f"{_base()}/pr_000000000000", f"{_base()}/not-an-id"):
        res = owner.post(url, json={"name": "Mine now"})
        assert res.status_code == 404, (url, res.text)
    assert _photo(owner, theirs).status_code == 404
    assert prop_store.get_item(theirs["prop_id"])["name"] == "Red umbrella"
    mine = _added(owner, "Oar", data=image_bytes(seed=6))
    res = owner.post(f"{_base()}/{mine['prop_id']}/photos/{theirs['photos'][0]['image_id']}/delete")
    assert res.status_code == 404 and prop_store.get_photo(theirs["photos"][0]["image_id"])["status"] == "active"


# ------------------------------------------------------------------------------------- photos


async def test_a_prop_joins_with_its_photo_in_one_step(studio):
    owner = _client(studio, "owner")
    view = owner.get(_base()).json()
    assert view["items"] == [] and view["limits"]["max_photos_per_item"] == 8
    assert view["limits"]["max_props_per_shot"] == 4 and view["attestation"]["rights"] == ["original", "owned"]
    data = image_bytes("JPEG", seed=12)
    res = _add(owner, "Harbour cabin", "place", data=data, filename="cabin.jpg", mime="image/jpeg", note="my photo")
    assert res.status_code == 200, res.text
    item = res.json()["item"]
    assert (item["kind"], item["slug"], item["description"]) == ("place", "harbour-cabin", "harbour cabin, as in the photo")
    [photo] = item["photos"]
    assert photo["is_primary"] is True and photo["mime"] == "image/jpeg" and photo["attestation"]["note"] == "my photo"
    full = owner.get(photo["url"])
    assert full.status_code == 200 and full.content == data
    assert full.headers["cache-control"] == "private, max-age=3600" and full.headers["x-content-type-options"] == "nosniff"
    thumb = owner.get(photo["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert [i["prop_id"] for i in owner.get(_base()).json()["items"]] == [item["prop_id"]]
    plain = owner.post(_base(), json={"kind": "prop", "name": "Oar", "description": "a long wooden oar"})
    assert plain.status_code == 200 and plain.json()["item"]["photos"] == []


def _gif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (640, 960), (40, 40, 40)).save(buf, "GIF")
    return buf.getvalue()


@pytest.mark.parametrize("data,filename,mime,expected", [
    (_gif(), "cabin.gif", "image/gif", "Only PNG, JPG and WebP images are accepted (this file is GIF)"),
    (_gif(), "cabin.png", "image/png", "Only PNG, JPG and WebP images are accepted (this file is GIF)"),
    (b"just some notes, not a picture", "cabin.png", "image/png", "This file is not a readable PNG, JPG or WebP image"),
    (image_bytes(size=(400, 900)), "small.png", "image/png", "The image is 400x900 px; each side must be at least 512 px"),
    (image_bytes()[:3000], "cut.png", "image/png", "damaged or incomplete"),
    (b"", "empty.png", "image/png", "The file is empty"),
])
async def test_bad_photos_are_refused_and_nothing_is_kept(studio, data, filename, mime, expected):
    owner = _client(studio, "owner")
    res = _add(owner, data=data, filename=filename, mime=mime)
    assert res.status_code == 400 and expected in res.json()["detail"], res.text
    assert _rows() == (0, 0) and _stored() == []
    item = owner.post(_base(), json={"kind": "prop", "name": "Oar"}).json()["item"]
    res = _photo(owner, item, data=data, filename=filename, mime=mime)
    assert res.status_code == 400 and expected in res.json()["detail"], res.text
    assert _rows() == (1, 0) and _stored() == []


async def test_a_photo_needs_the_rights_attestation(studio):
    owner = _client(studio, "owner")
    for form in ({"rights": ""}, {"rights": "found online"}):
        res = _add(owner, **form)
        assert res.status_code == 400 and res.json()["detail"].startswith("Confirm the image is yours"), res.text
    res = _add(owner, depicts_real_person="true")
    assert res.status_code == 400 and "consent" in res.json()["detail"]
    assert _rows() == (0, 0) and _stored() == []
    res = _add(owner, rights="owned", depicts_real_person="true", likeness_consent="true", note="signed release")
    assert res.status_code == 200, res.text
    attestation = res.json()["item"]["photos"][0]["attestation"]
    assert attestation["depicts_real_person"] is True and attestation[cast_store.LIKENESS_CONSENT] is True


async def test_oversize_photos_get_413_before_anything_is_stored(studio, monkeypatch):
    from PIL import Image
    owner = _client(studio, "owner")
    item = owner.post(_base(), json={"kind": "place", "name": "Harbour cabin"}).json()["item"]
    noise = io.BytesIO()
    Image.frombytes("RGB", (640, 960), os.urandom(640 * 960 * 3)).save(noise, "PNG")
    data = noise.getvalue()
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) // 2))
    for res in (_add(owner, "Oar", data=data), _photo(owner, item, data=data)):  # refused by the declared length
        assert res.status_code == 413 and res.json()["detail"].startswith("That file is too large"), res.text
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) - 10))
    for res in (_add(owner, "Oar", data=data), _photo(owner, item, data=data)):  # refused by the handler
        assert res.status_code == 413 and "too large" in res.json()["detail"], res.text
    assert _rows() == (1, 0) and _stored() == []
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data)))
    assert _photo(owner, item, data=data).status_code == 200


@pytest.mark.parametrize("path", ["/api/episodes/ep71prop/props/pr_000000000000/photos",
                                  "/api/episodes/ep71prop/props/with-photo"])
async def test_the_size_limit_counts_a_prop_photo_body_without_a_declared_length(monkeypatch, path):
    from api.cast_routes import CastUploadLimit

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
    assert called == [] and sent[0]["status"] == 413
    sent.clear()
    chunks[:] = [{"type": "http.request", "body": b"x" * 40_000, "more_body": False}]
    other = {"type": "http", "method": "POST", "headers": [], "path": "/api/episodes/ep71prop/props"}
    await CastUploadLimit(app)(other, receive, send)  # a JSON route is not counted
    assert sent == [] and len(called) == 1


async def test_text_follows_the_content_policy_and_the_kind_is_checked(studio):
    from hfvg.content_policy import hard_stop_terms
    owner = _client(studio, "owner")
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))
    res = owner.post(_base(), json={"kind": "prop", "name": "Oar", "description": f"covered in {bad}"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword")
    res = _add(owner, "Oar", description=f"covered in {bad}")
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword")
    res = owner.post(_base(), json={"kind": "thing", "name": "Oar"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Choose what this is")
    assert _rows() == (0, 0)


async def test_edit_main_photo_and_deletes(studio):
    owner = _client(studio, "owner")
    item = _added(owner)
    second = _photo(owner, item, data=image_bytes(seed=9)).json()
    assert second["duplicate"] is False and second["photo"]["is_primary"] is False
    again = _photo(owner, item, data=image_bytes(seed=9)).json()
    assert again["duplicate"] is True and again["photo"]["image_id"] == second["photo"]["image_id"]
    pid = item["prop_id"]
    res = owner.post(f"{_base()}/{pid}", json={"description": "a red umbrella with a curved wooden handle"})
    assert res.status_code == 200 and res.json()["item"]["kind"] == "prop"
    res = owner.post(f"{_base()}/{pid}/photos/{second['photo']['image_id']}/primary")
    assert res.status_code == 200 and res.json()["photo"]["is_primary"] is True
    first = item["photos"][0]
    res = owner.post(f"{_base()}/{pid}/photos/{first['image_id']}/delete")
    assert res.status_code == 200 and res.json() == {"deleted": first["image_id"], "file_removed": True}
    assert owner.get(first["url"]).status_code == 404
    res = owner.post(f"{_base()}/{pid}/delete")
    assert res.status_code == 200 and res.json() == {"deleted": pid, "photos_removed": 1}
    assert _rows() == (0, 0) and _stored() == []
    with cast_store.connect() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM cast_audit_log WHERE action LIKE 'prop%' ORDER BY id")]
    assert actions == ["prop_create", "prop_photo_upload", "prop_photo_upload", "prop_photo_upload_duplicate",
                       "prop_update", "prop_photo_primary", "prop_photo_delete", "prop_delete"]


async def test_deletes_are_refused_while_a_run_is_in_progress(studio):
    owner = _client(studio, "owner")
    item = _added(owner)
    photo = item["photos"][0]
    _running(studio)
    res = owner.post(f"{_base()}/{item['prop_id']}/photos/{photo['image_id']}/delete")
    assert res.status_code == 409 and "being made right now" in res.json()["detail"]
    res = owner.post(f"{_base()}/{item['prop_id']}/delete")
    assert res.status_code == 409 and "props and places can be deleted" in res.json()["detail"]
    assert prop_store.get_photo(photo["image_id"])["status"] == "active" and _rows() == (1, 1)
    # adding does not change what the run planned: allowed
    assert _photo(owner, item, data=image_bytes(seed=11)).status_code == 200
    assert _add(owner, "Oar", data=image_bytes(seed=12)).status_code == 200
