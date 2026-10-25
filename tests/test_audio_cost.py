"""G-AUD-COST: actual ElevenLabs cost from the ``character-cost`` response header.

- reserve the documented credit upper bound (>= the API USD estimate) before the call;
- commit the actual: header credits x ELEVENLABS_USD_PER_CREDIT; release the remainder;
- the commit is <= the reservation for every documented price (the live eleven_v3 case: 71
  characters sent, character-cost 28);
- characters / credits / cost_source persisted on job_ledger (migration for older DBs);
- no header: commit the estimate, flagged cost_source="estimate";
- TTS, SFX and music. Provider calls hit respx mocks of the documented endpoints: no network,
  nothing spent."""

import os

import aiosqlite
import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from hfvg.activities import audio as act
from hfvg.budget import BudgetLedger
from hfvg.providers import elevenlabs as el

API = "https://api.elevenlabs.io"
MP3 = b"ID3" + b"\x00" * 256
LINE_71 = "The kettle clicks off and steam curls past the window into the cold air"  # 71 chars


@pytest.fixture
async def live(monkeypatch):
    from hfvg.studio_db import init_studio_db
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    await init_studio_db(os.environ["DATABASE_PATH"])


def _ledger() -> BudgetLedger:
    return BudgetLedger(os.environ["DATABASE_PATH"])


async def _txns(job_id: str) -> list[tuple[str, int]]:
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        async with db.execute("SELECT txn_type, amount FROM budget_transactions WHERE job_id = ? ORDER BY rowid",
                              (job_id,)) as cur:
            return [tuple(r) for r in await cur.fetchall()]


async def _job_row(job_id: str) -> dict:
    jobs = [j for j in await _ledger().get_jobs("ep01") if j["job_id"] == job_id]
    assert len(jobs) == 1, jobs
    return jobs[0]


def _audio(headers: dict | None = None) -> httpx.Response:
    return httpx.Response(200, content=MP3, headers=headers or {})


async def _run(fn, *args):
    return await ActivityEnvironment().run(fn, *args)


# ------------------------------------------------------------------ pricing helpers

PAID_PLANS_USD_PER_CREDIT = {  # https://elevenlabs.io/pricing, checked 2026-10-25
    "starter": 6 / 30_000, "creator": 22 / 121_000, "pro": 99 / 600_000,
    "scale": 299 / 1_800_000, "business": 990 / 6_000_000,
}


