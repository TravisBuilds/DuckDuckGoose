"""Tests for the dollar budget ledger (integer usd_micros; every expectation is a literal)."""

import os
import tempfile

import pytest

from hfvg.budget import BudgetLedger
from tests.ledger_helpers import expect_value_error


def create_test_ledger():
    """Create a fresh temporary budget ledger for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return BudgetLedger(db_path=path), path


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.mark.asyncio
async def test_default_settings_are_literal_dollars():
    """Defaults: cap 60.00 USD, stop 48.00 USD, GC.01 headroom 5.00 USD, revision 10.00 USD."""
    from hfvg.budget import EPISODE_CAP_USD_MICROS, get_budget_settings

    assert EPISODE_CAP_USD_MICROS == 60_000_000
    settings = get_budget_settings()
    assert settings.episode_cap_usd_micros == 60_000_000
    assert settings.stop_percent == 80
    assert settings.episode_stop_usd_micros == 48_000_000
    assert settings.gc01_headroom_usd_micros == 5_000_000
    assert settings.revision_reserve_usd_micros == 10_000_000


@pytest.mark.asyncio
async def test_usd_micro_conversion_literals():
    """1 USD == 1_000_000 usd_micros; floats are refused; provider decimal strings are exact."""
    from hfvg.pricing import micros_to_usd_str, usd_to_micros

    assert usd_to_micros("1") == 1_000_000
    assert usd_to_micros("0.06") == 60_000
    assert usd_to_micros("60.00") == 60_000_000
    assert usd_to_micros("0.0000001") == 1  # rounds UP: never under-reserve
    assert micros_to_usd_str(48_000_000) == "48.00"
    assert micros_to_usd_str(60_000) == "0.06"
    with pytest.raises(TypeError):
        usd_to_micros(0.06)


@pytest.mark.asyncio
async def test_init_episode_budget():
    """Episode lines come from the policy's USD line caps (stop = integer 80%)."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")

        # Policy usd.ep04_lines_app_usd: L1_refs 5.70 USD
        l1 = await ledger.get_line_status("ep04", "L1_refs")
        assert l1["budget_cap"] == 5_700_000
        assert l1["stop_threshold"] == 4_560_000
        assert l1["unit"] == "usd_micros"

        # L4_video 14.25 USD
        l4 = await ledger.get_line_status("ep04", "L4_video")
        assert l4["budget_cap"] == 14_250_000
        assert l4["stop_threshold"] == 11_400_000

        # Separate revision reserve: 10.00 USD, usable up to its cap
        rev = await ledger.get_line_status("ep04", "L7_revision")
        assert rev["budget_cap"] == 10_000_000
        assert rev["stop_threshold"] == 10_000_000

        # ElevenLabs credits converted at 0.0002 USD/credit: 700 credits = 0.14 USD
        vo = await ledger.get_line_status("ep04", "el_vo_takes")
        assert vo["budget_cap"] == 140_000
        assert vo["stop_threshold"] == 112_000
        assert vo["provider"] == "elevenlabs"
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_reserve_commit_flow():
    """Reserve -> commit actual (under the hold) spends only the actual, frees the rest."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")

        assert await ledger.reserve("ep04", "L1_refs", 3_000_000, "Test refs") is True
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 0
        assert status["reserved"] == 3_000_000
        assert status["total_committed"] == 3_000_000
        assert status["available"] == 1_560_000  # 4_560_000 stop - 3_000_000

        # Commit 2_850_000 actual against the 3_000_000 hold
        assert await ledger.commit("ep04", "L1_refs", 3_000_000, 2_850_000, reason="actual") is True
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 2_850_000
        assert status["reserved"] == 0

        # 1_710_000 left to the stop: one micro more is refused, exactly the rest is allowed
        assert await ledger.reserve("ep04", "L1_refs", 1_710_001) is False
        assert await ledger.reserve("ep04", "L1_refs", 1_710_000) is True
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_release_refund():
    """Release frees the hold without spending."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")
        await ledger.reserve("ep04", "L1_refs", 2_000_000)
        assert (await ledger.get_line_status("ep04", "L1_refs"))["reserved"] == 2_000_000

        released = await ledger.release("ep04", "L1_refs", 2_000_000, "Content blocked")
        assert released == 2_000_000

        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["spent"] == 0
        assert status["reserved"] == 0
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_episode_summary():
    """Summary totals per provider, episode total (excl. revision reserve) and caps."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")

        await ledger.reserve("ep04", "L1_refs", 2_000_000)
        await ledger.commit("ep04", "L1_refs", 2_000_000)
        await ledger.reserve("ep04", "L4_video", 5_000_000)
        await ledger.commit("ep04", "L4_video", 5_000_000)
        await ledger.reserve("ep04", "el_vo_takes", 100_000)
        await ledger.commit("ep04", "el_vo_takes", 100_000)

        summary = await ledger.get_episode_summary("ep04")
        assert summary["episode_id"] == "ep04"
        assert summary["unit"] == "usd_micros"
        assert summary["higgsfield_total"] == 7_000_000
        assert summary["elevenlabs_total"] == 100_000
        assert summary["episode_total"] == 7_100_000
        assert summary["episode_cap"] == 60_000_000
        assert summary["episode_stop"] == 48_000_000
        assert summary["gc01_headroom"] == 5_000_000
        names = {line["line_name"] for line in summary["lines"]}
        assert "L7_revision" in names
        rev = [line for line in summary["lines"] if line["line_name"] == "L7_revision"][0]
        assert rev["revision_reserve"] is True
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_80_percent_stop():
    """Line stop at 80%: exact boundary allowed, the next micro-dollar refused."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")

        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 5_700_000
        assert status["stop_threshold"] == 4_560_000
        assert status["spent"] == 0
        assert status["reserved"] == 0

        assert await ledger.reserve("ep04", "L1_refs", 4_000_000) is True
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 4_000_000
        assert status["at_stop"] is False

        # 4_600_000 total exceeds the 4_560_000 stop -> refused
        assert await ledger.reserve("ep04", "L1_refs", 600_000) is False

        # Exactly to the stop threshold is allowed...
        assert await ledger.reserve("ep04", "L1_refs", 560_000) is True
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 4_560_000
        assert status["at_stop"] is True  # at exactly the stop threshold

        # ...and then the very next reserve (1 micro-dollar) is refused
        assert await ledger.reserve("ep04", "L1_refs", 1) is False
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["reserved"] == 4_560_000
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_hard_cap_enforcement():
    """A reserve above the line cap raises ValueError (not just False) and reserves nothing."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")
        status = await ledger.get_line_status("ep04", "L1_refs")
        assert status["budget_cap"] == 5_700_000

        # MUST raise ValueError (not just return False)
        message = await expect_value_error(
            ledger.reserve("ep04", "L1_refs", 6_000_000), "cap exceeded")
        assert "5.70 USD" in message, message

        assert (await ledger.get_line_status("ep04", "L1_refs"))["reserved"] == 0
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_episode_cap_default_60_usd_and_stop_48():
    """Episode: stop at 48.00 USD (refuse, False), cap 60.00 USD (ValueError naming 60.00)."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 50_000_000, 50_000_000)
        await ledger.set_line("ep77", "LB", 50_000_000, 50_000_000)

        assert await ledger.reserve("ep77", "LA", 40_000_000) is True
        assert await ledger.reserve("ep77", "LB", 8_000_000) is True  # episode total == 48_000_000

        # At exactly the episode stop the next reserve is refused
        assert await ledger.reserve("ep77", "LB", 1) is False
        summary = await ledger.get_episode_summary("ep77")
        assert summary["episode_total"] == 48_000_000
        assert summary["episode_at_stop"] is True

        # Above the 60.00 USD cap it is a hard error that names the cap
        with pytest.raises(ValueError) as exc_info:
            await ledger.reserve("ep77", "LB", 12_000_001)
        msg = str(exc_info.value)
        assert "episode cap" in msg.lower() and "60.00" in msg, msg
        assert "1250" not in msg and "950" not in msg, msg
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_stop_is_integer_80_percent_no_float_drift():
    """Stop derives from the cap with integer math: odd caps floor, never a float."""
    from hfvg.budget import compute_stop_usd_micros

    derived = compute_stop_usd_micros(33_333_333, 80)
    assert derived == 26_666_666  # floor(33_333_333 * 80 / 100)
    assert isinstance(derived, int)
    assert compute_stop_usd_micros(60_000_000, 80) == 48_000_000

    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 33_333_333)  # stop defaults to 80%
        status = await ledger.get_line_status("ep77", "LA")
        assert status["budget_cap"] == 33_333_333
        assert status["stop_threshold"] == 26_666_666  # floor(33_333_333 * 80 / 100)
        assert isinstance(status["stop_threshold"], int)

        import aiosqlite
        async with aiosqlite.connect(path) as db:
            async with db.execute(
                "SELECT typeof(cap_usd_micros), typeof(stop_usd_micros), typeof(reserved_usd_micros)"
                " FROM budget_lines WHERE line_id = 'ep77:LA'"
            ) as cur:
                assert await cur.fetchone() == ("integer", "integer", "integer")
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_ledger_rejects_floats():
    """Floats can never enter the ledger."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 10_000_000)
        with pytest.raises(TypeError):
            await ledger.reserve("ep77", "LA", 1.5)
        with pytest.raises(TypeError):
            await ledger.set_line("ep77", "LB", 10_000_000.0)
        with pytest.raises(TypeError):
            await ledger.release("ep77", "LA", 0.5)
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_gc01_headroom_refuses_when_balance_too_low():
    """GC.01: refuse a new paid job when manual balance - reserved < 5.00 USD headroom."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 50_000_000)
        await ledger.set_manual_balance(9_000_000, "test", "balance 9.00")  # 9.00 USD

        # 9.00 - 4.00 = 5.00 == headroom: allowed (not below)
        assert await ledger.reserve("ep77", "LA", 4_000_000) is True
        # 9.00 - 4.00 - 1 micro = 4.999999 < 5.00: refused
        await expect_value_error(ledger.reserve("ep77", "LA", 1), "GC.01")
        assert (await ledger.get_line_status("ep77", "LA"))["reserved"] == 4_000_000

        # Releasing the hold restores the headroom
        await ledger.release("ep77", "LA", 4_000_000)
        assert await ledger.reserve("ep77", "LA", 4_000_000) is True
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_gc01_counts_spend_since_balance_entry():
    """Committed spend after the manual entry also reduces what is left above the headroom."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 50_000_000)
        await ledger.set_manual_balance(10_000_000, "test", "balance 10.00")
        assert await ledger.reserve("ep77", "LA", 3_000_000) is True
        await ledger.commit("ep77", "LA", 3_000_000, 3_000_000)

        status = await ledger.get_balance_status()
        assert status["manual_balance_usd_micros"] == 10_000_000
        assert status["spent_since_entry_usd_micros"] == 3_000_000
        assert status["reserved_usd_micros"] == 0
        assert status["balance_remaining_usd_micros"] == 7_000_000

        # 7.00 - 2.00 = 5.00 ok; one micro more breaks the headroom
        await expect_value_error(ledger.reserve("ep77", "LA", 2_000_001), "GC.01")
        assert await ledger.reserve("ep77", "LA", 2_000_000) is True

        # A fresh manual entry resets the baseline
        await ledger.set_manual_balance(50_000_000, "test", "topped up")
        status = await ledger.get_balance_status()
        assert status["spent_since_entry_usd_micros"] == 0
        assert status["balance_remaining_usd_micros"] == 48_000_000  # 50.00 - 2.00 held
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_live_preflight_requires_manual_balance():
    """require_balance=True (live paid jobs) fails closed when no manual balance exists."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 50_000_000)
        await expect_value_error(
            ledger.reserve("ep77", "LA", 1_000_000, require_balance=True), "no manual Higgsfield balance")
        assert (await ledger.get_line_status("ep77", "LA"))["reserved"] == 0

        await ledger.set_manual_balance(100_000_000, "test", "ok")
        assert await ledger.reserve("ep77", "LA", 1_000_000, require_balance=True) is True
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_revision_reserve_requires_revision_tag():
    """The 10.00 USD revision reserve is usable ONLY with the revision tag, outside the episode cap."""
    ledger, path = create_test_ledger()
    try:
        await ledger.init_episode_budget("ep04")

        await expect_value_error(
            ledger.reserve("ep04", "L7_revision", 1_000_000), "approval tag 'revision'")
        assert (await ledger.get_line_status("ep04", "L7_revision"))["reserved"] == 0

        assert await ledger.reserve("ep04", "L7_revision", 10_000_000, revision=True) is True
        assert (await ledger.get_line_status("ep04", "L7_revision"))["reserved"] == 10_000_000
        # reserve is exhausted at its 10.00 USD cap
        await expect_value_error(
            ledger.reserve("ep04", "L7_revision", 1, revision=True), "cap exceeded")

        # Revision spend does not count toward the 60.00 USD episode cap/stop
        summary = await ledger.get_episode_summary("ep04")
        assert summary["episode_total"] == 0
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_per_job_ledger_and_idempotent_commit():
    """Per-job rows (model, tier, usd_micros, status, verdict, used); finalize is idempotent."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 50_000_000)
        assert await ledger.reserve("ep77", "LA", 1_000_000) is True
        assert await ledger.record_job("ep77", "job-1", "A01", "kling-3.0", "draft", "LA", 1_000_000)
        # recording the same provider job twice is a no-op
        assert not await ledger.record_job("ep77", "job-1", "A01", "kling-3.0", "draft", "LA", 1_000_000)

        jobs = await ledger.get_jobs("ep77")
        assert len(jobs) == 1
        assert jobs[0]["job_id"] == "job-1" and jobs[0]["shot_id"] == "A01"
        assert jobs[0]["model"] == "kling-3.0" and jobs[0]["tier"] == "draft"
        assert jobs[0]["usd_micros"] == 1_000_000 and jobs[0]["usd"] == "1.00"
        assert jobs[0]["status"] == "reserved" and jobs[0]["verdict"] is None
        assert jobs[0]["used"] is None

        assert await ledger.commit("ep77", "LA", 1_000_000, 900_000, job_id="job-1") is True
        # second commit/release for the same job changes nothing
        assert await ledger.commit("ep77", "LA", 1_000_000, 900_000, job_id="job-1") is False
        assert await ledger.release("ep77", "LA", 1_000_000, job_id="job-1") == 0
        status = await ledger.get_line_status("ep77", "LA")
        assert status["spent"] == 900_000
        assert status["reserved"] == 0

        await ledger.set_job_verdict("job-1", "passed", used=False)
        jobs = await ledger.get_jobs("ep77")
        assert jobs[0]["status"] == "committed" and jobs[0]["usd_micros"] == 900_000
        assert jobs[0]["verdict"] == "passed" and jobs[0]["used"] == "unused"

        with pytest.raises(ValueError):
            await ledger.record_job("ep77", "job-2", "A01", "m", "bogus-tier", "LA", 1)
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_legacy_credit_ledger_is_migrated_aside():
    """An old credit-unit budget_lines table is renamed, never read as dollars."""
    import aiosqlite
    ledger, path = create_test_ledger()
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute("""CREATE TABLE budget_lines (
                line_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL, provider TEXT NOT NULL,
                line_name TEXT NOT NULL, budget_cap REAL NOT NULL, stop_threshold REAL NOT NULL,
                unit TEXT NOT NULL, spent REAL DEFAULT 0, reserved REAL DEFAULT 0, created_at TEXT)""")
            await db.execute("INSERT INTO budget_lines (line_id, episode_id, provider, line_name,"
                             " budget_cap, stop_threshold, unit) VALUES ('e:L1','e','higgsfield','L1',"
                             "91.2,72.96,'Higgsfield API credits')")
            await db.commit()
        await ledger.init_db()
        with pytest.raises(ValueError):
            await ledger.get_line_status("e", "L1")  # not visible in the dollar ledger
        async with aiosqlite.connect(path) as db:
            async with db.execute("SELECT budget_cap FROM budget_lines_legacy_credits") as cur:
                assert (await cur.fetchone())[0] == 91.2
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_release_over_hold_is_clamped_never_negative():
    """Releasing more than is held frees only the hold: reserved never goes below 0."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 10_000_000)
        assert await ledger.reserve("ep77", "LA", 500_000) is True

        released = await ledger.release("ep77", "LA", 800_000, "over-release")

        assert released == 500_000
        status = await ledger.get_line_status("ep77", "LA")
        assert status["reserved"] == 0
        assert status["total_committed"] == 0
        # and a further release is a no-op, still never negative
        assert await ledger.release("ep77", "LA", 100_000, "again") == 0
        assert (await ledger.get_line_status("ep77", "LA"))["reserved"] == 0
    finally:
        _cleanup(path)


@pytest.mark.asyncio
async def test_commit_overage_spends_actual_not_reserved():
    """Actual cost above the hold is spent in full (and logged), never capped at the hold."""
    ledger, path = create_test_ledger()
    try:
        await ledger.set_line("ep77", "LA", 10_000_000)
        assert await ledger.reserve("ep77", "LA", 1_000_000) is True

        assert await ledger.commit("ep77", "LA", 1_000_000, 1_300_000, job_id="j1") is True

        status = await ledger.get_line_status("ep77", "LA")
        assert status["spent"] == 1_300_000
        assert status["reserved"] == 0
        import aiosqlite
        async with aiosqlite.connect(path) as db:
            async with db.execute(
                "SELECT txn_type, amount FROM budget_transactions WHERE job_id = 'j1' ORDER BY txn_id"
            ) as cur:
                rows = await cur.fetchall()
        assert rows == [("commit", 1_300_000), ("overage_warning", 300_000)]
    finally:
        _cleanup(path)
