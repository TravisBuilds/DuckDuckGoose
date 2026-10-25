"""Round 4 audio activities + ElevenLabs provider + caps (G-AUD-1b, G-CAPS-1).

Activities run in ActivityEnvironment (they heartbeat). Live provider paths hit respx mocks of the
documented REST endpoints; nothing reaches the network and nothing is spent."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from hfvg.activities import audio as act
from hfvg.budget import BudgetLedger
from hfvg.providers import elevenlabs as el

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")

API = "https://api.elevenlabs.io"
MP3 = b"ID3" + b"\x00" * 256  # bytes only: the provider stores what the API returns


class HB:
    def __init__(self):
        self.env = ActivityEnvironment()
        self.beats = []
        self.env.on_heartbeat = lambda *d: self.beats.append(d[0] if d else None)

    async def run(self, fn, *args):
        return await self.env.run(fn, *args)


async def _raises(coro) -> ApplicationError | None:
    try:
        await coro
    except ApplicationError as e:
        return e
    except Exception as e:  # a raw exception is retryable in Temporal: report it as a wrong outcome
        return ApplicationError(f"unexpected {type(e).__name__}: {e}", type=f"raw:{type(e).__name__}")
    return None


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    return tmp_path


async def _init_db():
    from hfvg.studio_db import init_studio_db
    await init_studio_db(os.environ["DATABASE_PATH"])


# ------------------------------------------------------------------ caps (G-CAPS-1)

def test_caps_rows_match_official_numbers_and_costs():
    from hfvg import model_caps
    reg = model_caps.get_registry()
    assert reg.resolve("eleven_v3").limit("text_max_chars").value == 5000
    assert reg.resolve("eleven_flash_v2_5").limit("text_max_chars").value == 40000
    assert reg.resolve("eleven_multilingual_v2").limit("text_max_chars").value == 10000
    for mid in ("music_v1", "music_v2", "music_v2_5"):
        caps = reg.resolve(mid)
        assert caps.limit("duration_ms").value == {"min": 3000, "max": 300000}
        assert caps.upstream["endpoint"] == "POST /v1/music"
    raw = json.loads((Path(model_caps.__file__).parent / "data" / "model_caps.json").read_text())
    for mid, cost in el.AUDIO_COSTS.items():
        row = raw["models"][mid]
        assert row["cost"]["usd"] == cost["usd"], mid
        assert row["cost"]["unit"] == {"1k_chars": "1k_chars", "minute": "minute"}[cost["unit"]], mid
        assert all(raw["sources"][s]["url"].startswith("https://elevenlabs.io/") for s in row["cost"]["sources"])
    assert el.canonical_audio_model("eleven_music") == "music_v1"


def test_char_based_cost_never_zero():
    v3 = el.estimate_usd("tts", {"model": "eleven_v3", "text": "x" * 2500})
    flash = el.estimate_usd("tts", {"model": "eleven_flash_v2_5", "text": "x" * 2500})
    assert abs(v3 - 0.20) < 1e-9 and abs(flash - 0.10) < 1e-9
    assert el.estimate_usd_micros("tts", {"model": "eleven_v3", "text": "a"}) >= 1
    assert abs(el.estimate_usd("music", {"model": "music_v1", "duration": 30}) - 0.075) < 1e-9
    assert abs(el.estimate_usd("sfx", {"model": "sound_generation", "duration": 5}) - 0.01) < 1e-9
    for bad in ({"model": "eleven_v3"}, {"model": "eleven_v3", "text": ""}):
        try:
            el.estimate_usd("tts", bad)
            raised = False
        except ValueError:
            raised = True
        assert raised, f"cost estimated without text: {bad}"


def test_live_provider_requires_api_key(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    try:
        el.ElevenLabsProvider(dry_run=False)
        raised = False
    except ValueError:
        raised = True
    assert raised


# ------------------------------------------------------------------ provider REST (respx)

@respx.mock
async def test_provider_tts_writes_durable_file_with_real_cost(tmp_path):
    route = respx.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=httpx.Response(200, content=MP3))
    p = el.ElevenLabsProvider(api_key="k", dry_run=False, output_dir=tmp_path)
    job = await p.submit_audio("Hello there.", "voice-1", model="eleven_v3", out_path=tmp_path / "l.mp3")
    req = route.calls.last.request
    body = json.loads(req.content)
    assert body == {"text": "Hello there.", "model_id": "eleven_v3"}
    assert req.headers["xi-api-key"] == "k" and req.url.params["output_format"] == "mp3_44100_128"
    st = await p.get_job_status(job)
    assert st.output_url == (tmp_path / "l.mp3").resolve().as_uri()
    assert (tmp_path / "l.mp3").read_bytes() == MP3
    assert st.cost == el.estimate_usd("tts", {"model": "eleven_v3", "text": "Hello there."}) > 0


@respx.mock
async def test_provider_music_uses_compose_endpoint(tmp_path):
    route = respx.post(f"{API}/v1/music").mock(return_value=httpx.Response(200, content=MP3))
    p = el.ElevenLabsProvider(api_key="k", dry_run=False, output_dir=tmp_path)
    job = await p.submit_music("calm piano", 12.5, model="eleven_music")
    body = json.loads(route.calls.last.request.content)
    assert body.get("music_length_ms") == 12500 and body["model_id"] == "music_v1" and body["prompt"] == "calm piano"
    st = await p.get_job_status(job)
    assert st.output_url.startswith("file://") and st.cost > 0


@respx.mock
async def test_provider_sfx_and_errors(tmp_path):
    route = respx.post(f"{API}/v1/sound-generation").mock(return_value=httpx.Response(200, content=MP3))
    p = el.ElevenLabsProvider(api_key="k", dry_run=False, output_dir=tmp_path)
    await p.submit_sfx("door slam", duration=2.0)
    body = json.loads(route.calls.last.request.content)
    assert body["duration_seconds"] == 2.0 and body["model_id"] == "eleven_text_to_sound_v2"
    respx.post(f"{API}/v1/sound-generation").mock(return_value=httpx.Response(429, text="busy"))
    try:
        await p.submit_sfx("door slam", duration=2.0)
        raised = False
    except el.ElevenLabsError:
        raised = True
    assert raised
    respx.get(f"{API}/v1/user/subscription").mock(
        return_value=httpx.Response(200, json={"character_count": 1200, "character_limit": 100000}))
    assert await p.get_balance() == 98800.0


# ------------------------------------------------------------------ activities

@needs_ffmpeg
async def test_generate_voiceover_dry_run(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    hb = HB()
    with respx.mock(assert_all_called=False) as mock:
        res = await hb.run(act.generate_voiceover, "ep01", "A line of text.", "voice-1")
        assert not mock.calls, "dry run reached the provider"
    assert res["dry_run"] is True and res["source"] == "dry_run_stand_in" and Path(res["path"]).is_file()
    assert "/audio/generated/vo/" in res["path"]
    assert any(isinstance(b, dict) and b.get("stage") == "voiceover" for b in hb.beats)


async def test_generate_voiceover_locked_path(live, tmp_path):
    locked = tmp_path / "L01.wav"
    locked.write_bytes(b"RIFF....WAVE")
    res = await HB().run(act.generate_voiceover, "ep01", "", "", "eleven_v3", "L01", str(locked))
    assert res["source"] == "locked" and res["path"] == str(locked) and res["dry_run"] is False
    assert not Path(os.environ["DATABASE_PATH"]).exists() or not await BudgetLedger(os.environ["DATABASE_PATH"]).get_jobs("ep01")


async def test_missing_inputs_are_non_retryable_live(live, tmp_path):
    hb = HB()
    cases = [
        (act.generate_voiceover, ("ep01", "", "", "eleven_v3", "L01", str(tmp_path / "nope.wav")), "MissingAudioInput"),
        (act.generate_sfx, ("ep01", "", 2.0, "S01", str(tmp_path / "nope.wav")), "MissingAudioInput"),
        (act.generate_music, ("ep01", "", 30.0, "music_v1", "music", str(tmp_path / "nope.wav")), "MissingAudioInput"),
        (act.generate_voiceover, ("ep01", "", "voice", "eleven_v3", "L02", None), "MissingAudioInput"),
        (act.load_audio_plan, ("ep01", None), "MissingAudioPlan"),
        (act.mix_and_deliver, ("ep01", str(tmp_path / "mute.mp4"), {"vo": {"lines": []}}, None), "MissingPicture"),
    ]
    for fn, args, kind in cases:
        err = await _raises(hb.run(fn, *args))
        assert err is not None, f"{fn.__name__}{args} did not raise"
        assert err.non_retryable and err.type == kind, (fn.__name__, err.type, err.non_retryable)


async def test_dry_run_plan_and_delivery_are_dry(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    hb = HB()
    plan = await hb.run(act.load_audio_plan, "ep01", None)
    assert plan["plan_found"] is False and plan["vo"]["lines"] == []
    m = await hb.run(act.mix_and_deliver, "ep01", "/nonexistent/mute.mp4", plan, None)
    assert m["dry_run"] is True and m["paths"]["dir"].endswith("ep01/delivery")
    qc = await hb.run(act.check_delivery_qc, "ep01", m, plan, None)
    assert qc["verdict"] == "DRY_RUN" and qc["blocking"] is False


async def test_preflight_refuses_over_limit_before_any_spend(live):
    await _init_db()
    hb = HB()
    with respx.mock(assert_all_called=False) as mock:
        mock.post(url__startswith=API).mock(return_value=httpx.Response(200, content=MP3))
        cases = [
            (act.generate_voiceover, ("ep01", "x" * 5001, "voice-1", "eleven_v3", "L01", None)),
            (act.generate_voiceover, ("ep01", "hello", "voice-1", "eleven_turbo_v2_5", "L02", None)),
            (act.generate_voiceover, ("ep01", "hello", "voice-1", "not_a_model", "L03", None)),
            (act.generate_music, ("ep01", "piano", 301.0, "music_v1", "music", None)),
            (act.generate_sfx, ("ep01", "thud", 31.0, "S01", None)),
        ]
        for fn, args in cases:
            err = await _raises(hb.run(fn, *args))
            assert err is not None and err.type == "PreflightRefused" and err.non_retryable, (args, err)
        assert not mock.calls, "a refused request reached the provider"
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    assert await ledger.get_jobs("ep01") == []
    # 5,000 characters is the documented limit: allowed (and billed per character)
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=httpx.Response(200, content=MP3))
        ok = await hb.run(act.generate_voiceover, "ep01", "y" * 5000, "voice-1", "eleven_v3", "L04", None)
    assert ok["usd_micros"] == 400_000 and ok["characters"] == 5000


async def test_live_generation_reserves_commits_and_reuses(live):
    await _init_db()
    hb = HB()
    text = "A short narrated line."
    with respx.mock() as mock:
        route = mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=httpx.Response(200, content=MP3))
        res = await hb.run(act.generate_voiceover, "ep01", text, "voice-1", "eleven_v3", "L01", None)
        again = await hb.run(act.generate_voiceover, "ep01", text, "voice-1", "eleven_v3", "L01", None)
        assert route.call_count == 1, "a retried / repeated line was paid twice"
    assert res["source"] == "generated" and again["source"] == "generated_reused" and again["path"] == res["path"]
    expected = el.estimate_usd_micros("tts", {"model": "eleven_v3", "text": text})
    assert res["usd_micros"] == expected > 0
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    line = await ledger.get_line_status("ep01", "el_vo_takes")
    assert line["spent"] == expected and line["reserved"] == 0
    hold = await ledger.get_round_hold(res["job_id"])
    assert hold["round_id"] == "AUDIO" and hold["status"] != "held", hold
    jobs = await ledger.get_jobs("ep01")
    assert [j["job_id"] for j in jobs] == [res["job_id"]]


async def test_live_generation_failure_releases_hold(live):
    await _init_db()
    with respx.mock() as mock:
        mock.post(f"{API}/v1/music").mock(return_value=httpx.Response(500, text="boom"))
        try:
            await HB().run(act.generate_music, "ep01", "calm piano", 20.0, "music_v1", "music", None)
            raised = False
        except Exception:
            raised = True
    assert raised
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    line = await ledger.get_line_status("ep01", "el_music_one_take")
    assert line["spent"] == 0 and line["reserved"] == 0
    rounds = await ledger.get_round_status("ep01")
    assert rounds["rounds"].get("AUDIO", {}).get("total_usd_micros", 0) == 0, rounds


async def test_round_cap_refuses_audio_spend(live, monkeypatch):
    await _init_db()
    monkeypatch.setenv("ROUND_CAP_USD", "0.000001")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=httpx.Response(200, content=MP3))
        err = await _raises(HB().run(act.generate_voiceover, "ep01", "x" * 400, "voice-1", "eleven_v3", "L01", None))
        assert err is not None and err.type in ("RoundBudgetError", "BudgetExceeded") and err.non_retryable, err
        assert route.call_count == 0
