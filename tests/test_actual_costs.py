"""Actual provider charges per job, stored next to the estimate (docs/ACTUAL-COSTS.md).

ElevenLabs: the character-cost header of the call is the actual, recorded in the same transaction as
the guard's commit. Higgsfield: a charge field in a completed status body (none is documented; the
recognised names are tested with fake bodies) or the usage report (tests/test_provider_usage.py).
Older rows are backfilled from what they already hold and labelled. Provider HTTP is respx-mocked:
no network, nothing spent."""

import os

import aiosqlite
import httpx
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from hfvg import actual_costs as ac
from hfvg.activities import audio as act
from hfvg.budget import BudgetLedger
from hfvg.providers import elevenlabs as el

EL_API = "https://api.elevenlabs.io"
HF_API = "https://api.higgsfield.ai"
MP3 = b"ID3" + b"\x00" * 256
LINE = "The kettle clicks off and steam curls past the window into the cold air"  # 71 chars


def _db() -> str:
    return os.environ["DATABASE_PATH"]


async def _row(job_id: str) -> dict:
    async with aiosqlite.connect(_db()) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
    assert row is not None, job_id
    return dict(row)


@pytest.fixture
async def live(monkeypatch):
    from hfvg.studio_db import init_studio_db
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    monkeypatch.delenv("HIGGSFIELD_USD_PER_CREDIT", raising=False)
    await init_studio_db(_db())


# ------------------------------------------------------------------ Higgsfield status charge parser

def test_documented_status_body_has_no_charge():
    assert ac.higgsfield_status_charge({"status": "completed", "request_id": "r1",
                                        "images": [{"url": "https://cdn.test/a.png"}]}) is None
    assert ac.higgsfield_status_charge(None) is None
    assert ac.higgsfield_status_charge({"status": "completed", "cost": "0.12"}) is None, \
        "a bare cost without a currency has no known unit"


def test_usd_charge_fields_are_read_top_level_and_nested():
    top = ac.higgsfield_status_charge({"status": "completed", "charged_usd": "0.125"})
    assert (top.usd_micros, top.unit, top.source, top.ref) == (125_000, "usd", ac.SOURCE_HF_STATUS, "status.charged_usd")
    nested = ac.higgsfield_status_charge({"status": "completed", "billing": {"cost": "1.5", "currency": "USD"}})
    assert (nested.usd_micros, nested.unit, nested.ref) == (1_500_000, "usd", "status.cost")


