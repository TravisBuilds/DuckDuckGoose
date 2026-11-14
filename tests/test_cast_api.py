"""Cast library API (api/cast_routes.py): authenticated reference uploads with validation and the
rights attestation, private image serving, member / owner / series-owner rules, shot cast
assignment, the create wizard's cast, and character sheets behind the estimate, approval and balance
gates (dry placeholders end to end; live refusals before any spend)."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import respx
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import ActivityEnvironment

from hfvg import cast_store
from hfvg.paths import episode_dir
from tests.cast_helpers import image_bytes
from tests.ledger_helpers import set_balance

ADMIN = "cast-api-admin-secret-0123456789abcdef-xyz"
OWNER = ("pip.owner@cast.test", "owner-pass-123")
OTHER = ("moss.other@cast.test", "other-pass-123")
EP = "ep61cast"
OTHER_EP = "ep62cast"
REFUSED = "belongs to another member; only its owner or an admin can change it"
IDEA = "A paper kite and a small robot look for the lighthouse keeper's lost cat"
ESTIMATE_URL = "https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0"


def _temporal():
    client = MagicMock()
    handle = MagicMock()
    handle.describe = AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    handle.query = AsyncMock(return_value={})
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.list_workflows = MagicMock(side_effect=lambda *_a, **_k: iter(()))
    client.start_workflow = AsyncMock(return_value=MagicMock(id=EP, result_run_id="run-1"))
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
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.setenv("SNAPSHOT_SAVE_INTERVAL_S", "0")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("ELEVENLABS_DEFAULT_VOICE_ID", "CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX", "MODEL_PATH_STILL",
                "MODEL_MAX_REFS_JSON"):
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


def _base(ep: str = EP) -> str:
    return f"/api/episodes/{ep}/cast"


def _create(client, name="Pip", ep=EP, **body) -> dict:
    res = client.post(_base(ep), json={"name": name, "role": "lead",
                                       "description": f"{name}, a small round robot with one green eye", **body})
    assert res.status_code == 200, res.text
    return res.json()["character"]


def _upload(client, character, data=None, filename="pip.png", mime="image/png", version=1, ep=EP, **form):
    body = io.BytesIO(image_bytes() if data is None else data)
    return client.post(f"{_base(ep)}/{character['character_id']}/versions/{version}/images",
                       data={"rights": "original", **form}, files={"image_file": (filename, body, mime)})


def _stored_images() -> list:
    root = cast_store.cast_root() / "images"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def _sheet_jobs() -> list:
    with cast_store.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM cast_sheet_jobs ORDER BY job_id")]


# ------------------------------------------------------------------------------------- auth


async def test_cast_routes_need_a_login(studio):
    anon = _client(studio, None)
    pip = _create(_client(studio, "owner"))
    image = _upload(_client(studio, "owner"), pip).json()["image"]
    for method, url in [("GET", _base()), ("POST", _base()), ("GET", "/api/series"), ("POST", "/api/series"),
                        ("GET", image["url"]), ("GET", image["thumb_url"]),
                        ("GET", f"{_base()}/{pip['character_id']}")]:
        res = anon.request(method, url, json={"name": "Moss"} if method == "POST" else None)
        assert res.status_code == 401, (url, res.text)
    res = _upload(anon, pip, data=image_bytes(seed=7))
    assert res.status_code == 401
    assert len(_stored_images()) == 1


async def test_another_member_cannot_change_the_cast(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    pip = _create(owner)
    res = _upload(other, pip)
    assert res.status_code == 403 and res.json()["detail"] == f"Episode {EP} {REFUSED}"
    assert _stored_images() == []
    for url, body in [(_base(), {"name": "Moss"}), (f"{_base()}/{pip['character_id']}", {"name": "Zed"}),
                      (f"{_base()}/{pip['character_id']}/delete", {}),
                      (f"{_base()}/{pip['character_id']}/versions", {"label": "Coat"}),
                      (f"{_base()}/import", {"characters": [{"name": "Moss"}]})]:
        res = other.post(url, json=body)
        assert res.status_code == 403 and REFUSED in res.text, (url, res.text)
    assert [c["name"] for c in cast_store.list_characters(("episode", EP))] == ["Pip"]


async def test_members_can_read_any_cast_and_admins_can_change_it(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    pip = _create(owner)
    image = _upload(owner, pip).json()["image"]
    view = other.get(_base())
    assert view.status_code == 200 and view.json()["can_edit"] is False
    assert owner.get(_base()).json()["can_edit"] is True
    assert other.get(image["url"]).status_code == 200
    for who in ("admin", "bearer"):
        client = _client(studio, who)
        assert client.get(_base()).json()["can_edit"] is True
        assert _upload(client, pip, data=image_bytes(seed=hash(who) % 100)).status_code == 200


async def test_a_series_cast_needs_the_series_owner(studio):
    owner, other, admin = _client(studio, "owner"), _client(studio, "other"), _client(studio, "admin")
    series = owner.post("/api/series", json={"name": "Harbor Tales"}).json()["series"]
    assert series["owner_user_id"] == studio.owner_id
    pip = _create(owner)
    linked = owner.post(f"/api/episodes/{EP}/series", json={"series_id": series["series_id"]})
    assert linked.status_code == 200 and linked.json()["moved_characters"] == ["pip"]
    # another member's own episode cannot join (and so cannot change) the owner's series
    res = other.post(f"/api/episodes/{OTHER_EP}/series", json={"series_id": series["series_id"]})
    assert res.status_code == 403 and res.json()["detail"] == (
        f"Series Harbor Tales {REFUSED}")
    # an admin may put it there; the member can then read the series cast but not change it
    assert admin.post(f"/api/episodes/{OTHER_EP}/series", json={"series_id": series["series_id"]}).status_code == 200
    view = other.get(_base(OTHER_EP)).json()
    assert [c["slug"] for c in view["characters"]] == ["pip"] and view["can_edit"] is False
    res = _upload(other, pip, ep=OTHER_EP)
    assert res.status_code == 403 and "Series Harbor Tales" in res.json()["detail"]
    assert _upload(owner, pip).status_code == 200
    listed = {s["series_id"]: s for s in other.get("/api/series").json()["series"]}
    assert listed[series["series_id"]]["can_edit"] is False


# ------------------------------------------------------------------------------------- upload validation


def _gif() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (640, 960), (40, 40, 40)).save(buf, "GIF")
    return buf.getvalue()


@pytest.mark.parametrize("data,filename,mime,expected", [
    (_gif(), "pip.gif", "image/gif", "Only PNG, JPG and WebP images are accepted (this file is GIF)"),
    (_gif(), "pip.png", "image/png", "Only PNG, JPG and WebP images are accepted (this file is GIF)"),
    (b"just some notes, not a picture", "pip.png", "image/png",
     "This file is not a readable PNG, JPG or WebP image"),
    (image_bytes(size=(400, 900)), "small.png", "image/png",
     "The image is 400x900 px; each side must be at least 512 px"),
    (image_bytes()[:3000], "cut.png", "image/png", "damaged or incomplete"),
    (b"", "empty.png", "image/png", "The file is empty"),
])
async def test_bad_uploads_are_refused(studio, data, filename, mime, expected):
    owner = _client(studio, "owner")
    pip = _create(owner)
    res = _upload(owner, pip, data=data, filename=filename, mime=mime)
    assert res.status_code == 400 and expected in res.json()["detail"], res.text
    assert _stored_images() == []


async def test_an_upload_needs_the_rights_attestation(studio):
    owner = _client(studio, "owner")
    pip = _create(owner)
    for form in ({"rights": ""}, {"rights": "found online"}):
        res = _upload(owner, pip, **form)
        assert res.status_code == 400 and res.json()["detail"].startswith("Confirm the image is yours"), res.text
    res = _upload(owner, pip, depicts_real_person="true")
    assert res.status_code == 400 and "consent" in res.json()["detail"]
    assert _stored_images() == []
    res = _upload(owner, pip, rights="owned", depicts_real_person="true", likeness_consent="true",
                  note="signed release from the actor")
    assert res.status_code == 200, res.text
    attestation = res.json()["image"]["attestation"]
    assert attestation["depicts_real_person"] is True and attestation[cast_store.LIKENESS_CONSENT] is True


async def test_oversize_uploads_get_413_before_anything_is_stored(studio, monkeypatch):
    owner = _client(studio, "owner")
    pip = _create(owner)
    from PIL import Image
    noise = io.BytesIO()
    Image.frombytes("RGB", (640, 960), os.urandom(640 * 960 * 3)).save(noise, "PNG")
    data = noise.getvalue()
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) // 2))
    res = _upload(owner, pip, data=data)  # far over: refused by the declared length, body never parsed
    assert res.status_code == 413 and res.json()["detail"].startswith("That file is too large")
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data) - 10))
    res = _upload(owner, pip, data=data)  # just over: inside the form allowance, refused by the handler
    assert res.status_code == 413 and "too large" in res.json()["detail"]
    assert _stored_images() == []
    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", str(len(data)))
    assert _upload(owner, pip, data=data).status_code == 200


async def test_the_size_limit_counts_a_body_without_a_declared_length(monkeypatch):
    from api.cast_routes import FORM_OVERHEAD_BYTES, CastUploadLimit

    monkeypatch.setenv("CAST_UPLOAD_MAX_BYTES", "1000")
    called, sent = [], []

    async def app(scope, receive, send):
        called.append(await receive())

    chunks = [{"type": "http.request", "body": b"x" * 40_000, "more_body": True} for _ in range(3)]

    async def receive():
        return chunks.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "headers": [],
             "path": "/api/episodes/ep61cast/cast/ch_000000000000/versions/1/images"}
    await CastUploadLimit(app)(scope, receive, send)
    assert called == [] and sent[0]["status"] == 413 and 1000 + FORM_OVERHEAD_BYTES < 120_000
    # a small body is passed on unchanged; other routes are not touched
    chunks[:] = [{"type": "http.request", "body": b"abc", "more_body": False}]
    await CastUploadLimit(app)(scope, receive, send)
    assert called == [{"type": "http.request", "body": b"abc", "more_body": False}]


# ------------------------------------------------------------------------------------- upload, view, delete


async def test_upload_list_view_and_delete(studio):
    owner, other = _client(studio, "owner"), _client(studio, "other")
    pip = _create(owner)
    data = image_bytes("JPEG", seed=4)
    res = _upload(owner, pip, data=data, filename="pip-front.jpg", mime="image/jpeg", primary="true")
    assert res.status_code == 200, res.text
    image, duplicate = res.json()["image"], res.json()["duplicate"]
    assert duplicate is False and image["mime"] == "image/jpeg" and image["width"] == 640
    assert image["url"] == f"/api/cast/images/{image['image_id']}"
    again = _upload(owner, pip, data=data, filename="copy.jpg", mime="image/jpeg")
    assert again.json()["duplicate"] is True and again.json()["image"]["image_id"] == image["image_id"]
    view = owner.get(_base()).json()
    images = view["characters"][0]["versions"][0]["images"]
    assert [i["image_id"] for i in images] == [image["image_id"]]
    assert view["limits"]["min_image_px"] == 512 and view["attestation"]["likeness_gate"] == "likeness_consent"
    full = other.get(image["url"])
    assert full.status_code == 200 and full.content == data
    assert full.headers["cache-control"] == "private, max-age=3600"
    assert full.headers["x-content-type-options"] == "nosniff"
    thumb = other.get(image["thumb_url"])
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert other.get("/api/cast/images/ci_ffffffffffff").status_code == 404
    assert other.get("/api/cast/images/..%2F..%2Fstudio.db").status_code == 404
    audit = owner.get(f"{_base()}/{pip['character_id']}").json()["audit"]
    assert {"image_upload", "image_upload_duplicate"} <= {a["action"] for a in audit}
    res = owner.post(f"{_base()}/{pip['character_id']}/versions/1/images/{image['image_id']}/delete")
    assert res.status_code == 200, res.text
    assert other.get(image["url"]).status_code == 404 and _stored_images() == []


async def test_cast_changes_are_refused_while_a_run_is_in_progress(studio):
    owner = _client(studio, "owner")
    pip = _create(owner)
    image = _upload(owner, pip).json()["image"]
    _running(studio)
    res = owner.post(f"{_base()}/{pip['character_id']}/versions/1/images/{image['image_id']}/delete")
    assert res.status_code == 409 and "being made right now" in res.json()["detail"]
    res = owner.post(f"{_base()}/{pip['character_id']}/delete", json={})
    assert res.status_code == 409
    assert cast_store.get_image(image["image_id"])["status"] == "active"


async def test_character_text_follows_the_content_policy(studio):
    from hfvg.content_policy import hard_stop_terms
    owner = _client(studio, "owner")
    bad = next(t for t in ("gore", "nude", "blood") if hard_stop_terms(t))
    res = owner.post(_base(), json={"name": "Pip", "description": f"covered in {bad}"})
    assert res.status_code == 400 and res.json()["detail"].startswith("Please reword")


# ------------------------------------------------------------------------------------- shots


def _shots(*shots) -> dict:
    return {"episode_id": EP, "shots": [{
        "id": sid, "count": 1, "characters": [], "wet": False, "exterior": True, "water": False, "model": "kling",
        "duration": 4, "end_frame": False, "aspect_ratio": "9:16", "prompt": f"A quiet pier at dawn, shot {sid}.",
        "ref_keys": [], **extra} for sid, extra in shots]}


def _upload_shots(client, shots: dict, ep=EP):
    refs = {"episode_id": ep, "per_shot": {s["id"]: [] for s in shots["shots"]}}
    return client.post(f"/api/episodes/{ep}/upload-shots", files={
        "shots_file": ("shots.json", io.BytesIO(json.dumps(shots).encode()), "application/json"),
        "refs_file": ("refs.json", io.BytesIO(json.dumps(refs).encode()), "application/json")})


async def test_assigning_cast_to_shots(studio):
    owner = _client(studio, "owner")
    assert owner.post(f"{_base()}/shots", json={"shots": {"A01": ["pip"]}}).status_code == 404
    pip, moss = _create(owner, "Pip"), _create(owner, "Moss")
    _upload(owner, pip)
    _upload(owner, moss, data=image_bytes(seed=9))
    assert _upload_shots(owner, _shots(("A01", {}), ("A02", {}))).status_code == 200
    res = owner.post(f"{_base()}/shots", json={"shots": {"A01": ["pip", "moss"], "A02": ["pip"]},
                                               "allow_text": {"A02": True}})
    assert res.status_code == 200, res.text
    stored = json.loads((episode_dir(EP) / "shots.json").read_text())
    assert [s.get("cast") for s in stored["shots"]] == [["pip", "moss"], ["pip"]]
    assert stored["shots"][1]["allow_text"] is True
    shots = {s["shot_id"]: s for s in res.json()["shots"]["shots"]}
    assert shots["A01"]["refs"][0]["label"] == "lineup: Pip + Moss" and shots["A01"]["text_policy"] == "no_text"
    assert shots["A02"]["text_policy"] is None
    for body, detail in [({"shots": {"A09": ["pip"]}}, "Unknown shots: ['A09']"),
                         ({"shots": {"A01": ["zed"]}}, "shot A01: ['zed'] are not in this episode's cast"),
                         ({"shots": {"A01": ["Pip"]}}, "is not a character id")]:
        res = owner.post(f"{_base()}/shots", json=body)
        assert res.status_code == 400 and detail in res.json()["detail"], res.text
    res = owner.post(f"{_base()}/shots", json={"shots": {"A02": []}, "allow_text": {"A02": None}})
    stored = json.loads((episode_dir(EP) / "shots.json").read_text())
    assert "cast" not in stored["shots"][1] and "allow_text" not in stored["shots"][1]
    _running(studio)
    res = owner.post(f"{_base()}/shots", json={"shots": {"A02": ["pip"]}})
    assert res.status_code == 409


async def test_an_unknown_cast_is_reported_at_upload_and_refused_at_the_start(studio):
    owner = _client(studio, "owner")
    _create(owner, "Pip")
    res = _upload_shots(owner, _shots(("A01", {"cast": ["zed"]})))
    assert res.status_code == 200 and res.json()["cast_problems"] == [
        "shot A01: cast character 'zed' is not in the cast of this episode (add it in the Cast panel)"]
    report = owner.post(f"/api/episodes/{EP}/preflight").json()
    assert report["has_errors"] is True and report["cast_errors"]
    beatmap = owner.post("/api/episodes/draft-from-idea", json={"idea": IDEA, "target_duration": 12}).json()["beatmap"]
    res = owner.post("/api/episodes", json={"episode_id": EP, "dry_run": True, "beatmap_content": beatmap})
    assert res.status_code == 400 and "cast: shot A01: cast character 'zed'" in res.json()["detail"]
    studio.temporal_client.start_workflow.assert_not_called()
    legacy = _upload_shots(owner, _shots(("A01", {})))
    assert legacy.json()["cast_problems"] == []
    assert "cast_errors" in owner.post(f"/api/episodes/{EP}/preflight").json()


async def test_a_kit_text_policy_is_validated(studio):
    owner = _client(studio, "owner")
    for policy, status in (("sometimes", 400), ("allow_text", 200), ("no_text", 200)):
        kit = {"style": "paper cut-out", "characters": {}, "text_policy": policy}
        res = owner.post(f"/api/episodes/{EP}/upload-prompt-kit", files={
            "prompt_kit_file": ("prompt_kit.json", io.BytesIO(json.dumps(kit).encode()), "application/json")})
        assert res.status_code == status, res.text


# ------------------------------------------------------------------------------------- the create wizard


async def test_draft_from_idea_writes_a_cast(studio):
    draft = _client(studio, "owner").post("/api/episodes/draft-from-idea", json={
        "idea": IDEA, "archetype_id": None, "target_duration": 12}).json()
    cast = draft["cast"]
    assert len(cast) >= 1 and all(c["name"] and c["slug"] and c["code"] for c in cast)
    assert len({c["slug"] for c in cast}) == len(cast)
    assert draft["prompt_kit_json"]["text_policy"] == "no_text"
    codes = {c["code"] for c in cast}
    assert codes == set(draft["prompt_kit_json"]["characters"])
    assert all("cast" not in s for s in draft["shots_json"]["shots"])  # the wizard adds it after the import
    assert any(c["shots"] for c in cast)


async def test_the_wizard_creates_the_cast_and_starts(studio):
    """The create wizard's calls: draft, beatmap, kit, cast import, shots with cast, start."""
    owner = _client(studio, "owner")
    ep = "ep63wiz"
    draft = owner.post("/api/episodes/draft-from-idea", json={"idea": IDEA, "target_duration": 12}).json()
    assert owner.post("/api/episodes/upload", data={"episode_id": ep}, files={
        "beatmap_file": ("BEATMAP.md", io.BytesIO(draft["beatmap"].encode()), "text/markdown")}).status_code == 200
    kit_file = ("prompt_kit.json", io.BytesIO(json.dumps(draft["prompt_kit_json"]).encode()), "application/json")
    assert owner.post(f"/api/episodes/{ep}/upload-prompt-kit", files={"prompt_kit_file": kit_file}).status_code == 200
    entries = [{k: c[k] for k in ("code", "slug", "name", "role", "description")} for c in draft["cast"]]
    imported = owner.post(f"{_base(ep)}/import", json={"characters": entries})
    assert imported.status_code == 200, imported.text
    assert [c["status"] for c in imported.json()["characters"]] == ["created"] * len(entries)
    again = owner.post(f"{_base(ep)}/import", json={"characters": entries}).json()["characters"]
    assert [c["status"] for c in again] == ["existing"] * len(entries)  # a retried wizard reuses them
    slug_of = {c["code"]: c["slug"] for c in imported.json()["characters"]}
    shots = draft["shots_json"]
    for shot in shots["shots"]:
        cast = [slug_of[str(code)] for code in shot.get("characters") or [] if str(code) in slug_of]
        if cast:
            shot["cast"] = cast
    res = owner.post(f"/api/episodes/{ep}/upload-shots", files={
        "shots_file": ("shots.json", io.BytesIO(json.dumps(shots).encode()), "application/json"),
        "refs_file": ("refs.json", io.BytesIO(json.dumps(draft["refs_json"]).encode()), "application/json")})
    assert res.status_code == 200 and res.json()["cast_problems"] == [], res.text
    res = owner.post("/api/episodes", json={"episode_id": ep, "dry_run": True, "enable_story_shaping": True,
                                            "archetype_id": draft["archetype_id"], "idea": IDEA,
                                            "target_duration": draft["target_duration"]})
    assert res.status_code == 200, res.text
    view = owner.get(_base(ep)).json()
    assert sorted(c["slug"] for c in view["characters"]) == sorted(slug_of.values())
    assert any(s["cast"] for s in view["shots"]["shots"]) and view["shots"]["plan"]["errors"] == []
    descriptions = {c["slug"]: c["description"] for c in view["characters"]}
    assert all(descriptions[c["slug"]] == c["description"] for c in draft["cast"])


