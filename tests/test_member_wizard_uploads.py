"""A member completes the create page through the API: draft, every upload, start.

The wizard uploads BEATMAP.md, shots.json + refs.json, prompt_kit.json and the audio plan
(data/episodes/<id>/audio/audio_plan.json) with the member's session cookie; the routes check it
with verify_auth_unified(require_admin=False). Then POST /api/episodes starts the workflow with the
card's brief as its 9th input.
"""

import io
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.service import RPCError, RPCStatusCode

from hfvg import audio_mix
from hfvg.genre_presets import resolve_profile

ADMIN_SECRET = "member-wizard-admin-secret-0123456789abcdef"
MEMBER = ("member@wizard.test", "member-pass-123")
IDEA = "A lighthouse keeper befriends a lost paper kite on a windy evening by the harbor"


@pytest.fixture
async def setup_db(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager

    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    users = UserManager(db)
    await users.init_db()
    await users.create_user(name="Member", email=MEMBER[0], role="member", password=MEMBER[1])
    sys.modules.pop("api.main", None)
    import api.main as api_main

    temporal = MagicMock()
    temporal.get_workflow_handle.return_value.describe = AsyncMock(
        side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    handle = MagicMock(id="wizard-run", result_run_id="run-1")
    temporal.start_workflow = AsyncMock(return_value=handle)
    api_main.temporal_client = temporal
    return api_main


def _client(api_main, role: str):
    from fastapi.testclient import TestClient

    client = TestClient(api_main.app)
    if role == "none":
        return client
    body = {"email": MEMBER[0], "password": MEMBER[1]} if role == "member" else {"admin_secret": ADMIN_SECRET}
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _json_file(name: str, data) -> tuple:
    return (name, io.BytesIO(json.dumps(data).encode()), "application/json")


def _draft(client, archetype_id: str | None):
    profile = resolve_profile(archetype_id)
    res = client.post("/api/episodes/draft-from-idea", json={
        "idea": IDEA, "archetype_id": archetype_id, "target_duration": profile["default_length_s"]})
    assert res.status_code == 200, res.text
    return res.json()


def _upload_all(client, draft: dict, audio_plan: dict) -> dict:
    ep = draft["episode_id"]
    results = {
        "beatmap": client.post("/api/episodes/upload", data={"episode_id": ep}, files={
            "beatmap_file": ("BEATMAP.md", io.BytesIO(draft["beatmap"].encode()), "text/markdown")}),
        "shots": client.post(f"/api/episodes/{ep}/upload-shots", files={
            "shots_file": _json_file("shots.json", draft["shots_json"]),
            "refs_file": _json_file("refs.json", draft["refs_json"])}),
        "prompt_kit": client.post(f"/api/episodes/{ep}/upload-prompt-kit", files={
            "prompt_kit_file": _json_file("prompt_kit.json", draft["prompt_kit_json"])}),
        "audio_plan": client.post(f"/api/episodes/{ep}/upload-audio-plan", files={
            "audio_plan_file": _json_file("audio_plan.json", audio_plan)}),
    }
    return {k: (r.status_code, r.json()) for k, r in results.items()}


def _with_voices(draft: dict) -> dict:
    plan = json.loads(json.dumps(draft["audio_plan_json"]))
    for slot in draft["voice_slots"]:
        voice = "dryrun-voice-" + slot["key"].split(":")[-1].lower()
        if slot["speaker"]:
            plan["vo"]["speakers"][slot["speaker"]] = voice
        else:
            plan["vo"]["voice_id"] = voice
    return plan


async def test_member_can_upload_shots(setup_db):
    client = _client(setup_db, "member")
    draft = _draft(client, None)
    status = _upload_all(client, draft, draft["audio_plan_json"])
    assert status["shots"][0] == 200, status["shots"]
    assert status["shots"][1]["shots_count"] == len(draft["shots_json"]["shots"])
    assert status["shots"][1]["refs_uploaded"] is True
    ep_dir = audio_mix.episode_dir(draft["episode_id"])
    assert json.loads((ep_dir / "shots.json").read_text()) == draft["shots_json"]
    assert json.loads((ep_dir / "refs.json").read_text()) == draft["refs_json"]


async def test_member_can_upload_prompt_kit(setup_db):
    client = _client(setup_db, "member")
    draft = _draft(client, None)
    status = _upload_all(client, draft, draft["audio_plan_json"])
    assert status["prompt_kit"][0] == 200, status["prompt_kit"]
    stored = audio_mix.episode_dir(draft["episode_id"]) / "prompt_kit.json"
    assert json.loads(stored.read_text()) == draft["prompt_kit_json"]


async def test_member_can_upload_audio_plan(setup_db):
    client = _client(setup_db, "member")
    card = next(pid for pid in _cards() if resolve_profile(pid)["vo_mode"] == "dialogue")
    draft = _draft(client, card)
    plan = _with_voices(draft)
    status = _upload_all(client, draft, plan)
    assert status["audio_plan"][0] == 200, status["audio_plan"]
    path = audio_mix.audio_plan_path(draft["episode_id"])
    assert status["audio_plan"][1]["audio_plan_path"] == str(path)
    assert status["audio_plan"][1]["lines_missing_voice"] == []
    assert json.loads(path.read_text()) == plan
    loaded = audio_mix.load_plan_file(path)
    assert loaded["vo"]["speakers"] == plan["vo"]["speakers"]
    assert all(ln["voice_id"] == plan["vo"]["speakers"][ln["speaker"]] for ln in loaded["vo"]["lines"])


async def test_unauthenticated_cannot_upload(setup_db):
    member = _client(setup_db, "member")
    draft = _draft(member, None)
    status = _upload_all(_client(setup_db, "none"), draft, draft["audio_plan_json"])
    assert {k: code for k, (code, _) in status.items()} == {
        "beatmap": 401, "shots": 401, "prompt_kit": 401, "audio_plan": 401}
    assert not audio_mix.episode_dir(draft["episode_id"]).exists()


async def test_admin_bearer_secret_can_upload_the_audio_plan(setup_db):
    client = _client(setup_db, "none")
    res = client.post("/api/episodes/ep42bearer/upload-audio-plan",
                      headers={"Authorization": f"Bearer {ADMIN_SECRET}"},
                      files={"audio_plan_file": _json_file("audio_plan.json", {"music": {"prompt": "calm piano"}})})
    assert res.status_code == 200, res.text
    assert audio_mix.audio_plan_path("ep42bearer").is_file()


@pytest.mark.parametrize("content,detail", [
    (b"{not json", "Invalid JSON"),
    (json.dumps({"vo": {"speakers": {"Host": "v1"}, "lines": [
        {"id": "L01", "at": 0.3, "text": "Hi", "speaker": "Guest"}]}}).encode(), "not listed in vo.speakers"),
    (json.dumps({"vo": {"lines": [{"id": "L01", "at": "soon", "text": "Hi"}]}}).encode(), "Invalid audio plan"),
    (json.dumps(["not", "a", "plan"]).encode(), "Invalid audio plan"),
])
async def test_invalid_audio_plan_is_refused_and_not_stored(setup_db, content, detail):
    client = _client(setup_db, "member")
    res = client.post("/api/episodes/ep42bad/upload-audio-plan",
                      files={"audio_plan_file": ("audio_plan.json", io.BytesIO(content), "application/json")})
    assert res.status_code == 400
    assert detail in res.json()["detail"]
    assert not audio_mix.audio_plan_path("ep42bad").exists()


async def test_audio_plan_upload_reports_lines_still_missing_a_voice(setup_db):
    client = _client(setup_db, "member")
    card = next(pid for pid in _cards() if resolve_profile(pid)["vo_mode"] == "narration")
    draft = _draft(client, card)
    status = _upload_all(client, draft, draft["audio_plan_json"])
    assert status["audio_plan"][0] == 200
    assert status["audio_plan"][1]["lines_missing_voice"] == [
        ln["id"] for ln in draft["audio_plan_json"]["vo"]["lines"]]


@pytest.mark.parametrize("vo_mode", ["none", "caption_only", "narration", "dialogue"])
async def test_member_completes_the_create_page(setup_db, vo_mode):
    client = _client(setup_db, "member")
    card = next(pid for pid in _cards() if resolve_profile(pid)["vo_mode"] == vo_mode)
    draft = _draft(client, card)
    status = _upload_all(client, draft, _with_voices(draft))
    assert {k: code for k, (code, _) in status.items()} == {
        "beatmap": 200, "shots": 200, "prompt_kit": 200, "audio_plan": 200}, status
    start = client.post("/api/episodes", json={
        "episode_id": draft["episode_id"], "dry_run": True, "enable_story_shaping": True,
        "archetype_id": card, "idea": IDEA, "target_duration": draft["target_duration"]})
    assert start.status_code == 200, start.text
    args = setup_db.temporal_client.start_workflow.call_args.kwargs["args"]
    assert len(args) == 9
    assert args[0] == draft["episode_id"] and args[2] is True and args[7] is True
    assert args[8] == {"idea": IDEA, "archetype_id": card, "target_duration_s": draft["target_duration"],
                       "preset": resolve_profile(card)}


def _cards() -> list[str]:
    import yaml
    from hfvg.genre_presets import REGISTRY_PATH

    return [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]