def test_default_usd_per_credit_is_the_highest_paid_plan_rate(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    rate = el.usd_per_credit()
    assert rate == pytest.approx(0.0002)
    for plan, plan_rate in PAID_PLANS_USD_PER_CREDIT.items():
        assert rate >= plan_rate - 1e-12, f"default under-states the {plan} plan's credit price"


def test_usd_per_credit_is_configurable_and_fails_closed(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_USD_PER_CREDIT", "0.000182")
    assert el.usd_per_credit() == pytest.approx(0.000182)
    assert el.credits_to_usd_micros(28) == 5_096  # 28 x $0.000182
    for bad in ("0", "-0.0002", "abc", "nan", "inf"):
        monkeypatch.setenv("ELEVENLABS_USD_PER_CREDIT", bad)
        err = None
        try:
            el.usd_per_credit()
        except Exception as e:  # noqa: BLE001 - the type is asserted below
            err = e
        assert isinstance(err, ValueError), f"rate {bad!r} must be refused, got {err!r}"


def test_credits_to_usd_micros_rounds_up(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    assert el.credits_to_usd_micros(28) == 5_600
    assert el.credits_to_usd_micros(0.5) == 100
    assert el.credits_to_usd_micros(2.5025) == 501  # 500.5 micro-dollars: rounds up, never down
    assert el.credits_to_usd_micros(0.001) == 1  # never 0 for a billed request


def test_character_cost_header_parsing():
    assert el.parse_character_cost(httpx.Headers({"Character-Cost": "28"})) == 28.0
    assert el.parse_character_cost(httpx.Headers({"character-cost": " 12.5 "})) == 12.5
    assert el.parse_character_cost({"CHARACTER-COST": "7"}) == 7.0
    for headers in (None, {}, {"character-cost": ""}, {"character-cost": "n/a"},
                    {"character-cost": "-3"}, {"character-cost": "nan"}, {"x-other": "28"}):
        assert el.parse_character_cost(headers) is None, headers


def test_reservation_is_the_documented_upper_bound(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    assert len(LINE_71) == 71
    tts = {"model": "eleven_v3", "text": LINE_71}
    assert el.estimate_usd_micros("tts", tts) == 5_680          # API price: $0.08 / 1k chars
    assert el.reservation_usd_micros("tts", tts) == 14_200      # 71 credits x $0.0002 (1 credit/char)
    assert el.reservation_usd_micros("sfx", {"duration": 5.0}) == 40_000    # 40 credits/s
    # auto duration: 200 credits ($0.04) < the API estimate's 30 s billing floor ($0.06)
    assert el.reservation_usd_micros("sfx", {"duration": None}) == 60_000
    assert el.reservation_usd_micros("sfx", {"duration": 30.0}) == 240_000
    assert el.reservation_usd_micros("music", {"model": "music_v1", "duration": 20.0}) == 60_000  # 900/min
    # never below the API price estimate either
    for kind, params in (("tts", tts), ("sfx", {"duration": 5.0}), ("music", {"model": "music_v1", "duration": 20.0})):
        assert el.reservation_usd_micros(kind, params) >= el.estimate_usd_micros(kind, params)


# ------------------------------------------------------------------ provider: header -> job metadata

@respx.mock
async def test_provider_reads_character_cost_for_tts_sfx_music(tmp_path):
    p = el.ElevenLabsProvider(dry_run=False, output_dir=tmp_path, api_key="k")
    respx.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio({"character-cost": "28"}))
    respx.post(f"{API}/v1/sound-generation").mock(return_value=_audio({"Character-Cost": "200"}))
    respx.post(f"{API}/v1/music").mock(return_value=_audio({"character-cost": "300"}))
    tts = await p.get_job_status(await p.submit_audio(LINE_71, "voice-1", model="eleven_v3"))
    sfx = await p.get_job_status(await p.submit_sfx("a soft knock", duration=5.0))
    mus = await p.get_job_status(await p.submit_music("calm piano", 20.0))
    assert tts.raw == {"credits": 28.0, "characters": 71, "cost_source": "provider_header"}
    assert tts.cost == pytest.approx(0.0056)
    assert sfx.raw == {"credits": 200.0, "characters": None, "cost_source": "provider_header"}
    assert sfx.cost == pytest.approx(0.04)
    assert mus.raw == {"credits": 300.0, "characters": None, "cost_source": "provider_header"}
    assert mus.cost == pytest.approx(0.06)


@respx.mock
async def test_provider_without_header_falls_back_to_estimate_flagged(tmp_path):
    p = el.ElevenLabsProvider(dry_run=False, output_dir=tmp_path, api_key="k")
    respx.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio())
    job = await p.get_job_status(await p.submit_audio(LINE_71, "voice-1", model="eleven_v3"))
    assert job.raw == {"credits": None, "characters": 71, "cost_source": "estimate"}
    assert job.cost == pytest.approx(el.estimate_usd("tts", {"model": "eleven_v3", "text": LINE_71}))


# ------------------------------------------------------------------ activities: reserve, commit actual, release

async def test_tts_commits_header_credits_and_releases_the_rest(live):
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio({"character-cost": "28"}))
        res = await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
    assert res["reserved_usd_micros"] == 14_200
    assert res["usd_micros"] == 5_600 <= res["reserved_usd_micros"]
    assert res["cost_source"] == "provider_header" and res["credits"] == 28.0 and res["characters"] == 71
    assert res["over_reservation"] is False
    line = await _ledger().get_line_status("ep01", "el_vo_takes")
    assert line["spent"] == 5_600 and line["reserved"] == 0, line
    assert await _txns(res["job_id"]) == [("reserve", 14_200), ("commit", 5_600), ("release", 8_600)]
    row = await _job_row(res["job_id"])
    assert row["status"] == "committed" and row["usd_micros"] == 5_600
    assert (row["characters"], row["credits"], row["cost_source"]) == (71, 28.0, "provider_header")
    hold = await _ledger().get_round_hold(res["job_id"])
    assert hold["status"] == "committed" and hold["usd_micros"] == 5_600, hold


async def test_job_row_is_reserved_during_the_provider_call(live):
    """The job row exists (status reserved, reservation amount) while the provider is called."""
    seen = {}

    def respond(request):
        import sqlite3
        with sqlite3.connect(os.environ["DATABASE_PATH"]) as db:
            seen["rows"] = db.execute("SELECT status, usd_micros FROM job_ledger").fetchall()
        return _audio({"character-cost": "28"})

    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(side_effect=respond)
        await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
    assert seen["rows"] == [("reserved", 14_200)], seen


async def test_worst_case_one_credit_per_char_still_within_reservation(live):
    """Multilingual v2 bills 1 credit per character: the commit equals, never exceeds, the hold."""
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio({"character-cost": "71"}))
        res = await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_multilingual_v2", "L71", None)
    assert res["usd_micros"] == res["reserved_usd_micros"] == 14_200
    assert res["over_reservation"] is False
    line = await _ledger().get_line_status("ep01", "el_vo_takes")
    assert line["spent"] == 14_200 and line["reserved"] == 0


async def test_configured_rate_is_used_for_reserve_and_commit(live, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_USD_PER_CREDIT", "0.0001")
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio({"character-cost": "28"}))
        res = await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
    assert res["reserved_usd_micros"] == 7_100   # max($0.00568 API estimate, 71 x $0.0001)
    assert res["usd_micros"] == 2_800