# ------------------------------------------------------------------------------------- character sheets (dry)


async def _run_jobs(studio) -> list[dict]:
    from hfvg.activities.cast_sheet import generate_cast_sheet_image_activity
    request = studio.temporal_client.start_workflow.call_args.args[1]
    env = ActivityEnvironment()
    return [await env.run(generate_cast_sheet_image_activity, job) for job in request["jobs"]]


async def test_a_dry_sheet_from_the_description_to_the_lock(studio):
    from hfvg.estimates import still_call_usd_micros
    from hfvg.workflows.cast_sheet import CastSheetWorkflow
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    estimate = owner.post(f"{url}/sheet/estimate", json={"stage": "options", "source": "text"}).json()
    assert estimate["count"] == 3 and estimate["mode"] == "text" and estimate["dry_run"] is True
    assert estimate["total_usd_micros"] == 3 * still_call_usd_micros()
    assert studio.temporal_client.start_workflow.call_count == 0 and _sheet_jobs() == []
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options", "source": "text",
                                                    "approved_total_usd_micros": estimate["total_usd_micros"]})
    assert res.status_code == 200, res.text
    call = studio.temporal_client.start_workflow.call_args
    assert call.args[0] == CastSheetWorkflow.run and call.kwargs["id"] == res.json()["workflow_id"]
    assert call.kwargs["task_queue"] == "hfvg-tasks"
    assert [j["status"] for j in _sheet_jobs()] == ["queued"] * 3
    results = await _run_jobs(studio)
    assert all(r["status"] == "completed" and r["placeholder"] for r in results)
    images = owner.get(_base()).json()["characters"][0]["versions"][0]["images"]
    options = [i for i in images if i["role"] == cast_store.OPTION_ROLE]
    assert len(options) == 3
    # pick one: the turnaround is built from it
    res = owner.post(f"{url}/sheet/pick", json={"image_id": options[1]["image_id"]})
    assert res.status_code == 200 and res.json()["sheet_stage"] == "master_picked"
    estimate = owner.post(f"{url}/sheet/estimate", json={"stage": "turnaround"}).json()
    assert estimate["views"] == ["turnaround_front", "turnaround_three_quarter", "turnaround_side",
                                 "turnaround_back", "head_sheet"]
    res = owner.post(f"{url}/sheet/generate", json={"stage": "turnaround",
                                                    "approved_total_usd_micros": estimate["total_usd_micros"]})
    assert res.status_code == 200, res.text
    turnaround_jobs = studio.temporal_client.start_workflow.call_args.args[1]["jobs"]
    assert all(j["refs"] and j["ref_shas"] == [cast_store.get_image(options[1]["image_id"])["sha256"]]
               for j in turnaround_jobs)
    await _run_jobs(studio)
    # placeholders lock only when asked for, and only in dry-run mode
    res = owner.post(f"{url}/approve", json={})
    assert res.status_code == 409 and "placeholder" in res.json()["detail"]
    res = owner.post(f"{url}/approve", json={"allow_placeholder": True})
    assert res.status_code == 200, res.text
    version = res.json()["version"]
    assert version["locked"] is True and len(version["lock_hash"]) == 64
    # locked: nothing in the version changes any more
    assert _upload(owner, pip, data=image_bytes(seed=21)).status_code == 409
    master = next(i for i in res.json()["character"]["versions"][0]["images"] if i["role"] == "master")
    assert owner.post(f"{url}/images/{master['image_id']}/delete").status_code == 409
    for stage in ("options", "turnaround"):
        res = owner.post(f"{url}/sheet/estimate", json={"stage": stage})
        assert res.status_code == 409 and "locked" in res.json()["detail"]
    assert owner.post(f"{url}/sheet/reject", json={"stage": "all"}).status_code == 409
    assert owner.post(url, json={"outfit": "a new hat"}).status_code == 409
    # a new outfit is a new version made from the locked master
    res = owner.post(f"{_base()}/{pip['character_id']}/versions", json={"label": "Raincoat",
                                                                         "outfit": "a yellow raincoat",
                                                                         "derive_from_version": 1})
    assert res.status_code == 200 and res.json()["version"]["derived_from_version"] == 1
    estimate = owner.post(f"{_base()}/{pip['character_id']}/versions/2/sheet/estimate",
                          json={"stage": "options"}).json()
    assert estimate["mode"] == "outfit"
    # deleting a character with a locked sheet: only an admin, with force and a reason
    res = owner.post(f"{_base()}/{pip['character_id']}/delete", json={})
    assert res.status_code == 409
    res = owner.post(f"{_base()}/{pip['character_id']}/delete", json={"force": True, "reason": "redo"})
    assert res.status_code == 403
    res = _client(studio, "admin").post(f"{_base()}/{pip['character_id']}/delete",
                                        json={"force": True, "reason": "duplicate character"})
    assert res.status_code == 200, res.text