def test_credit_charge_needs_the_configured_rate(monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_USD_PER_CREDIT", raising=False)
    charge = ac.higgsfield_status_charge({"status": "completed", "credits_charged": 12})
    assert (charge.units, charge.unit, charge.usd_micros, charge.rate_usd) == (12.0, "credits", None, None)
    monkeypatch.setenv("HIGGSFIELD_USD_PER_CREDIT", "0.0625")
    charge = ac.higgsfield_status_charge({"status": "completed", "usage": {"cost": 12, "currency": "credits"}})
    assert (charge.usd_micros, charge.rate_usd) == (750_000, "0.0625")


@pytest.mark.parametrize("value", [-1, "abc", True, float("nan"), float("inf"), None])
def test_unusable_charge_values_are_ignored(value):
    assert ac.higgsfield_status_charge({"status": "completed", "charged_usd": value}) is None


@pytest.mark.parametrize("raw", ["abc", "0", "-0.5"])
def test_bad_credit_rate_is_reported_and_not_used(monkeypatch, raw):
    monkeypatch.setenv("HIGGSFIELD_USD_PER_CREDIT", raw)
    rate, problem = ac.higgsfield_rate()
    assert rate is None and "HIGGSFIELD_USD_PER_CREDIT" in problem


# ------------------------------------------------------------------ ElevenLabs: header actual at commit

async def test_voiceover_records_the_header_charge_next_to_the_estimate(live):
    with respx.mock() as mock:
        mock.post(f"{EL_API}/v1/text-to-speech/voice-1").mock(
            return_value=httpx.Response(200, content=MP3, headers={"character-cost": "28"}))
        res = await ActivityEnvironment().run(act.generate_voiceover, "ep01", LINE, "voice-1", "eleven_v3", "L71", None)
    row = await _row(res["job_id"])
    estimate = el.estimate_usd_micros("tts", {"model": "eleven_v3", "text": LINE})
    assert row["status"] == "committed" and row["usd_micros"] == 5_600
    assert (row["actual_usd_micros"], row["actual_units"], row["actual_unit"]) == (5_600, 28.0, "credits")
    assert (row["actual_source"], row["actual_rate_usd"], row["actual_backfilled"]) == (ac.SOURCE_EL_HEADER, "0.0002", 0)
    assert (row["estimate_usd_micros"], row["estimate_source"]) == (estimate, ac.ESTIMATE_EL_PRICE)
    assert row["actual_recorded_at"]


async def test_voiceover_without_header_has_no_actual(live):
    with respx.mock() as mock:
        mock.post(f"{EL_API}/v1/text-to-speech/voice-1").mock(return_value=httpx.Response(200, content=MP3))
        res = await ActivityEnvironment().run(act.generate_voiceover, "ep01", LINE, "voice-1", "eleven_v3", "L71", None)
    row = await _row(res["job_id"])
    assert row["cost_source"] == "estimate" and row["actual_usd_micros"] is None and row["actual_source"] is None
    assert row["estimate_source"] == ac.ESTIMATE_EL_PRICE


async def test_commit_stores_the_charge_in_the_same_transaction():
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    await ledger.set_line("ep01", "el_sfx", 10_000_000, 8_000_000, "elevenlabs")
    assert await ledger.reserve("ep01", "el_sfx", 40_000, "sfx", job_id="sfx-1")
    await ledger.record_job("ep01", "sfx-1", "S01", "sound_generation", "final", "el_sfx", 40_000)
    assert (await _row("sfx-1"))["estimate_source"] == ac.ESTIMATE_RESERVATION
    charge = ac.elevenlabs_charge(120)
    assert await ledger.commit("ep01", "el_sfx", 40_000, charge.usd_micros, "sfx-1", "sfx", credits=120.0,
                               cost_source="provider_header", provider_charge=charge)
    row = await _row("sfx-1")
    assert (row["usd_micros"], row["actual_usd_micros"], row["actual_source"]) == (24_000, 24_000, ac.SOURCE_EL_HEADER)
    assert (row["estimate_usd_micros"], row["estimate_source"]) == (40_000, ac.ESTIMATE_RESERVATION)
    assert not await ledger.commit("ep01", "el_sfx", 40_000, 1, "sfx-1", "again", provider_charge=ac.elevenlabs_charge(5))
    assert (await _row("sfx-1"))["actual_usd_micros"] == 24_000, "a second commit is a no-op"


# ------------------------------------------------------------------ Higgsfield: charge in a completed status

async def _hf_job(job_id: str, usd: int) -> None:
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode
    await init_studio_db(_db())
    await create_episode(_db(), "ep99")
    await set_live_mode(_db(), "ep99", True)
    await approve_g108(_db(), "ep99")
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    await ledger.set_line("ep99", "L4_video", 100_000_000, 80_000_000, "higgsfield")
    assert await ledger.reserve("ep99", "L4_video", usd, "submit", job_id=job_id)
    await ledger.record_job("ep99", job_id, "A01", "kling-video/v3.0/pro/image-to-video", "final", "L4_video", usd)
    credits = CreditLedger(_db())
    await credits.init_db()
    await credits.create_episode_line("ep99", cap_credits=500)
    await credits.reserve_credits("ep99", job_id, 7.5)
    await credits.persist_provider_job(job_id, f"prov-{job_id}")


@pytest.fixture
def hf_live(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", HF_API)
    monkeypatch.delenv("HIGGSFIELD_USD_PER_CREDIT", raising=False)


@pytest.mark.parametrize("body_extra,expected", [
    ({"charged_usd": "0.42"}, (420_000, ac.SOURCE_HF_STATUS)),
    ({}, (None, None)),
])
async def test_poll_records_a_reported_charge_on_completion(hf_live, body_extra, expected):
    from hfvg.activities.studio_generation import poll_job_status
    await _hf_job("clip-ep99-A01-v1", 336_000)
    body = {"status": "completed", "video": {"url": "https://cdn.test/a01.mp4"}, **body_extra}
    with respx.mock() as mock:
        mock.get(f"{HF_API}/requests/prov-clip-ep99-A01-v1/status").mock(return_value=httpx.Response(200, json=body))
        status = await ActivityEnvironment().run(poll_job_status, "clip-ep99-A01-v1", "clip", "ep99")
    assert status["status"] == "completed"
    row = await _row("clip-ep99-A01-v1")
    assert (row["actual_usd_micros"], row["actual_source"]) == expected
    assert row["status"] == "reserved" and row["usd_micros"] == 336_000, "the poll takes no budget action"


async def test_recording_a_status_charge_never_raises(tmp_path):
    missing = str(tmp_path / "no" / "such" / "dir" / "x.db")
    assert await ac.record_status_charge(missing, "job-1", {"charged_usd": "1"}) is None


# ------------------------------------------------------------------ allocation guard

async def test_usage_allocation_never_replaces_a_per_request_charge():
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    await ledger.set_line("ep01", "L2_drafts", 10_000_000, 8_000_000, "higgsfield")
    for job in ("a", "b"):
        await ledger.record_job("ep01", job, "A01", "soul", "draft", "L2_drafts", 100_000)
    assert await ac.record_charge(_db(), "a", ac.Charge(1, "usd", 1_000_000, "1", ac.SOURCE_HF_STATUS))
    usage = ac.Charge(0.2, "usd", 200_000, "1", ac.SOURCE_HF_USAGE, "usage:2026-11-01:soul")
    assert not await ac.record_charge(_db(), "a", usage)
    assert (await _row("a"))["actual_usd_micros"] == 1_000_000
    assert await ac.record_charge(_db(), "b", usage)
    again = ac.Charge(0.3, "usd", 300_000, "1", ac.SOURCE_HF_USAGE, "usage:2026-11-01:soul")
    assert await ac.record_charge(_db(), "b", again), "a newer allocation replaces the older one"
    assert (await _row("b"))["actual_usd_micros"] == 300_000


# ------------------------------------------------------------------ backfill of older rows

OLD_JOB_LEDGER = """CREATE TABLE job_ledger (job_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL,
    shot_id TEXT, model TEXT NOT NULL, tier TEXT NOT NULL, line_name TEXT NOT NULL,
    usd_micros INTEGER NOT NULL, status TEXT NOT NULL, verdict TEXT, used INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, user_id TEXT, actual_usd_micros INTEGER,
    provider_credits_consumed REAL, cost_source TEXT, characters INTEGER, credits REAL)"""

OLD_ROWS = [
    # job_id, line, usd_micros, status, cost_source, credits, actual
    ("hf-clip", "L4_video", 336_000, "committed", None, None, None),
    ("el-header", "el_vo_takes", 5_600, "committed", "provider_header", 28.0, None),
    ("el-estimate", "el_sfx", 7_000, "committed", "estimate", None, None),
    ("hf-failed", "L2_drafts", 50_000, "released", "failed", 0.0, 0),
]


async def _old_db() -> None:
    async with aiosqlite.connect(_db()) as db:
        await db.execute(OLD_JOB_LEDGER)
        for job_id, line, usd, status, source, credits, actual in OLD_ROWS:
            await db.execute(
                "INSERT INTO job_ledger (job_id, episode_id, shot_id, model, tier, line_name, usd_micros, status, "
                "created_at, updated_at, cost_source, credits, actual_usd_micros) "
                "VALUES (?, 'ep01', 'A01', 'm', 'final', ?, ?, ?, '2026-10-01T10:00:00+00:00', "
                "'2026-10-01T10:05:00+00:00', ?, ?, ?)", (job_id, line, usd, status, source, credits, actual))
        await db.commit()


async def test_backfill_labels_what_older_rows_hold():
    await _old_db()
    await BudgetLedger(_db()).init_db()
    clip, header, estimate, failed = [await _row(j) for j, *_ in OLD_ROWS]
    assert (clip["estimate_usd_micros"], clip["estimate_source"]) == (336_000, ac.ESTIMATE_BACKFILL)
    assert clip["actual_usd_micros"] is None and clip["actual_backfilled"] == 0
    assert (header["estimate_usd_micros"], header["estimate_source"]) == (None, ac.ESTIMATE_NOT_RECORDED)
    assert (header["actual_usd_micros"], header["actual_source"], header["actual_backfilled"]) == \
        (5_600, ac.SOURCE_EL_HEADER, 1)
    assert header["actual_recorded_at"] == "2026-10-01T10:05:00+00:00", "dated when the commit happened"
    assert (estimate["estimate_usd_micros"], estimate["estimate_source"]) == (7_000, ac.ESTIMATE_BACKFILL)
    assert estimate["actual_usd_micros"] is None
    assert (failed["actual_usd_micros"], failed["actual_source"]) == (0, "cost_tracker_failed")


async def test_backfill_runs_once():
    await _old_db()
    await BudgetLedger(_db()).init_db()
    async with aiosqlite.connect(_db()) as db:
        counts = await ac.backfill_job_costs(db)
    assert counts == {"estimates": 0, "elevenlabs_actuals": 0, "tracker_labels": 0, "repriced": 0}


async def test_credit_charges_get_a_dollar_value_once_the_rate_is_set(monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_USD_PER_CREDIT", raising=False)
    ledger = BudgetLedger(_db())
    await ledger.init_db()
    await ledger.set_line("ep01", "L2_drafts", 10_000_000, 8_000_000, "higgsfield")
    await ledger.record_job("ep01", "still-1", "A01", "soul", "draft", "L2_drafts", 100_000)
    await ac.record_status_charge(_db(), "still-1", {"status": "completed", "credits_used": 4})
    assert (await _row("still-1"))["actual_usd_micros"] is None
    monkeypatch.setenv("HIGGSFIELD_USD_PER_CREDIT", "0.03")
    await BudgetLedger(_db()).init_db()
    row = await _row("still-1")
    assert (row["actual_usd_micros"], row["actual_rate_usd"], row["actual_units"]) == (120_000, "0.03", 4.0)
