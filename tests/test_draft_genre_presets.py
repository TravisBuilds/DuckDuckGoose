"""Create-wizard drafts per card (POST /api/episodes/draft-from-idea).

Every card in hfvg/data/genre_presets.yaml (and the freeform Skip) gets files that pass the real
validators, the card's pacing and length range, and an audio plan in the delivery loader's schema
(hfvg.audio_mix.normalize_plan) with the voices the wizard asks for. tests/test_draft_from_idea.py
keeps main's contract (IP note first, fake refusal in live mode, request bounds, repair call).
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from hfvg import genre_drafts
from hfvg.activities.audio import dry_vo_seconds
from hfvg.audio_mix import normalize_plan
from hfvg.episode_parser import parse_beatmap
from hfvg.genre_presets import REGISTRY_PATH, clamp_length, resolve_profile
from hfvg.preflight import preflight_plans
from hfvg.shot_data import parse_shots

PRESET_IDS = [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]
ADMIN_SECRET = "draft-genre-admin-secret-0123456789abcdef"
IDEA = "A lighthouse keeper befriends a lost paper kite on a windy evening by the harbor"
TEST_MODE = "Test mode: Using canned draft data (DRAFT_LLM_FAKE=1)"


def _first_card(vo_mode: str) -> str:
    return next(pid for pid in PRESET_IDS if resolve_profile(pid)["vo_mode"] == vo_mode)


def test_every_card_of_the_registry_is_covered():
    assert len(PRESET_IDS) == 24
    assert len(set(PRESET_IDS)) == 24


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient

    from api.main import app

    test_client = TestClient(app)
    login = test_client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert login.status_code == 200
    test_client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return test_client


def _draft(client, archetype_id, target, idea=IDEA):
    return client.post("/api/episodes/draft-from-idea",
                       json={"idea": idea, "archetype_id": archetype_id, "target_duration": target})


@pytest.mark.parametrize("archetype_id", PRESET_IDS + [None])
def test_test_draft_follows_the_card(client, archetype_id, tmp_path):
    profile = resolve_profile(archetype_id)
    target = profile["default_length_s"]
    res = _draft(client, archetype_id, target)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["warnings"][0] == TEST_MODE
    assert data["archetype_id"] == archetype_id
    assert data["preset"]["id"] == profile["id"]
    assert data["preset"]["story_structure"] == profile["story_structure"]
    assert data["preset"]["vo_mode"] == profile["vo_mode"]
    assert data["target_duration"] == target
    assert data["prompt_kit_json"]["aspect_ratio"] == "9:16"
    assert data["default_voice_available"] is False

    shots = data["shots_json"]["shots"]
    parse_shots(data["shots_json"])
    assert preflight_plans(shots)["errors"] == []
    assert sum(s["duration"] for s in shots) == target
    assert [s["duration"] for s in shots] == genre_drafts.shot_lengths(profile, target)
    assert sorted(data["refs_json"]["per_shot"]) == sorted(s["id"] for s in shots)
    beatmap = tmp_path / "BEATMAP.md"
    beatmap.write_text(data["beatmap"], encoding="utf-8")
    assert [p["screen_time"] for p in parse_beatmap(beatmap)] == [float(s["duration"]) for s in shots]

    plan = normalize_plan(data["audio_plan_json"], tmp_path)
    lines = plan["vo"]["lines"]
    slots = data["voice_slots"]
    if profile["vo_mode"] == "narration":
        assert lines and all("speaker" not in ln for ln in lines)
        assert [s["key"] for s in slots] == ["narrator"]
    elif profile["vo_mode"] == "dialogue":
        assert lines and all(ln["speaker"] in plan["vo"]["speakers"] for ln in lines)
        assert [s["key"] for s in slots] == [f"speaker:{name}" for name in plan["vo"]["speakers"]]
        assert len(plan["vo"]["speakers"]) >= 2
    else:
        assert lines == [] and slots == []
    picture = float(target)
    for ln in lines:
        assert ln["at"] + dry_vo_seconds(ln["text"]) <= picture
    for cue in plan["sfx"]:
        assert cue["at"] < picture
    if plan["music"] is not None:
        assert plan["music"]["level_db"] == (genre_drafts.MUSIC_BED_DB if lines else genre_drafts.MUSIC_ONLY_DB)


def test_length_is_clamped_to_the_card_range_with_a_note(client):
    profile = resolve_profile("cozy-hangout")
    expected, note = clamp_length(profile, 30)
    assert (expected, profile["length_s"]) == (15, [10, 15])
    res = _draft(client, "cozy-hangout", 30)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["target_duration"] == 15
    assert sum(s["duration"] for s in data["shots_json"]["shots"]) == 15
    assert data["warnings"][:2] == [TEST_MODE, note]
    assert "10-15 seconds" in note


@pytest.mark.parametrize("target,status", [(9, 422), (181, 422), (10, 200), (180, 200)])
def test_request_length_bounds(client, target, status):
    assert _draft(client, None, target).status_code == status


def test_freeform_short_request_is_raised_to_its_minimum(client):
    data = _draft(client, None, 10).json()
    assert data["target_duration"] == 30
    assert "30-120 seconds" in data["warnings"][1]


def test_unknown_card_is_refused(client):
    res = _draft(client, "no-such-card", 30)
    assert res.status_code == 400
    assert "Unknown video type" in res.json()["detail"]


@pytest.mark.parametrize("archetype_id", PRESET_IDS)
def test_cards_made_for_another_aspect_say_the_studio_renders_9_16(client, archetype_id):
    profile = resolve_profile(archetype_id)
    warnings = _draft(client, archetype_id, profile["default_length_s"]).json()["warnings"]
    aspect_notes = [w for w in warnings if "renders 9:16" in w]
    assert len(aspect_notes) == (0 if profile["aspect"] == "9:16" else 1)


def test_members_can_draft_with_a_card(client, monkeypatch):
    import asyncio

    from fastapi.testclient import TestClient

    from api.main import app
    from hfvg.users import UserManager

    users = UserManager(os.environ["DATABASE_PATH"])
    asyncio.run(users.init_db())
    asyncio.run(users.create_user(name="M", email="m@draft.test", role="member", password="member-pass-1"))
    member = TestClient(app)
    login = member.post("/api/login", json={"email": "m@draft.test", "password": "member-pass-1"})
    assert login.status_code == 200
    member.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    res = _draft(member, _first_card("dialogue"), 30)
    assert res.status_code == 200, res.text


# ------------------------------------------------------------------------------- LLM path (mocked OpenAI)

def _llm_client(result: dict):
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=json.dumps(result)))]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=completion)
    return client


def _llm_result(profile, target, script=None, ip_note=""):
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", target, "watercolor")
    result = {k: files[k] for k in ("beatmap_md", "shots_json", "refs_json", "prompt_kit_json")}
    result["ip_steering_note"] = ip_note
    if script is not None:
        result["audio_script"] = script
    return result


@pytest.fixture
def llm(client, monkeypatch):
    from hfvg.config import config

    monkeypatch.delenv("DRAFT_LLM_FAKE")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    return client


@pytest.mark.parametrize("archetype_id", PRESET_IDS)
def test_llm_prompt_carries_the_card_rules(llm, archetype_id):
    profile = resolve_profile(archetype_id)
    target = profile["default_length_s"]
    mock = _llm_client(_llm_result(profile, target))
    with patch("openai.AsyncOpenAI", return_value=mock):
        res = _draft(llm, archetype_id, target)
    assert res.status_code == 200, res.text
    assert mock.chat.completions.create.call_count == 1
    messages = mock.chat.completions.create.call_args.kwargs["messages"]
    system, user = messages[0]["content"], messages[1]["content"]
    assert genre_drafts.llm_instructions(profile, target) in system
    lengths = genre_drafts.shot_lengths(profile, target)
    assert f"Create {len(lengths)} shots total; their durations add up to {target}s" in system
    assert "5-8 seconds per shot" not in system
    assert f"Video type: {profile['name']}" in user
    assert ('"audio_script"' in system) == (profile["vo_mode"] in ("narration", "dialogue"))


def test_freeform_llm_prompt_keeps_the_generic_pacing(llm):
    mock = _llm_client(_llm_result(resolve_profile(None), 60))
    with patch("openai.AsyncOpenAI", return_value=mock):
        assert _draft(llm, None, 60).status_code == 200
    system = mock.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert "5-8 seconds per shot" in system and "Create 10 to 12 shots total" in system
    assert "VIDEO TYPE" not in system and '"audio_script"' not in system


def test_dialogue_script_from_the_llm_becomes_speaker_lines(llm, tmp_path):
    card = _first_card("dialogue")
    profile = resolve_profile(card)
    target = profile["length_s"][1]
    script = [{"speaker": "Mara", "text": "Did you hear the bell ring twice?"},
              {"speaker": "Tobin", "text": "Twice means the tide is turning."},
              {"speaker": "Mara", "text": "Then we leave at first light."}]
    with patch("openai.AsyncOpenAI", return_value=_llm_client(_llm_result(profile, target, script))):
        data = _draft(llm, card, target).json()
    plan = normalize_plan(data["audio_plan_json"], tmp_path)
    assert [(ln["speaker"], ln["text"]) for ln in plan["vo"]["lines"]] == [(s["speaker"], s["text"]) for s in script]
    assert plan["vo"]["speakers"] == {"Mara": "", "Tobin": ""}
    assert [s["key"] for s in data["voice_slots"]] == ["speaker:Mara", "speaker:Tobin"]


def test_narration_script_from_the_llm_is_one_voice(llm, tmp_path):
    card = _first_card("narration")
    profile = resolve_profile(card)
    target = profile["default_length_s"]
    script = [{"speaker": "Narrator", "text": "The harbor sleeps, but one light still burns."}]
    with patch("openai.AsyncOpenAI", return_value=_llm_client(_llm_result(profile, target, script))):
        data = _draft(llm, card, target).json()
    plan = normalize_plan(data["audio_plan_json"], tmp_path)
    assert [ln["text"] for ln in plan["vo"]["lines"]] == [script[0]["text"]]
    assert "speaker" not in plan["vo"]["lines"][0]
    assert [s["key"] for s in data["voice_slots"]] == ["narrator"]


def test_voice_card_without_a_script_warns_and_keeps_music(llm):
    card = _first_card("narration")
    profile = resolve_profile(card)
    with patch("openai.AsyncOpenAI", return_value=_llm_client(_llm_result(profile, profile["default_length_s"]))):
        data = _draft(llm, card, profile["default_length_s"]).json()
    assert "No voice-over lines were drafted; the video will have music only." in data["warnings"]
    assert data["voice_slots"] == []


def test_ip_note_stays_first_with_a_card_and_a_clamped_length(llm):
    profile = resolve_profile("cozy-hangout")
    note = "Changed a named character to an original sleepy cat in a knitted scarf"
    with patch("openai.AsyncOpenAI", return_value=_llm_client(_llm_result(profile, 15, ip_note=note))):
        data = _draft(llm, "cozy-hangout", 30).json()
    assert data["warnings"][0] == f"Content policy note: {note}"
    assert "original" in data["warnings"][0].lower()
    assert data["warnings"][1] == clamp_length(profile, 30)[1]


def test_default_voice_is_reported_to_the_wizard(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DEFAULT_VOICE_ID", "studio-default-voice")
    data = _draft(client, _first_card("narration"), 30).json()
    assert data["default_voice_available"] is True