async def test_a_sheet_from_an_uploaded_picture(studio):
    owner = _client(studio, "owner")
    pip = _create(owner)
    upload = _upload(owner, pip).json()["image"]
    url = f"{_base()}/{pip['character_id']}/versions/1"
    estimate = owner.post(f"{url}/sheet/estimate", json={"stage": "options"}).json()
    assert estimate["mode"] == "upload" and estimate["count"] == 2
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options",
                                                    "approved_total_usd_micros": estimate["total_usd_micros"]})
    jobs = studio.temporal_client.start_workflow.call_args.args[1]["jobs"]
    assert res.status_code == 200 and all(j["ref_shas"] == [upload["sha256"]] for j in jobs)
    res = owner.post(f"{url}/sheet/reject", json={"stage": "options"})
    assert res.status_code == 200


async def test_a_sheet_is_refused_when_the_estimate_is_above_the_approval(studio):
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    total = owner.post(f"{url}/sheet/estimate", json={"stage": "options", "count": 4}).json()["total_usd_micros"]
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options", "count": 4,
                                                    "approved_total_usd_micros": total - 1})
    assert res.status_code == 409 and res.json()["detail"].startswith("The estimate is now")
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options", "count": 4})
    assert res.status_code == 422  # no approval, no generation
    studio.temporal_client.start_workflow.assert_not_called()
    assert _sheet_jobs() == []


