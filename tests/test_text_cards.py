"""Subtitles, opening title card and end card.

Each card suggests its text on screen (``on_screen_text`` in hfvg/data/genre_presets.yaml, mirrored
in web/lib/presets.ts); the draft writes that suggestion into its audio plan and the create wizard
shows it as switches. The plan's ``title_card`` / ``end_card`` are drawn into every picture
deliverable with plain drawtext (so DRY_RUN renders them too), the GAP-60 sync guard leaves their
frames out, and delivery QC measures that each card is really in each file. Burned-in subtitles stay
optional (``subtitles.burn``); the .srt sidecar is delivered either way.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import yaml

from hfvg import aspect, audio_mix, genre_drafts
from hfvg.audio_mix import AudioInputError, normalize_plan
from hfvg.genre_presets import FREEFORM_PROFILE, ON_SCREEN_TEXT, REGISTRY_PATH, load_genre_presets, resolve_profile
from tests.test_audio_delivery import episode
from tests.test_picture_assembly_round3 import needs_ffmpeg

RAW_PRESETS = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]
PRESET_IDS = [p["id"] for p in RAW_PRESETS]
ADMIN_SECRET = "text-cards-admin-secret-0123456789abcdef"
IDEA = "A lighthouse keeper befriends a lost paper kite on a windy evening by the harbor"


def _raises(fn, *args) -> str:
    try:
        fn(*args)
    except AudioInputError as e:
        return str(e)
    raise AssertionError(f"{fn.__name__}{args!r} was accepted")


# ------------------------------------------------------------------ the card's suggestion

def test_every_card_states_its_on_screen_text():
    assert len(RAW_PRESETS) == 24
    for raw in RAW_PRESETS:
        assert "on_screen_text" in raw, f"{raw['id']} does not say which text it puts on screen"
        value = raw["on_screen_text"]
        assert isinstance(value, list) and value == [v for v in ON_SCREEN_TEXT if v in value], raw["id"]
        assert resolve_profile(raw["id"])["on_screen_text"] == value, raw["id"]
    used = {v for raw in RAW_PRESETS for v in raw["on_screen_text"]}
    assert used == set(ON_SCREEN_TEXT)


def test_voice_cards_suggest_subtitles_and_loops_open_on_picture():
    for pid in PRESET_IDS:
        profile = resolve_profile(pid)
        voiced = profile["vo_mode"] in ("narration", "dialogue")
        assert ("subtitles" in profile["on_screen_text"]) == voiced, pid
        if profile["template"] == "mood_loop":
            assert "title_card" not in profile["on_screen_text"], pid


def test_freeform_suggests_no_text_and_hands_out_a_copy():
    profile = resolve_profile(None)
    assert profile["on_screen_text"] == []
    profile["on_screen_text"].append("title_card")
    assert FREEFORM_PROFILE["on_screen_text"] == [] and resolve_profile(None)["on_screen_text"] == []


@pytest.mark.parametrize("bad", [["end_card", "title_card"], ["subtitles", "subtitles"], ["captions"]])
def test_on_screen_text_is_validated_on_load(tmp_path, bad):
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["presets"][0]["on_screen_text"] = bad
    path = tmp_path / "presets.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="on_screen_text"):
        load_genre_presets(path)


def test_a_card_without_the_field_suggests_nothing(tmp_path):
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    del data["presets"][0]["on_screen_text"]
    path = tmp_path / "presets.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    preset = load_genre_presets(path).get_preset(data["presets"][0]["id"])
    assert preset.on_screen_text == [] and preset.profile()["on_screen_text"] == []


# ------------------------------------------------------------------ the audio plan's cards

def test_cards_are_optional_and_validated(tmp_path):
    plan = normalize_plan({}, tmp_path)
    assert "title_card" not in plan and "end_card" not in plan
    plan = normalize_plan({"title_card": {"text": "  The   Lighthouse\nKeeper "}, "end_card": {"text": "The End",
                                                                                         "visible_s": 4, "fade_s": 0}},
                          tmp_path)
    assert plan["title_card"] == {"text": "The Lighthouse Keeper", "visible_s": 2.5, "fade_s": 0.4}
    assert plan["end_card"] == {"text": "The End", "visible_s": 4.0, "fade_s": 0.0}

    def bad(card):
        return _raises(normalize_plan, {"title_card": card}, tmp_path)

    assert "object with a text string" in bad("The Lighthouse")
    assert "object with a text string" in bad({"text": 7})
    assert "title_card.text is empty" in bad({"text": " \t\n"})
    assert "81 characters (at most 80)" in bad({"text": "x" * 81})
    assert "title_card.visible_s=0.2 out of range" in bad({"text": "Hi", "visible_s": 0.2})
    assert "title_card.visible_s=11.0 out of range" in bad({"text": "Hi", "visible_s": 11})
    assert "title_card.fade_s=3.0 out of range" in bad({"text": "Hi", "fade_s": 3})
    assert "more than half of visible_s" in bad({"text": "Hi", "visible_s": 1.0, "fade_s": 0.6})
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"png")
    msg = _raises(normalize_plan, {"end_card": {"text": "The End"}, "logo": {"path": str(logo)}}, tmp_path)
    assert "end_card and logo" in msg
    assert normalize_plan({"title_card": {"text": "Hi"}, "logo": {"path": str(logo)}}, tmp_path)["title_card"]


def test_a_card_changes_the_delivery_key_and_plans_without_cards_keep_theirs(tmp_path):
    raw = {"vo": {"voice_id": "v", "lines": [{"id": "L01", "at": 0.5, "text": "Hello.", "path": "vo.wav"}]}}
    plain = normalize_plan(raw, tmp_path)
    assert set(plain) == set(normalize_plan({**raw, "subtitles": {"burn": True}}, tmp_path))
    assert audio_mix.input_key("m", 6.0, plain, {}) == audio_mix.input_key(
        "m", 6.0, normalize_plan({**raw, "subtitles": {"burn": True}}, tmp_path), {})
    titled = normalize_plan({**raw, "title_card": {"text": "Hello"}}, tmp_path)
    assert audio_mix.input_key("m", 6.0, titled, {}) != audio_mix.input_key("m", 6.0, plain, {})


def test_card_windows_and_overlap():
    title, end = {"text": "T", "visible_s": 2.5, "fade_s": 0.4}, {"text": "E", "visible_s": 2.0, "fade_s": 0.4}
    assert audio_mix.text_card_windows({}, 6.0) == {}
    assert audio_mix.text_card_windows({"title_card": title, "end_card": end}, 6.0) == {
        "title_card": (0.0, 2.5), "end_card": (4.0, 6.0)}
    assert audio_mix.text_card_windows({"title_card": title}, 1.5) == {"title_card": (0.0, 1.5)}
    msg = _raises(audio_mix.text_card_windows, {"title_card": title, "end_card": end}, 4.0)
    assert "overlap on the 4.00s picture" in msg


@pytest.mark.parametrize("size", [(1080, 1920), (1920, 1080), (1440, 1080), (540, 960)])
def test_card_text_size_follows_the_shorter_side(size):
    w, h = size
    lines, font = audio_mix.text_card_layout("The Lighthouse", w, h)
    assert lines == ["The Lighthouse"]
    assert font == round(min(w, h) * audio_mix.TEXT_CARD_FONT_FRAC)
    long_text = "A Very Long Title About The Lighthouse Keeper And The Paper Kite On A Windy Night"[:80]
    lines, font = audio_mix.text_card_layout(long_text, w, h)
    assert 1 <= len(lines) <= audio_mix.TEXT_CARD_MAX_LINES
    assert " ".join(lines) == long_text
    assert all(len(line) * font * 0.6 <= w * 0.84 + font for line in lines), (lines, font)
    assert font >= round(min(w, h) * audio_mix.TEXT_CARD_MIN_FONT_FRAC)


@pytest.mark.parametrize("canvas", ["9:16", "16:9", "4:3"])
def test_subtitles_are_readable_and_clear_of_the_card_band(canvas):
    """Each canvas' subtitle style: big enough for its frame, the longest line fits across it, and
    two lines stay in the bottom of the frame, below where the centred title / end card sits."""
    w, h = aspect.canvas(canvas)
    subs, (rw, rh) = audio_mix.subtitle_settings(dict(audio_mix.DEFAULT_SUBTITLES), w, h)
    assert (rw, rh) == (w, h)
    assert subs["fontsize"] / min(w, h) >= 0.05, subs
    assert subs["max_line_chars"] * subs["fontsize"] * 0.55 <= w * 0.92, subs
    block_top = h - subs["margin_v"] - 2 * 1.25 * subs["fontsize"]
    lines, font = audio_mix.text_card_layout("x " * 40, w, h)
    card_bottom = (h + round(font * 1.55) * len(lines)) / 2 + round(font * 0.18)
    assert block_top > card_bottom, (canvas, block_top, card_bottom)


# ------------------------------------------------------------------ rendering (real ffmpeg)

def _grey(path, t: float) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=135:240,format=gray", "-f", "rawvideo", "-"],
                         capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(out, np.uint8).astype(np.int16)


@pytest.fixture(scope="module")
def carded(tmp_path_factory):
    """One delivery with a title card, an end card and burned subtitles on a moving picture."""
    import asyncio
    import os

    tmp = tmp_path_factory.mktemp("cards")
    old = os.environ.get("EPISODES_DIR")
    os.environ["EPISODES_DIR"] = str(tmp / "data" / "episodes")
    try:
        mute, plan = episode(tmp, "ep71", seconds=6.0)
        plan = normalize_plan({**json.loads((mute.parent.parent / "audio" / "audio_plan.json").read_text()),
                               "title_card": {"text": "The Paper Kite", "visible_s": 1.5},
                               "end_card": {"text": "The End", "visible_s": 1.5}}, mute.parent.parent)
        manifest = asyncio.run(audio_mix.mix_and_deliver("ep71", str(mute), plan))
        qc = asyncio.run(audio_mix.delivery_qc(manifest, plan))
        yield {"mute": mute, "plan": plan, "manifest": manifest, "qc": qc}
    finally:
        if old is None:
            os.environ.pop("EPISODES_DIR", None)
        else:
            os.environ["EPISODES_DIR"] = old


@needs_ffmpeg
def test_cards_are_drawn_into_every_picture_deliverable(carded):
    m, qc = carded["manifest"], carded["qc"]
    assert qc["verdict"] == "PASS", qc["failed"]
    assert m["subtitles_burned"] is True
    assert m["title_card"]["text"] == "The Paper Kite" and m["title_card"]["lines"] == ["The Paper Kite"]
    assert (m["title_card"]["start_s"], m["title_card"]["end_s"]) == (0.0, 1.5)
    assert (m["end_card"]["start_s"], m["end_card"]["end_s"]) == (4.5, 6.0)
    fps = 30
    assert m["title_card"]["frames"] == [0, 1.5 * fps + 1] and m["end_card"]["frames"] == [4.5 * fps - 1, 6 * fps]
    checks = {c["name"]: c for c in qc["checks"]}
    for name in ("nosubs", "phone", "master_subs"):
        for kind in ("title_card", "end_card"):
            check = checks[f"{name}_{kind}_drawn"]
            assert check["ok"] and check["value"] >= audio_mix.TEXT_CARD_MIN_CHANGED, check
        assert checks[f"{name}_frame_count"]["ok"], checks[f"{name}_frame_count"]
    encodes = {e["what"]: e for e in m["picture_sync"]["encodes"]}
    assert set(encodes) >= {"picture_cards", "subs"} and any(w.startswith("phone@") for w in encodes)
    for e in encodes.values():
        assert e["attempts"][-1]["misaligned"] == 0, e
        assert e["skipped_card_frames"] == [m["title_card"]["frames"], m["end_card"]["frames"]], e


@needs_ffmpeg
def test_cards_only_touch_their_own_frames(carded):
    nosubs, mute = carded["manifest"]["paths"]["nosubs"], carded["mute"]
    title = np.abs(_grey(nosubs, 0.75) - _grey(mute, 0.75)).mean()
    middle = np.abs(_grey(nosubs, 3.0) - _grey(mute, 3.0)).mean()
    end = np.abs(_grey(nosubs, 5.5) - _grey(mute, 5.5)).mean()
    assert title > 4 * middle and end > 4 * middle, (title, middle, end)
    assert middle < 2.0, middle


@needs_ffmpeg
@pytest.mark.asyncio
async def test_delivery_qc_fails_when_a_card_is_missing(carded):
    m, plan = carded["manifest"], carded["plan"]
    outputs = {**m["outputs"], "nosubs": {**m["outputs"]["nosubs"], "path": str(carded["mute"])}}
    qc = await audio_mix.delivery_qc({**m, "outputs": outputs}, plan)
    assert "nosubs_title_card_drawn" in qc["failed"] and "nosubs_end_card_drawn" in qc["failed"], qc["failed"]
    qc = await audio_mix.delivery_qc({**m, "title_card": None}, plan)
    assert qc["verdict"] == "FAIL" and "title_card_drawn" in qc["failed"], qc["failed"]


@needs_ffmpeg
@pytest.mark.asyncio
async def test_overlapping_cards_are_refused_before_any_encode(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    mute, plan = episode(tmp_path, "ep72", seconds=4.0)
    plan = {**plan, "title_card": {"text": "Hello", "visible_s": 2.5, "fade_s": 0.4},
            "end_card": {"text": "Bye", "visible_s": 2.5, "fade_s": 0.4}}
    with pytest.raises(AudioInputError, match="overlap on the 4.00s picture"):
        await audio_mix.mix_and_deliver("ep72", str(mute), plan)
    assert not Path(audio_mix.delivery_paths("ep72")["dir"]).exists()


def _srt_cues(path) -> list[tuple[float, float, str]]:
    def secs(stamp: str) -> float:
        h, m, rest = stamp.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000

    blocks = Path(path).read_text(encoding="utf-8").strip().split("\n\n")
    cues = []
    for block in blocks:
        rows = block.splitlines()
        a, b = re.match(r"(\S+) --> (\S+)", rows[1]).groups()
        cues.append((secs(a), secs(b), " ".join(rows[2:])))
    return cues


@needs_ffmpeg
@pytest.mark.asyncio
async def test_subtitles_off_still_delivers_the_srt_on_the_vo_timing(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    mute, plan = episode(tmp_path, "ep73", seconds=6.0)
    plan = {**plan, "subtitles": {**plan["subtitles"], "burn": False}}
    m = await audio_mix.mix_and_deliver("ep73", str(mute), plan)
    assert m["subtitles_burned"] is False
    assert "master_subs" not in m["outputs"] and not Path(m["paths"]["master_subs"]).exists()
    srt = m["outputs"]["srt"]["path"]
    cues = _srt_cues(srt)
    lines = m["vo_lines"]
    assert [c[2] for c in cues] == ["First line here.", "Second line."]
    for (start, end, _), line in zip(cues, lines):
        assert abs(start - line["at"]) <= 0.001 and abs(end - (line["at"] + line["dur"])) <= 0.001, (cues, lines)
    qc = await audio_mix.delivery_qc(m, plan)
    assert qc["verdict"] == "PASS", qc["failed"]
    assert not any(c["name"].startswith("master_subs") for c in qc["checks"])
    assert {c["name"] for c in qc["checks"]} >= {"subtitle_timing", "subtitle_cue_count"}


# ------------------------------------------------------------------ the draft's defaults

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient

    from api.main import app

    test_client = TestClient(app)
    login = test_client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert login.status_code == 200
    test_client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return test_client


def _draft(client, archetype_id, target):
    res = client.post("/api/episodes/draft-from-idea",
                      json={"idea": IDEA, "archetype_id": archetype_id, "target_duration": target})
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize("archetype_id", PRESET_IDS + [None])
def test_the_draft_carries_the_card_text_defaults(client, archetype_id, tmp_path):
    profile = resolve_profile(archetype_id)
    data = _draft(client, archetype_id, profile["default_length_s"])
    want = profile["on_screen_text"]
    assert data["preset"]["on_screen_text"] == want
    assert data["title"] == "The Lighthouse"
    plan = data["audio_plan_json"]
    has_lines = bool((plan.get("vo") or {}).get("lines"))
    assert has_lines == (profile["vo_mode"] in ("narration", "dialogue")), archetype_id
    if has_lines:
        assert plan["subtitles"] == {"burn": "subtitles" in want}
    else:
        assert "subtitles" not in plan
    assert plan.get("title_card") == ({"text": "The Lighthouse"} if "title_card" in want else None)
    assert plan.get("end_card") == ({"text": "The End"} if "end_card" in want else None)
    normalized = normalize_plan(plan, tmp_path)
    assert normalized["subtitles"]["burn"] == ("subtitles" in want or not has_lines)


def test_short_drafts_fit_both_cards():
    profile = resolve_profile("cute-greeting")
    for total in (3.0, 6.0, 9.0):
        plan = genre_drafts.apply_on_screen_text({}, {**profile, "on_screen_text": ["title_card", "end_card"]},
                                                 "Hi", total)
        timing = genre_drafts.card_timing(total)
        assert plan["title_card"] == {"text": "Hi", **timing} and plan["end_card"] == {"text": "The End", **timing}
        windows = audio_mix.text_card_windows(normalize_plan(plan, Path(".")), total)
        assert windows["title_card"][1] <= windows["end_card"][0]
        assert timing["fade_s"] * 2 < timing["visible_s"]
    assert genre_drafts.card_timing(10.0) == {} and genre_drafts.card_timing(3.0) == {"visible_s": 0.8, "fade_s": 0.2}
    assert genre_drafts.card_timing(9.0) == {"visible_s": 2.3, "fade_s": 0.4}


def _llm(result: dict):
    completion = MagicMock()
    completion.choices = [MagicMock(message=MagicMock(content=json.dumps(result)))]
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=completion)
    return client


@pytest.mark.parametrize("title, shown", [('  "Mixtape Summer" ', "Mixtape Summer"), (None, "The Lighthouse"),
                                          ("", "The Lighthouse"), (42, "The Lighthouse")])
def test_the_llm_names_the_episode(client, monkeypatch, title, shown):
    from hfvg.config import config

    monkeypatch.delenv("DRAFT_LLM_FAKE")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    card = "original-series"
    profile = resolve_profile(card)
    target = profile["default_length_s"]
    files = genre_drafts.fake_draft(profile, IDEA, "ep42abc", target, "watercolor")
    result = {k: files[k] for k in ("beatmap_md", "shots_json", "refs_json", "prompt_kit_json", "audio_script")}
    if title is not None:
        result["title"] = title
    mock = _llm(result)
    with patch("openai.AsyncOpenAI", return_value=mock):
        data = _draft(client, card, target)
    assert data["title"] == shown
    assert data["audio_plan_json"]["title_card"] == {"text": shown}
    assert data["audio_plan_json"]["end_card"] == {"text": "The End"}
    system = mock.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    assert '"title": a short original episode title' in system
