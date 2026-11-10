"""Create-wizard audio plans in the delivery stage.

For every card in hfvg/data/genre_presets.yaml (and the freeform Skip), at the shortest, default
and longest length: the plan the create page uploads to data/episodes/<id>/audio/audio_plan.json
loads through hfvg.activities.audio.load_audio_plan in a LIVE run (no plan = MissingAudioPlan,
invalid = InvalidAudioPlan), carries the voices the creator chose (narrator: vo.voice_id,
dialogue: vo.speakers -> each line's voice_id) and the workflow sends each line its own voice.
"""

import json
from pathlib import Path

import pytest
import yaml
from temporalio import activity, workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import audio_mix, genre_drafts
from hfvg.activities import audio as audio_activities
from hfvg.activities.audio import dry_vo_seconds, generate_voiceover, load_audio_plan
from hfvg.audio_mix import AudioInputError, normalize_plan, vo_lines_missing_voice
from hfvg.genre_presets import REGISTRY_PATH, resolve_profile
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

PRESET_IDS = [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]
VOICE_CARDS = [pid for pid in PRESET_IDS if resolve_profile(pid)["vo_mode"] in ("narration", "dialogue")]
IDEA = "A lighthouse keeper befriends a lost paper kite on a windy evening by the harbor"
EPISODE = "ep42wiz"


def _target(profile: dict, which: str) -> int:
    lo, hi = profile["length_s"]
    return {"min": lo, "default": profile["default_length_s"], "max": hi}[which]


def _voice_for(slot: dict) -> str:
    return "voice-" + slot["key"].replace(":", "-").lower()


def _wizard_plan(profile: dict, target: int, choose_voices: bool = True) -> tuple[dict, list[float]]:
    """The draft's audio plan with the voices chosen the way the create page writes them."""
    files = genre_drafts.fake_draft(profile, IDEA, EPISODE, target, "watercolor")
    lengths = [float(s["duration"]) for s in files["shots_json"]["shots"]]
    plan, _ = genre_drafts.build_audio_plan(profile, lengths, files["audio_script"])
    if choose_voices:
        for slot in genre_drafts.voice_slots(plan, profile):
            if slot["speaker"]:
                plan["vo"]["speakers"][slot["speaker"]] = _voice_for(slot)
            else:
                plan["vo"]["voice_id"] = _voice_for(slot)
    return plan, lengths


