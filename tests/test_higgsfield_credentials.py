"""G-AUD-KEY: a live Higgsfield still/clip without a usable HIGGSFIELD_API_KEY (missing, blank or
not id:secret) is refused, non-retryable, BEFORE preflight, the smoke-gate override log, the round
hold, the reservation and the job row - nothing is recorded and the provider is never called.

The episode is fully set up for a paid job (live mode, G1.08 approved, budget lines, smoke-gate
override on), so without the key check the activity would write the override log and go on
towards the reservation; the assertions below would then see rows. No network, no spend."""

import os

import aiosqlite
import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError

from hfvg.activities import studio_generation as sg
from tests.ledger_helpers import insert_line

EP = "ep99"


@pytest.fixture
async def live_episode(monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await create_episode(db, EP)
    await set_live_mode(db, EP, True)
    await approve_g108(db, EP)
    async with aiosqlite.connect(db) as c:
        await insert_line(c, EP, "L2_drafts", 10_000_000, 8_000_000)
        await insert_line(c, EP, "L4_video", 30_000_000, 24_000_000)
        await c.commit()
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)
    return db


async def _outcome(coro):
    try:
        await coro
    except ApplicationError as e:
        return e
    except Exception as e:  # any other exception is a wrong outcome (and retryable in Temporal)
        return f"unexpected {type(e).__name__}: {e}"
    return "no error raised"


async def _assert_nothing_recorded(db):
    async with aiosqlite.connect(db) as c:
        for table in ("smoke_gate_overrides", "round_holds", "budget_transactions", "job_ledger"):
            async with c.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                n = (await cur.fetchone())[0]
            assert n == 0, f"{table} has {n} rows after a key refusal"
        async with c.execute("SELECT COALESCE(SUM(reserved_usd_micros), 0), COALESCE(SUM(spent_usd_micros), 0) "
                             "FROM budget_lines") as cur:
            reserved, spent = await cur.fetchone()
    assert reserved == 0 and spent == 0, (reserved, spent)


def _still():
    return sg.submit_still_job_enforced(episode_id=EP, shot_id="A01", prompt="Probe still prompt.", version=1)


def _clip():
    return sg.submit_clip_job_enforced(episode_id=EP, shot_id="A01", start_image_url="https://example.invalid/a.jpg",
                                       prompt="Probe clip prompt.", duration=5.0, version=1)


@pytest.mark.parametrize("key,expected_type", [
    (None, "MissingCredentials"), ("   ", "MissingCredentials"),
    ("malformed-no-colon", "InvalidCredentials"), ("id-only:", "InvalidCredentials"), (":secret-only", "InvalidCredentials"),
])
@pytest.mark.parametrize("submit", ["still", "clip"])
async def test_live_submit_without_usable_key_refused_before_any_ledger_row(live_episode, monkeypatch, submit,
                                                                            key, expected_type):
    if key is not None:
        monkeypatch.setenv("HIGGSFIELD_API_KEY", key)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.route(url__startswith="https://api.higgsfield.ai").mock(
            return_value=httpx.Response(200, json={"request_id": "x", "status": "queued", "credits": "1", "usd": "0.01"}))
        err = await _outcome(_still() if submit == "still" else _clip())
        assert route.call_count == 0, "the provider was called without a usable key"
    assert isinstance(err, ApplicationError), err
    assert err.type == expected_type and err.non_retryable is True, (err.type, err.non_retryable)
    assert "HIGGSFIELD_API_KEY" in str(err)
    await _assert_nothing_recorded(live_episode)


@pytest.mark.parametrize("submit", ["still", "clip"])
async def test_dry_run_needs_no_higgsfield_key(monkeypatch, submit):
    from hfvg.studio_db import create_episode, init_studio_db
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await create_episode(db, EP)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)
    res = await (_still() if submit == "still" else _clip())
    assert "-dry-" in res["job_id"] or res["job_id"].startswith(("still-dry-", "clip-dry-")), res


def test_well_formed_key_passes_the_check(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    assert sg._check_higgsfield_credentials() is None
