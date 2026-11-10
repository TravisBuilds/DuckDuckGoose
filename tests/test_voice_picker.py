"""Voice picker (create wizard): describe a voice in plain words, the studio picks an ElevenLabs voice.

Covers the ranking and "Try another voice" exclusion (hfvg.voice_picker), the voice listing
(ElevenLabsProvider.list_voices) and POST /api/voices/pick: dry-run placeholder voices without any
network call, live listing with the LLM or the label match, and auth (none / member / admin).
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from hfvg import voice_picker as vp

ADMIN_SECRET = "voice-picker-admin-secret-0123456789abcdef"
MEMBER = ("member@voices.test", "member-pass-123")

LIVE_VOICES = {
    "voices": [
        {"voice_id": "acct-river", "name": "River", "category": "cloned",
         "labels": {"gender": "female", "age": "young", "descriptive": "bright"},
         "description": "Bright young voice", "preview_url": "https://previews.test/acct-river.mp3"},
        {"voice_id": "pre-oak", "name": "Oak", "category": "premade",
         "labels": {"gender": "male", "age": "old", "descriptive": "deep", "use_case": "narrative_story"},
         "description": "Deep, slow storyteller", "preview_url": "https://previews.test/pre-oak.mp3"},
    ],
    "has_more": False,
}


def _openai_client(reply):
    client = MagicMock()
    if isinstance(reply, Exception):
        client.chat.completions.create = AsyncMock(side_effect=reply)
    else:
        completion = MagicMock()
        completion.choices = [MagicMock(message=MagicMock(content=json.dumps(reply)))]
        client.chat.completions.create = AsyncMock(return_value=completion)
    return client


def _sent_listing(client) -> dict:
    return json.loads(client.chat.completions.create.call_args.kwargs["messages"][1]["content"])


# ------------------------------------------------------------------------------- ranking (no network)

def test_fake_voices_are_deterministic_placeholders():
    first, second = vp.fake_voices(), vp.fake_voices()
    assert first == second
    assert len(first) == len(vp.FAKE_VOICES) >= 6
    assert all(vp.is_fake_voice_id(v["voice_id"]) for v in first)
    assert all(v["preview_url"].startswith("data:audio/wav;base64,") for v in first)
    assert len({v["preview_url"] for v in first}) == len(first)  # a different tone per voice
    assert not vp.is_fake_voice_id("pre-oak") and not vp.is_fake_voice_id("") and not vp.is_fake_voice_id(None)


@pytest.mark.parametrize("description,expected", [
    ("an old man with a deep, low voice", "Cedar"),
    ("excited young woman, sports announcer", "Ember"),
    ("soft whispery ASMR voice", "Juniper"),
    ("friendly british host", "Harbor"),
    ("playful cartoon kid", "Pebble"),
])
def test_label_match_picks_the_described_voice(description, expected):
    assert vp.rank_voices(description, vp.fake_voices())[0]["name"] == expected


def test_a_different_gender_than_asked_counts_against_a_voice():
    voices = [{"voice_id": "a", "name": "A", "labels": {"gender": "male", "descriptive": "warm"}},
              {"voice_id": "b", "name": "B", "labels": {"gender": "female"}}]
    assert vp.rank_voices("a warm woman", voices)[0]["voice_id"] == "b"


def test_ties_keep_the_listing_order():
    voices = [{"voice_id": f"v{i}", "name": f"V{i}", "labels": {}} for i in range(4)]
    assert [v["voice_id"] for v in vp.rank_voices("anything at all", voices)] == ["v0", "v1", "v2", "v3"]


async def test_try_another_excludes_every_voice_already_shown():
    voices = vp.fake_voices()
    shown: list[str] = []
    for _ in voices:
        pick = await vp.pick_voice("warm older storyteller", voices, exclude=shown)
        assert pick.voice_id not in shown
        shown.append(pick.voice_id)
        assert pick.remaining == len(voices) - len(shown)
        assert pick.method == "label_match"
    assert sorted(shown) == sorted(v["voice_id"] for v in voices)
    with pytest.raises(vp.NoVoiceLeftError, match="every available voice"):
        await vp.pick_voice("warm older storyteller", voices, exclude=shown)


async def test_llm_choice_is_used_when_it_names_a_listed_voice():
    voices = vp.fake_voices()
    client = _openai_client({"voice_id": "dryrun-voice-quill", "reason": "even documentary tone"})
    with patch("openai.AsyncOpenAI", return_value=client):
        pick = await vp.pick_voice("something neutral", voices, role="narrator", openai_api_key="test-key")
    assert (pick.voice_id, pick.method, pick.reason) == ("dryrun-voice-quill", "llm", "even documentary tone")
    sent = _sent_listing(client)
    assert sent["wanted_voice"] == "something neutral" and sent["speaks_as"] == "narrator"
    assert {v["voice_id"] for v in sent["voices"]} == {v["voice_id"] for v in voices}


@pytest.mark.parametrize("reply", [{"voice_id": "not-in-the-list"}, {"reason": "no id"}, RuntimeError("down")])
async def test_llm_answer_outside_the_list_or_an_error_falls_back_to_the_label_match(reply):
    voices = vp.fake_voices()
    expected = vp.rank_voices("deep old man", voices)[0]["voice_id"]
    with patch("openai.AsyncOpenAI", return_value=_openai_client(reply)):
        pick = await vp.pick_voice("deep old man", voices, openai_api_key="test-key")
    assert (pick.voice_id, pick.method) == (expected, "label_match")


async def test_the_llm_never_sees_excluded_voices():
    voices = vp.fake_voices()
    client = _openai_client({"voice_id": "dryrun-voice-maple"})
    with patch("openai.AsyncOpenAI", return_value=client):
        pick = await vp.pick_voice("warm", voices, exclude=["dryrun-voice-maple"], openai_api_key="test-key")
    assert "dryrun-voice-maple" not in {v["voice_id"] for v in _sent_listing(client)["voices"]}
    assert pick.voice_id != "dryrun-voice-maple" and pick.method == "label_match"


# ------------------------------------------------------------------------------- ElevenLabs listing

@respx.mock
async def test_list_voices_pages_through_account_and_premade_voices():
    from hfvg.providers.elevenlabs import API_BASE, ElevenLabsProvider

    route = respx.get(f"{API_BASE}/v2/voices").mock(side_effect=[
        httpx.Response(200, json={"voices": [LIVE_VOICES["voices"][0]], "has_more": True, "next_page_token": "p2"}),
        httpx.Response(200, json={"voices": [LIVE_VOICES["voices"][1]], "has_more": False}),
    ])
    voices = await ElevenLabsProvider(api_key="test-key", dry_run=False).list_voices()
    assert [(v["voice_id"], v["category"]) for v in voices] == [("acct-river", "cloned"), ("pre-oak", "premade")]
    assert voices[1]["preview_url"] == "https://previews.test/pre-oak.mp3"
    assert voices[1]["labels"]["age"] == "old"
    assert route.call_count == 2
    assert route.calls[0].request.headers["xi-api-key"] == "test-key"
    assert "next_page_token" not in route.calls[0].request.url.params
    assert route.calls[1].request.url.params["next_page_token"] == "p2"


@respx.mock
async def test_list_voices_http_error_raises_with_the_status():
    from hfvg.providers.elevenlabs import API_BASE, ElevenLabsError, ElevenLabsProvider

    respx.get(f"{API_BASE}/v2/voices").mock(return_value=httpx.Response(401, json={"detail": "missing_permissions"}))
    with pytest.raises(ElevenLabsError) as err:
        await ElevenLabsProvider(api_key="test-key", dry_run=False).list_voices()
    assert err.value.status_code == 401


async def test_list_voices_is_never_called_in_dry_run():
    from hfvg.providers.elevenlabs import ElevenLabsError, ElevenLabsProvider

    with pytest.raises(ElevenLabsError, match="live call"):
        await ElevenLabsProvider(dry_run=True).list_voices()


# ------------------------------------------------------------------------------- POST /api/voices/pick

@pytest.fixture
async def app(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager

    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    users = UserManager(db)
    await users.init_db()
    await users.create_user(name="Member", email=MEMBER[0], role="member", password=MEMBER[1])
    sys.modules.pop("api.main", None)
    from api.main import app as fastapi_app
    return fastapi_app


def _client(app, role: str):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    if role == "none":
        return client
    body = {"email": MEMBER[0], "password": MEMBER[1]} if role == "member" else {"admin_secret": ADMIN_SECRET}
    login = client.post("/api/login", json=body)
    assert login.status_code == 200, login.text
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _pick(client, description="warm older storyteller", exclude=(), role="narrator", **kwargs):
    return client.post("/api/voices/pick", json={"description": description, "exclude_voice_ids": list(exclude),
                                                 "role": role}, **kwargs)


def test_pick_requires_auth(app):
    assert _pick(_client(app, "none")).status_code == 401


@pytest.mark.parametrize("role", ["member", "admin"])
def test_member_and_admin_pick_a_placeholder_voice_in_dry_run(app, role):
    res = _pick(_client(app, role), "an old man with a deep, low voice")
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["mode"] == "dry_run"
    assert (data["voice_id"], data["name"]) == ("dryrun-voice-cedar", "Cedar")
    assert data["preview_url"].startswith("data:audio/wav;base64,")
    assert data["remaining"] == len(vp.FAKE_VOICES) - 1


def test_admin_bearer_secret_can_pick(app):
    res = _pick(_client(app, "none"), headers={"Authorization": f"Bearer {ADMIN_SECRET}"})
    assert res.status_code == 200, res.text


def test_dry_run_pick_never_lists_or_asks_the_llm(app, monkeypatch):
    from hfvg.config import config
    from hfvg.providers.elevenlabs import ElevenLabsProvider

    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(ElevenLabsProvider, "list_voices", AsyncMock(side_effect=AssertionError("network")))
    with patch("openai.AsyncOpenAI", side_effect=AssertionError("network")):
        res = _pick(_client(app, "member"))
    assert res.status_code == 200, res.text
    assert res.json()["method"] == "label_match"


def test_try_another_voice_walks_through_every_voice_then_409(app):
    client = _client(app, "member")
    shown: list[str] = []
    for _ in vp.FAKE_VOICES:
        res = _pick(client, exclude=shown)
        assert res.status_code == 200, res.text
        assert res.json()["voice_id"] not in shown
        shown.append(res.json()["voice_id"])
    assert len(set(shown)) == len(vp.FAKE_VOICES)
    last = _pick(client, exclude=shown)
    assert last.status_code == 409
    assert "every available voice" in last.json()["detail"]


def test_same_description_gives_the_same_first_voice(app):
    client = _client(app, "member")
    assert _pick(client).json()["voice_id"] == _pick(client).json()["voice_id"]


@pytest.mark.parametrize("body", [{"description": "ab"}, {"description": "x" * 301}, {}])
def test_pick_validates_the_description(app, body):
    assert _client(app, "member").post("/api/voices/pick", json=body).status_code == 422


def test_live_pick_without_elevenlabs_key_is_503(app, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    res = _pick(_client(app, "member"))
    assert res.status_code == 503
    assert "ELEVENLABS_API_KEY" in res.json()["detail"]


@respx.mock
def test_live_pick_lists_voices_and_label_matches_without_openai_key(app, monkeypatch):
    from hfvg.config import config
    from hfvg.providers.elevenlabs import API_BASE

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven-key")
    monkeypatch.setattr(config, "OPENAI_API_KEY", None)
    route = respx.get(f"{API_BASE}/v2/voices").mock(return_value=httpx.Response(200, json=LIVE_VOICES))
    client = _client(app, "member")
    first = _pick(client, "an older man, deep voice, storyteller")
    assert first.status_code == 200, first.text
    data = first.json()
    assert (data["voice_id"], data["method"], data["mode"]) == ("pre-oak", "label_match", "live")
    assert data["preview_url"] == "https://previews.test/pre-oak.mp3"
    second = _pick(client, "an older man, deep voice, storyteller", exclude=["pre-oak"])
    assert second.json()["voice_id"] == "acct-river"
    assert route.calls[0].request.headers["xi-api-key"] == "test-eleven-key"


@respx.mock
def test_live_pick_uses_the_llm_choice_when_openai_is_configured(app, monkeypatch):
    from hfvg.config import config
    from hfvg.providers.elevenlabs import API_BASE

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven-key")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-openai-key")
    respx.get(f"{API_BASE}/v2/voices").mock(return_value=httpx.Response(200, json=LIVE_VOICES))
    llm = _openai_client({"voice_id": "acct-river", "reason": "bright and young"})
    with patch("openai.AsyncOpenAI", return_value=llm):
        res = _pick(_client(app, "member"), "a cheerful teenager")
    assert res.status_code == 200, res.text
    assert (res.json()["voice_id"], res.json()["method"]) == ("acct-river", "llm")
    assert {v["voice_id"] for v in _sent_listing(llm)["voices"]} == {"acct-river", "pre-oak"}


@respx.mock
def test_live_listing_error_is_502(app, monkeypatch):
    from hfvg.providers.elevenlabs import API_BASE

    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-eleven-key")
    respx.get(f"{API_BASE}/v2/voices").mock(return_value=httpx.Response(401, json={"detail": "missing_permissions"}))
    res = _pick(_client(app, "member"))
    assert res.status_code == 502
    assert "HTTP 401" in res.json()["detail"]