def _upload(plan: dict) -> Path:
    path = audio_mix.audio_plan_path(EPISODE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


def test_registry_cards_are_all_covered():
    assert len(PRESET_IDS) == 24
    assert VOICE_CARDS, "at least one card speaks"


@pytest.mark.parametrize("which", ["min", "default", "max"])
@pytest.mark.parametrize("archetype_id", PRESET_IDS + [None])
async def test_audio_plan_validates_for_all_presets(archetype_id, which, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    profile = resolve_profile(archetype_id)
    plan, lengths = _wizard_plan(profile, _target(profile, which))
    path = _upload(plan)
    assert path == audio_mix.episode_dir(EPISODE) / "audio" / "audio_plan.json"

    loaded = await load_audio_plan(EPISODE)
    assert loaded["plan_found"] is True and loaded["plan_path"] == str(path)
    assert vo_lines_missing_voice(loaded) == []
    lines = loaded["vo"]["lines"]
    if profile["vo_mode"] == "dialogue":
        assert lines
        assert {ln["voice_id"] for ln in lines} <= set(plan["vo"]["speakers"].values())
        for ln in lines:
            assert ln["voice_id"] == plan["vo"]["speakers"][ln["speaker"]]
    elif profile["vo_mode"] == "narration":
        assert lines and loaded["vo"]["voice_id"] == "voice-narrator"
        assert all(ln.get("voice_id") is None for ln in lines)
    else:
        assert lines == [] and loaded["vo"].get("speakers") is None

    picture = sum(lengths)
    for ln, nxt in zip(lines, lines[1:] + [None]):
        end = ln["at"] + dry_vo_seconds(ln["text"])
        assert end <= picture
        if nxt is not None:
            assert end < nxt["at"], f"{ln['id']} would run into {nxt['id']}"
    for cue in loaded["sfx"]:
        assert 0 <= cue["at"] < picture
    assert loaded["music"] is not None or loaded["sfx"], "every card has a soundtrack"


def test_audio_plan_schema_structure():
    for archetype_id in [*VOICE_CARDS[:2], _card("dialogue"), _card("none"), _card("caption_only"), None]:
        profile = resolve_profile(archetype_id)
        plan, _ = _wizard_plan(profile, profile["default_length_s"], choose_voices=False)
        assert set(plan) <= {"vo", "music", "sfx"}
        if "vo" in plan:
            assert set(plan["vo"]) <= {"voice_id", "model", "speakers", "lines"}
            assert plan["vo"]["model"] == genre_drafts.VO_MODEL
            for ln in plan["vo"]["lines"]:
                assert set(ln) <= {"id", "at", "text", "speaker"}
        if "music" in plan:
            assert set(plan["music"]) == {"prompt", "loop", "level_db"}
        for cue in plan.get("sfx", []):
            assert {"id", "at", "dur", "prompt"} <= set(cue) <= {"id", "at", "dur", "prompt", "loop", "level_db"}


def _card(vo_mode: str) -> str:
    return next(pid for pid in PRESET_IDS if resolve_profile(pid)["vo_mode"] == vo_mode)


@pytest.mark.parametrize("archetype_id", VOICE_CARDS)
async def test_lines_without_a_voice_are_reported_until_one_is_chosen(archetype_id, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    profile = resolve_profile(archetype_id)
    plan, _ = _wizard_plan(profile, profile["default_length_s"], choose_voices=False)
    _upload(plan)
    loaded = await load_audio_plan(EPISODE)
    assert vo_lines_missing_voice(loaded) == [ln["id"] for ln in loaded["vo"]["lines"]]
    assert vo_lines_missing_voice(loaded, "studio-default-voice") == []


async def test_live_run_without_a_plan_is_refused(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    with pytest.raises(ApplicationError) as err:
        await load_audio_plan(EPISODE)
    assert err.value.type == "MissingAudioPlan"


async def test_unknown_speaker_is_an_invalid_plan(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    _upload({"vo": {"speakers": {"Host": "v1"}, "lines": [{"id": "L01", "at": 0.3, "text": "Hi", "speaker": "Guest"}]}})
    with pytest.raises(ApplicationError) as err:
        await load_audio_plan(EPISODE)
    assert err.value.type == "InvalidAudioPlan"
    assert "Guest" in str(err.value)


# ------------------------------------------------------------------------------- normalize_plan voices

def test_plans_without_speakers_normalize_as_before(tmp_path):
    plan = normalize_plan({"vo": {"voice_id": "v-main", "lines": [{"id": "L01", "at": 1.0, "text": "Hello"}]}}, tmp_path)
    assert "speakers" not in plan["vo"]
    assert "speaker" not in plan["vo"]["lines"][0] and "voice_id" not in plan["vo"]["lines"][0]
    assert plan["vo"]["voice_id"] == "v-main"


def test_speaker_voices_and_line_overrides(tmp_path):
    plan = normalize_plan({"vo": {"speakers": {"Host": "v-host", "Guide": " v-guide "}, "lines": [
        {"id": "L01", "at": 0.3, "text": "Welcome aboard", "speaker": "Host"},
        {"id": "L02", "at": 3.0, "text": "Mind the step", "speaker": "Guide"},
        {"id": "L03", "at": 6.0, "text": "A special guest line", "speaker": "Host", "voice_id": "v-special"},
    ]}}, tmp_path)
    assert plan["vo"]["speakers"] == {"Host": "v-host", "Guide": "v-guide"}
    assert [(ln["speaker"], ln["voice_id"]) for ln in plan["vo"]["lines"]] == [
        ("Host", "v-host"), ("Guide", "v-guide"), ("Host", "v-special")]


def test_speaker_not_chosen_yet_has_no_line_voice(tmp_path):
    plan = normalize_plan({"vo": {"speakers": {"Host": "", "Guide": None}, "lines": [
        {"id": "L01", "at": 0.3, "text": "Welcome", "speaker": "Host"}]}}, tmp_path)
    assert plan["vo"]["speakers"] == {"Host": "", "Guide": ""}
    assert "voice_id" not in plan["vo"]["lines"][0]
    assert vo_lines_missing_voice(plan) == ["L01"]


@pytest.mark.parametrize("vo,match", [
    ({"speakers": ["Host"], "lines": []}, "must map speaker names"),
    ({"speakers": {"": "v1"}, "lines": []}, "1-60 characters"),
    ({"speakers": {"x" * 61: "v1"}, "lines": []}, "1-60 characters"),
    ({"speakers": {"Host": 7}, "lines": []}, "voice id string"),
    ({"speakers": {"Host": "v1"}, "lines": [{"id": "L01", "at": 0.3, "text": "Hi", "speaker": "Guest"}]},
     "not listed in vo.speakers"),
    ({"lines": [{"id": "L01", "at": 0.3, "text": "Hi", "speaker": "  "}]}, "non-empty name"),
])
def test_invalid_speaker_data_is_refused(tmp_path, vo, match):
    with pytest.raises(AudioInputError, match=match):
        normalize_plan({"vo": vo}, tmp_path)


def test_missing_voice_ignores_locked_lines_and_uses_the_plan_voice(tmp_path):
    locked = tmp_path / "L01.wav"
    locked.write_bytes(b"RIFF")
    lines = [{"id": "L01", "at": 0.3, "path": str(locked)}, {"id": "L02", "at": 3.0, "text": "Generated"}]
    assert vo_lines_missing_voice(normalize_plan({"vo": {"lines": lines}}, tmp_path)) == ["L02"]
    assert vo_lines_missing_voice(normalize_plan({"vo": {"voice_id": "v1", "lines": lines}}, tmp_path)) == []


# ------------------------------------------------------------------------------- ELEVENLABS_DEFAULT_VOICE_ID

async def test_default_voice_speaks_lines_without_a_voice(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_DEFAULT_VOICE_ID", " studio-default-voice ")
    key = audio_activities._key("tts", "Hello there", "studio-default-voice", "eleven_v3")
    cached = audio_activities.generated_path(EPISODE, "vo", "L01", key)
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"ID3")  # already generated with the default voice: reused, nothing is spent
    res = await generate_voiceover(EPISODE, "Hello there", "", "eleven_v3", "L01")
    assert res["source"] == "generated_reused"
    assert res["path"] == str(cached)


async def test_without_a_default_voice_a_live_line_needs_one(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    with pytest.raises(ApplicationError) as err:
        await generate_voiceover(EPISODE, "Hello there", "", "eleven_v3", "L01")
    assert err.value.type == "MissingAudioInput"
    assert "needs a voice_id" in str(err.value)


# ------------------------------------------------------------------------------- workflow: per-line voices

@workflow.defn(name="ResolveAudioInputsHarness", sandboxed=False)
class ResolveAudioInputsHarness:
    @workflow.run
    async def run(self, episode_id: str, plan: dict) -> dict:
        return await EpisodeWorkflowV2()._resolve_audio_inputs(episode_id, plan, 30.0)


async def _resolve(plan: dict) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    @activity.defn(name="generate_voiceover")
    async def vo_stub(episode_id, script, voice_id, model, line_id, locked_path, take, user_id):
        calls.append((line_id, voice_id))
        return {"path": f"/tmp/{line_id}.wav", "dry_run": True}

    @activity.defn(name="generate_sfx")
    async def sfx_stub(*args):
        return {"path": "/tmp/sfx.wav", "dry_run": True}

    @activity.defn(name="generate_music")
    async def music_stub(*args):
        return {"path": "/tmp/music.wav", "dry_run": True}

    @activity.defn(name="get_episode_user_id_activity")
    async def user_stub(episode_id):
        return "usr_wizard"

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="audio-voices", workflows=[ResolveAudioInputsHarness],
                          activities=[vo_stub, sfx_stub, music_stub, user_stub]):
            await env.client.execute_workflow(ResolveAudioInputsHarness.run, args=[EPISODE, plan],
                                              id="resolve-audio-voices", task_queue="audio-voices")
    return calls


async def test_workflow_sends_each_dialogue_line_its_speakers_voice(tmp_path):
    profile = resolve_profile(_card("dialogue"))
    plan, _ = _wizard_plan(profile, profile["length_s"][1])
    normalized = normalize_plan(plan, tmp_path)
    calls = await _resolve(normalized)
    assert calls == [(ln["id"], plan["vo"]["speakers"][ln["speaker"]]) for ln in normalized["vo"]["lines"]]
    assert len({voice for _, voice in calls}) == len(plan["vo"]["speakers"])


async def test_workflow_sends_narration_lines_the_narrator_voice(tmp_path):
    profile = resolve_profile(_card("narration"))
    plan, _ = _wizard_plan(profile, profile["default_length_s"])
    normalized = normalize_plan(plan, tmp_path)
    calls = await _resolve(normalized)
    assert calls and all(voice == "voice-narrator" for _, voice in calls)