async def test_a_second_sheet_request_waits_for_the_first(studio):
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    total = owner.post(f"{url}/sheet/estimate", json={"stage": "options"}).json()["total_usd_micros"]
    body = {"stage": "options", "approved_total_usd_micros": total}
    assert owner.post(f"{url}/sheet/generate", json=body).status_code == 200
    _running(studio)
    res = owner.post(f"{url}/sheet/generate", json=body)
    assert res.status_code == 409 and "being made" in res.json()["detail"]
    assert owner.post(f"{url}/approve", json={"allow_placeholder": True}).status_code == 409
    # the run ended without making them (worker gone): the jobs are released for a new try
    studio.temporal_client.handle.describe = AsyncMock(side_effect=RPCError("gone", RPCStatusCode.NOT_FOUND, b""))
    assert owner.post(f"{url}/sheet/generate", json=body).status_code == 200
    assert sorted(j["status"] for j in _sheet_jobs()) == ["failed"] * 3 + ["queued"] * 3


# ------------------------------------------------------------------------------------- character sheets (live gates)


async def _live(studio, monkeypatch, *, live_mode=True):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    with sqlite3.connect(os.environ["DATABASE_PATH"]) as conn:
        conn.execute("UPDATE episodes SET live_mode = ? WHERE episode_id = ?", (1 if live_mode else 0, EP))