async def test_bad_rate_refused_before_any_ledger_row(live, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_USD_PER_CREDIT", "0")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=API).mock(return_value=_audio({"character-cost": "28"}))
        try:
            await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
            err = None
        except Exception as e:  # noqa: BLE001 - the type is asserted below
            err = e
        assert route.call_count == 0
    assert isinstance(err, ApplicationError) and err.type == "InvalidCostConfig" and err.non_retryable, repr(err)
    assert await _ledger().get_jobs("ep01") == []


async def test_missing_header_commits_the_estimate_flagged(live):
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio())
        res = await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
    estimate = el.estimate_usd_micros("tts", {"model": "eleven_v3", "text": LINE_71})
    assert res["cost_source"] == "estimate" and res["credits"] is None
    assert res["usd_micros"] == estimate <= res["reserved_usd_micros"]
    row = await _job_row(res["job_id"])
    assert (row["characters"], row["credits"], row["cost_source"]) == (71, None, "estimate")
    assert row["usd_micros"] == estimate
    line = await _ledger().get_line_status("ep01", "el_vo_takes")
    assert line["spent"] == estimate and line["reserved"] == 0


async def test_sfx_and_music_commit_header_credits(live):
    with respx.mock() as mock:
        mock.post(f"{API}/v1/sound-generation").mock(return_value=_audio({"character-cost": "120"}))
        mock.post(f"{API}/v1/music").mock(return_value=_audio({"character-cost": "300"}))
        sfx = await _run(act.generate_sfx, "ep01", "a soft door knock", 5.0, "S01", None)
        mus = await _run(act.generate_music, "ep01", "calm piano", 20.0, "music_v1", "M01", None)
    assert (sfx["reserved_usd_micros"], sfx["usd_micros"], sfx["cost_source"]) == (40_000, 24_000, "provider_header")
    assert (mus["reserved_usd_micros"], mus["usd_micros"], mus["cost_source"]) == (60_000, 60_000, "provider_header")
    for res, line_name in ((sfx, "el_sfx"), (mus, "el_music_one_take")):
        line = await _ledger().get_line_status("ep01", line_name)
        assert line["spent"] == res["usd_micros"] and line["reserved"] == 0, (line_name, line)
        row = await _job_row(res["job_id"])
        assert row["credits"] == res["credits"] and row["cost_source"] == "provider_header", row
    assert await _txns(sfx["job_id"]) == [("reserve", 40_000), ("commit", 24_000), ("release", 16_000)]
    assert await _txns(mus["job_id"]) == [("reserve", 60_000), ("commit", 60_000)]


async def test_sfx_and_music_without_header_commit_estimates_flagged(live):
    with respx.mock() as mock:
        mock.post(f"{API}/v1/sound-generation").mock(return_value=_audio())
        mock.post(f"{API}/v1/music").mock(return_value=_audio())
        sfx = await _run(act.generate_sfx, "ep01", "a soft door knock", 5.0, "S01", None)
        mus = await _run(act.generate_music, "ep01", "calm piano", 20.0, "music_v1", "M01", None)
    assert sfx["cost_source"] == mus["cost_source"] == "estimate"
    assert sfx["usd_micros"] == el.estimate_usd_micros("sfx", {"model": "sound_generation", "duration": 5.0})
    assert mus["usd_micros"] == el.estimate_usd_micros("music", {"model": "music_v1", "duration": 20.0})


async def test_header_above_the_reservation_is_committed_honestly(live):
    """If upstream ever bills more than the documented bound, the actual is spent (never hidden),
    an overage transaction is logged and the result is flagged."""
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice-1").mock(return_value=_audio({"character-cost": "100"}))
        res = await _run(act.generate_voiceover, "ep01", LINE_71, "voice-1", "eleven_v3", "L71", None)
    assert res["usd_micros"] == 20_000 and res["reserved_usd_micros"] == 14_200
    assert res["over_reservation"] is True
    assert ("overage_warning", 5_800) in await _txns(res["job_id"])
    line = await _ledger().get_line_status("ep01", "el_vo_takes")
    assert line["spent"] == 20_000 and line["reserved"] == 0


# ------------------------------------------------------------------ migration

async def test_job_ledger_migration_adds_usage_columns_and_keeps_rows():
    db_path = os.environ["DATABASE_PATH"]
    async with aiosqlite.connect(db_path) as db:  # the pre-G-AUD-COST job_ledger schema
        await db.execute("""CREATE TABLE job_ledger (job_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL,
            shot_id TEXT, model TEXT NOT NULL, tier TEXT NOT NULL, line_name TEXT NOT NULL,
            usd_micros INTEGER NOT NULL, status TEXT NOT NULL, verdict TEXT, used INTEGER,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        await db.execute("INSERT INTO job_ledger VALUES ('old-1','ep01','A01','m','final','L2_drafts',5,"
                         "'committed',NULL,NULL,'t','t')")
        await db.commit()
    await _ledger().init_db()
    await _ledger().init_db()  # idempotent
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("PRAGMA table_info(job_ledger)") as cur:
            cols = {r[1]: r[2] for r in await cur.fetchall()}
    assert (cols.get("characters"), cols.get("credits"), cols.get("cost_source")) == ("INTEGER", "REAL", "TEXT"), cols
    row = await _job_row("old-1")
    assert row["usd_micros"] == 5 and row["status"] == "committed" and row["credits"] is None
