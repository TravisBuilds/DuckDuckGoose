"""G-AUD-KEY: a live ElevenLabs activity without ELEVENLABS_API_KEY is refused, non-retryable,
BEFORE preflight, the smoke-gate override log, the AUDIO round hold, the reservation and the job
row - so the refusal leaves no ledger rows at all and never reaches the provider.

Every test runs with SMOKE_TEST_OVERRIDE=true and a real studio DB, so without the key check the
activity would get as far as writing the override log and reserving (the assertions below would
then see rows). Nothing reaches the network (respx) and nothing is spent."""

import os

import aiosqlite
import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from hfvg.activities import audio as act

API = "https://api.elevenlabs.io"
MP3 = b"ID3" + b"\x00" * 256


@pytest.fixture
async def live_no_key(monkeypatch):
    from hfvg.studio_db import init_studio_db
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    await init_studio_db(os.environ["DATABASE_PATH"])
    from hfvg.budget import BudgetLedger
    await BudgetLedger(os.environ["DATABASE_PATH"]).init_db()


async def _outcome(fn, *args):
    """The ApplicationError raised (or a stand-in describing any other outcome)."""
    try:
        await ActivityEnvironment().run(fn, *args)
    except ApplicationError as e:
        return e
    except Exception as e:  # anything else (retryable in Temporal) is a wrong outcome
        return f"unexpected {type(e).__name__}: {e}"
    return "no error raised"


async def _rows(table: str) -> int:
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        async with db.execute(f"SELECT COUNT(*) FROM {table}") as cur:
            return (await cur.fetchone())[0]


async def _assert_nothing_recorded():
    for table in ("smoke_gate_overrides", "round_holds", "budget_transactions", "job_ledger"):
        assert await _rows(table) == 0, f"{table} has rows after a missing-key refusal"
    async with aiosqlite.connect(os.environ["DATABASE_PATH"]) as db:
        async with db.execute("SELECT COALESCE(SUM(reserved_usd_micros), 0), COALESCE(SUM(spent_usd_micros), 0) "
                              "FROM budget_lines") as cur:
            reserved, spent = await cur.fetchone()
    assert reserved == 0 and spent == 0, (reserved, spent)


CASES = {
    "tts": (act.generate_voiceover, ("ep99", "A short narrated line.", "voice-1", "eleven_v3", "vo01", None)),
    "sfx": (act.generate_sfx, ("ep99", "a soft door knock", 3.0, "sfx01", None)),
    "music": (act.generate_music, ("ep99", "calm piano", 20.0, "music_v1", "music01", None)),
}


@pytest.mark.parametrize("kind", ["tts", "sfx", "music"])
async def test_missing_elevenlabs_key_refused_before_any_ledger_row(live_no_key, kind):
    fn, args = CASES[kind]
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=API).mock(return_value=httpx.Response(200, content=MP3))
        err = await _outcome(fn, *args)
        assert route.call_count == 0, "the provider was called without a key"
    assert isinstance(err, ApplicationError), err
    assert err.type == "MissingCredentials" and err.non_retryable is True, (err.type, err.non_retryable)
    assert "ELEVENLABS_API_KEY" in str(err)
    await _assert_nothing_recorded()


async def test_blank_elevenlabs_key_is_missing(live_no_key, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "   ")
    fn, args = CASES["tts"]
    err = await _outcome(fn, *args)
    assert isinstance(err, ApplicationError) and err.type == "MissingCredentials", err
    await _assert_nothing_recorded()


async def test_key_check_runs_before_preflight(live_no_key):
    """An over-limit request without a key is refused for the key (the first check), not preflight."""
    err = await _outcome(act.generate_voiceover, "ep99", "x" * 5001, "voice-1", "eleven_v3", "vo02", None)
    assert isinstance(err, ApplicationError) and err.type == "MissingCredentials", err
    await _assert_nothing_recorded()


async def test_reuse_of_a_paid_file_needs_no_key(live_no_key):
    """Content-hash reuse spends nothing, so it works without a key (and records nothing)."""
    fn, args = CASES["tts"]
    key = act._key("tts", args[1], args[2], args[3])
    out = act.generated_path("ep99", "vo", args[4], key)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(MP3)
    res = await ActivityEnvironment().run(fn, *args)
    assert res["source"] == "generated_reused"
    await _assert_nothing_recorded()


async def test_dry_run_needs_no_key(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__startswith=API).mock(return_value=httpx.Response(200, content=MP3))
        res = await ActivityEnvironment().run(act.generate_voiceover, "ep99", "Test", "voice-id", "eleven_v3",
                                              "vo01", None)
        assert route.call_count == 0
    assert res["dry_run"] is True and res["source"] == "dry_run_stand_in"