@respx.mock
async def test_live_sheets_need_a_live_episode(studio, monkeypatch):
    await _live(studio, monkeypatch, live_mode=False)
    est = respx.post(ESTIMATE_URL).mock(return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"}))
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    for route, body in (("estimate", {"stage": "options"}),
                        ("generate", {"stage": "options", "approved_total_usd_micros": 10_000_000})):
        res = owner.post(f"{url}/sheet/{route}", json=body)
        assert res.status_code == 409 and "not in live mode" in res.json()["detail"]
    assert est.call_count == 0
    studio.temporal_client.start_workflow.assert_not_called()


@respx.mock
async def test_live_sheet_estimates_come_from_the_provider_and_fail_closed(studio, monkeypatch):
    await _live(studio, monkeypatch)
    await set_balance(os.environ["DATABASE_PATH"], usd="100.00", episode_id=EP)
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    route = respx.post(ESTIMATE_URL).mock(return_value=httpx.Response(500, json={"error": "down"}))
    for path, body in (("estimate", {"stage": "options"}),
                       ("generate", {"stage": "options", "approved_total_usd_micros": 10_000_000})):
        res = owner.post(f"{url}/sheet/{path}", json=body)
        assert res.status_code == 503 and "did not give a price" in res.json()["detail"], res.text
    assert route.call_count >= 2
    studio.temporal_client.start_workflow.assert_not_called()
    assert _sheet_jobs() == []
    route.mock(return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"}))
    estimate = owner.post(f"{url}/sheet/estimate", json={"stage": "options"}).json()
    assert estimate["dry_run"] is False and estimate["per_image_usd_micros"] == [390_000] * 3
    assert estimate["total_usd_micros"] == 1_170_000 and "$1.17" in estimate["summary"]
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options", "approved_total_usd_micros": 1_200_000})
    assert res.status_code == 200, res.text
    jobs = studio.temporal_client.start_workflow.call_args.args[1]["jobs"]
    # each image may cost its estimate plus its share of the approved headroom, never more in total
    assert [j["approved_usd_micros"] for j in jobs] == [400_000] * 3
    assert sum(j["approved_usd_micros"] for j in jobs) <= 1_200_000


@respx.mock
async def test_live_sheets_check_the_balance_before_starting(studio, monkeypatch):
    await _live(studio, monkeypatch)
    await set_balance(os.environ["DATABASE_PATH"], usd="0.10", episode_id=EP)
    respx.post(ESTIMATE_URL).mock(return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"}))
    owner = _client(studio, "owner")
    pip = _create(owner)
    url = f"{_base()}/{pip['character_id']}/versions/1"
    res = owner.post(f"{url}/sheet/generate", json={"stage": "options", "approved_total_usd_micros": 2_000_000})
    assert res.status_code == 409 and res.json()["detail"].startswith("Balance check failed"), res.text
    studio.temporal_client.start_workflow.assert_not_called()
    assert _sheet_jobs() == []


async def test_placeholders_cannot_be_locked_in_live_mode(studio, monkeypatch):
    from tests.cast_helpers import complete_sheet
    owner = _client(studio, "owner")
    pip = _create(owner)
    complete_sheet(cast_store.get_character(pip["character_id"]), placeholder=True)
    monkeypatch.setenv("DRY_RUN", "false")
    res = owner.post(f"{_base()}/{pip['character_id']}/versions/1/approve", json={"allow_placeholder": True})
    assert res.status_code == 409 and "only in dry-run mode" in res.json()["detail"]
    assert cast_store.get_version(pip["character_id"], 1)["locked"] is False
