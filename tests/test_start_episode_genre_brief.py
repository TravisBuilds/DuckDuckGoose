"""POST /api/episodes with the create wizard's brief.

The API resolves the card (hfvg/data/genre_presets.yaml) and clamps the creator's length, then passes
{"idea", "archetype_id", "target_duration_s", "preset"} as the workflow's optional 9th input (starts
without a brief keep main's 8 arguments). A start is refused, dry or live, when the uploaded audio
plan has voice-over lines and no voice to speak them; a dry-run placeholder voice is no voice live.
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from hfvg import audio_mix
from hfvg.genre_presets import clamp_length, resolve_profile

ADMIN_SECRET = "start-brief-admin-secret-0123456789abcdef"
EPISODE = "ep42brief"
BEATMAP = """# Episode ep42brief Beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | S01 | 6.0 (6.0) | Harbor | Dusk | A | A kite lands on the lighthouse rail | Open | - | - | K |
"""
VO_PLAN = {"vo": {"voice_id": None, "lines": [{"id": "L01", "at": 0.3, "text": "The light still burns."},
                                               {"id": "L02", "at": 3.0, "text": "And the kite stays."}]}}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    sys.modules.pop("api.main", None)
    import api.main as api_main

    temporal = MagicMock()
    temporal.start_workflow = AsyncMock(return_value=MagicMock(id=EPISODE, result_run_id="run-1"))
    api_main.temporal_client = temporal
    return api_main


def _start(api, **body):
    from fastapi.testclient import TestClient

    payload = {"episode_id": EPISODE, "beatmap_content": BEATMAP, "dry_run": True, "enable_story_shaping": True}
    payload.update(body)
    return TestClient(api.app).post("/api/episodes", json=payload,
                                    headers={"Authorization": f"Bearer {ADMIN_SECRET}"})


def _args(api) -> list:
    return api.temporal_client.start_workflow.call_args.kwargs["args"]


def _plan(plan: dict) -> None:
    path = audio_mix.audio_plan_path(EPISODE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")


def test_brief_is_the_ninth_workflow_input(api):
    res = _start(api, archetype_id="cozy-hangout", idea="A cat naps by a rainy window", target_duration=12)
    assert res.status_code == 200, res.text
    args = _args(api)
    assert len(args) == 9
    assert args[:8] == [EPISODE, str(audio_mix.episode_dir(EPISODE) / "BEATMAP.md"), True, None, None, None,
                        None, True]
    assert args[8] == {"idea": "A cat naps by a rainy window", "archetype_id": "cozy-hangout",
                       "target_duration_s": 12, "preset": resolve_profile("cozy-hangout")}
    assert args[8]["preset"]["story_structure"] == "loop"


def test_brief_length_is_clamped_to_the_card(api):
    assert _start(api, archetype_id="cozy-hangout", idea="A cat naps", target_duration=60).status_code == 200
    assert _args(api)[8]["target_duration_s"] == clamp_length(resolve_profile("cozy-hangout"), 60)[0] == 15


def test_brief_without_a_card_is_freeform(api):
    assert _start(api, idea="A kite and a lighthouse").status_code == 200
    brief = _args(api)[8]
    assert brief["archetype_id"] is None
    assert brief["preset"] == resolve_profile(None)
    assert brief["target_duration_s"] == resolve_profile(None)["default_length_s"] == 60


def test_card_without_a_length_uses_its_default(api):
    profile = resolve_profile("time-travel-vlog")
    assert _start(api, archetype_id="time-travel-vlog", idea="A vlog from a market long ago").status_code == 200
    assert _args(api)[8]["target_duration_s"] == profile["default_length_s"]


def test_start_without_a_brief_keeps_eight_arguments(api):
    assert _start(api).status_code == 200
    assert len(_args(api)) == 8


def test_unknown_card_is_refused(api):
    res = _start(api, archetype_id="no-such-card", idea="Anything")
    assert res.status_code == 400
    assert "Unknown video type" in res.json()["detail"]
    api.temporal_client.start_workflow.assert_not_called()


@pytest.mark.parametrize("target", [9, 181])
def test_brief_length_bounds(api, target):
    assert _start(api, idea="A kite", target_duration=target).status_code == 422


@pytest.mark.parametrize("dry_run", [True, False])
def test_voice_over_without_a_voice_blocks_the_start(api, dry_run):
    _plan(VO_PLAN)
    res = _start(api, dry_run=dry_run)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert detail.startswith(f"Refusing to start {EPISODE}: no voice chosen for the voice-over lines L01, L02")
    assert "Describe the voice on the create page" in detail and "ELEVENLABS_DEFAULT_VOICE_ID" in detail
    api.temporal_client.start_workflow.assert_not_called()


@pytest.mark.parametrize("dry_run", [True, False])
def test_chosen_voice_lets_the_start_through(api, dry_run):
    _plan({"vo": {**VO_PLAN["vo"], "voice_id": "real-voice-id"}})
    assert _start(api, dry_run=dry_run).status_code == 200


def test_each_dialogue_speaker_needs_a_voice(api):
    plan = {"vo": {"speakers": {"Host": "real-host", "Guide": ""}, "lines": [
        {"id": "L01", "at": 0.3, "text": "Welcome aboard", "speaker": "Host"},
        {"id": "L02", "at": 3.0, "text": "Mind the step", "speaker": "Guide"}]}}
    _plan(plan)
    res = _start(api)
    assert res.status_code == 400
    assert "voice-over lines L02." in res.json()["detail"]
    plan["vo"]["speakers"]["Guide"] = "real-guide"
    _plan(plan)
    assert _start(api).status_code == 200


def test_default_voice_covers_lines_without_one(api, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_DEFAULT_VOICE_ID", "studio-default-voice")
    _plan(VO_PLAN)
    assert _start(api, dry_run=False).status_code == 200


def test_placeholder_voices_cannot_speak_live(api):
    _plan({"vo": {**VO_PLAN["vo"], "voice_id": "dryrun-voice-maple"}})
    live = _start(api, dry_run=False)
    assert live.status_code == 400
    assert "dry-run preview voices cannot speak in a live run" in live.json()["detail"]
    assert _start(api, dry_run=True).status_code == 200


@pytest.mark.parametrize("plan", [None, {"music": {"prompt": "calm piano"}},
                                  {"vo": {"lines": [{"id": "L01", "at": 0.3, "path": "audio/locked/L01.wav"}]}}])
def test_plans_without_lines_to_voice_start(api, plan):
    if plan is not None:
        locked = audio_mix.episode_dir(EPISODE) / "audio" / "locked" / "L01.wav"
        locked.parent.mkdir(parents=True, exist_ok=True)
        locked.write_bytes(b"RIFF")
        _plan(plan)
    assert _start(api, dry_run=False).status_code == 200
    assert os.path.isfile(audio_mix.episode_dir(EPISODE) / "BEATMAP.md")
