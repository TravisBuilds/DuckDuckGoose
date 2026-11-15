"""The dashboard's cost truth (docs/ACTUAL-COSTS.md).

Provider-reported charges next to the guard's estimates per provider, stage and episode; coverage;
spend per finished episode and per finished minute; spend by person (never the shared admin login);
the month-end projection from real spend so far; and the admin routes behind it. People, episodes
and amounts are made up."""

import json
import os
from datetime import datetime, timezone
from decimal import Decimal

import aiosqlite
import pytest

from hfvg import actual_costs as ac
from hfvg import cost_report as cr
from hfvg import provider_usage as pu
from hfvg import spend_attribution as sa
from hfvg.budget import BudgetLedger
from hfvg.studio_db import create_episode, init_studio_db
from hfvg.users import UserManager

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
DAY10 = "2026-11-10T10:00:00+00:00"


def _db() -> str:
    return os.environ["DATABASE_PATH"]


@pytest.fixture
async def people():
    from hfvg.cost_tracking import CostTracker
    await init_studio_db(_db())
    await BudgetLedger(_db()).init_db()
    await CostTracker(_db()).init_db()
    users = UserManager(_db())
    await users.init_db()
    one = await users.create_user(name="Producer One", email="producer.one@example.test", role="member")
    two = await users.create_user(name="Editor Two", email="editor.two@example.test", role="member")
    return one["user_id"], two["user_id"]


async def _job(job_id, episode_id, line, usd, *, status="committed", actual=None, source=None, estimate="same",
               backfilled=0, user_id="admin", created_at=DAY10):
    await BudgetLedger(_db()).record_job(episode_id, job_id, "A01", "model-x", "draft", line, usd, user_id=user_id)
    async with aiosqlite.connect(_db()) as db:
        await db.execute(
            "UPDATE job_ledger SET status = ?, actual_usd_micros = ?, actual_source = ?, actual_backfilled = ?, "
            "created_at = ?, estimate_usd_micros = ? WHERE job_id = ?",
            (status, actual, source, backfilled, created_at, usd if estimate == "same" else estimate, job_id))
        await db.commit()


async def _runs(*rows):
    async with aiosqlite.connect(_db()) as db:
        await ac.ensure_cost_schema(db)
        await db.executemany(
            "INSERT INTO episode_runs (episode_id, run_id, started_at, started_by, started_by_role, via) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)
        await db.commit()


# ------------------------------------------------------------------ actual vs estimate

async def test_actual_next_to_estimate_by_provider_stage_and_episode(people):
    await _job("clip-1", "ep01", "L4_video", 400_000, actual=420_000, source=ac.SOURCE_HF_USAGE)
    await _job("still-1", "ep01", "L2_drafts", 100_000)
    await _job("vo-1", "ep01", "el_vo_takes", 5_600, actual=5_600, source=ac.SOURCE_EL_HEADER, estimate=5_000)
    await _job("sfx-1", "ep02", "el_sfx", 24_000, actual=24_000, source=ac.SOURCE_EL_HEADER, estimate=None,
               backfilled=1)
    await _job("clip-2", "ep02", "L4_video", 400_000, status="reserved")
    await _job("clip-3", "ep02", "L4_video", 300_000, status="released", actual=0, source="failed")
    truth = await cr.build_cost_truth(_db(), NOW)

    t = truth["totals"]
    assert (t["jobs"], t["spend_usd_micros"], t["actual_usd_micros"], t["jobs_with_actual"]) == (4, 549_600, 449_600, 3)
    assert t["coverage_pct"] == 81.8
    assert (t["estimate_usd_micros"], t["jobs_with_estimate"], t["backfilled_usd_micros"]) == (505_000, 3, 24_000)
    # only jobs with both numbers are compared: the still without an actual is not an "exact" estimate
    assert (t["compared_jobs"], t["compared_estimate_usd_micros"], t["compared_actual_usd_micros"]) == \
        (2, 405_000, 425_600)
    assert (t["delta_usd_micros"], t["delta_pct"]) == (20_600, 5.1)
    assert truth["in_flight"] == {"jobs": 1, "held_usd_micros": 400_000}

    providers = {p["provider"]: p for p in truth["by_provider"]}
    assert (providers["higgsfield"]["spend_usd_micros"], providers["higgsfield"]["coverage_pct"],
            providers["higgsfield"]["delta_usd_micros"]) == (520_000, 80.8, 20_000)
    assert (providers["elevenlabs"]["spend_usd_micros"], providers["elevenlabs"]["coverage_pct"],
            providers["elevenlabs"]["delta_usd_micros"]) == (29_600, 100.0, 600)
    assert [(s["stage"], s["spend_usd_micros"]) for s in truth["by_stage"]] == [
        ("video_clips", 420_000), ("draft_stills", 100_000), ("sound_effects", 24_000), ("voice_over", 5_600)]
    episodes = {e["episode_id"]: e for e in truth["by_episode"]}
    assert [e["episode_id"] for e in truth["by_episode"]] == ["ep01", "ep02"]
    assert episodes["ep01"]["spend_usd_micros"] == 525_600
    assert episodes["ep01"]["providers"]["higgsfield"]["actual_usd_micros"] == 420_000
    assert episodes["ep02"]["backfilled_usd_micros"] == 24_000
    assert truth["by_actual_source"] == {ac.SOURCE_EL_HEADER: 29_600, ac.SOURCE_HF_USAGE: 420_000,
                                         "estimate_only": 100_000}


async def test_no_actuals_means_zero_coverage_not_zero_spend(people):
    await _job("still-1", "ep01", "L2_drafts", 100_000)
    truth = await cr.build_cost_truth(_db(), NOW)
    assert (truth["totals"]["spend_usd_micros"], truth["totals"]["coverage_pct"]) == (100_000, 0.0)
    assert truth["totals"]["delta_usd_micros"] is None and truth["totals"]["compared_jobs"] == 0


async def test_an_empty_studio_reports_nothing(people):
    truth = await cr.build_cost_truth(_db(), NOW)
    assert truth["totals"]["jobs"] == 0 and truth["totals"]["coverage_pct"] is None
    assert truth["monthly"]["projected_month_end_usd_micros"] == 0 and truth["by_person"] == []


async def test_a_database_without_a_job_ledger_still_answers(tmp_path):
    truth = await cr.build_cost_truth(str(tmp_path / "fresh.db"), NOW)
    assert truth["totals"]["jobs"] == 0 and truth["in_flight"] == {"jobs": 0, "held_usd_micros": 0}


# ------------------------------------------------------------------ the month-end projection

def test_projection_uses_spend_so_far_this_month():
    rows = [(datetime(2026, 11, 2, tzinfo=timezone.utc), 1_000_000, 1_000_000),
            (datetime(2026, 11, 14, tzinfo=timezone.utc), 450_000, None),
            (datetime(2026, 10, 31, 23, tzinfo=timezone.utc), 9_999_999, None),
            (datetime(2026, 11, 16, tzinfo=timezone.utc), 7_777_777, None)]
    out = cr.monthly_projection(rows, NOW.replace(day=15, hour=12))
    assert (out["month"], out["days_elapsed"], out["days_in_month"]) == ("2026-11", 14.5, 30)
    assert (out["spend_so_far_usd_micros"], out["actual_so_far_usd_micros"]) == (1_450_000, 1_000_000)
    assert (out["daily_rate_usd_micros"], out["projected_month_end_usd_micros"]) == (100_000, 3_000_000)
    assert out["coverage_pct"] == 69.0


def test_projection_early_in_the_month_counts_at_least_one_day():
    out = cr.monthly_projection([(datetime(2026, 12, 1, 1, tzinfo=timezone.utc), 500_000, None)],
                                datetime(2026, 12, 1, 6, tzinfo=timezone.utc))
    assert (out["days_elapsed"], out["daily_rate_usd_micros"], out["projected_month_end_usd_micros"]) == \
        (1.0, 500_000, 15_500_000)


async def test_month_projection_in_the_cost_truth_is_not_zero_without_actuals(people):
    await _job("clip-1", "ep01", "L4_video", 16_650_000, created_at="2026-11-03T10:00:00+00:00")
    monthly = (await cr.build_cost_truth(_db(), NOW))["monthly"]
    assert monthly["spend_so_far_usd_micros"] == 16_650_000 and monthly["coverage_pct"] == 0.0
    assert monthly["projected_month_end_usd_micros"] == int(16_650_000 / 14.5 * 30)


async def test_runrate_counts_committed_spend_without_actuals(people):
    """The dashboard's run-rate read $0.00 with $16.65 spent: it summed provider charges only."""
    from hfvg.cost_tracking import CostTracker
    ledger = BudgetLedger(_db())
    await ledger.record_job("ep01", "clip-1", "A01", "m", "draft", "L4_video", 16_650_000)
    await ledger.record_job("ep01", "clip-2", "A02", "m", "draft", "L4_video", 3_000_000)
    async with aiosqlite.connect(_db()) as db:
        await db.execute("UPDATE job_ledger SET status = 'committed' WHERE job_id = 'clip-1'")
        await db.execute("UPDATE job_ledger SET status = 'released' WHERE job_id = 'clip-2'")
        await db.commit()
    runrate = await CostTracker(_db()).get_monthly_runrate()
    assert runrate["current_spend_usd_micros"] == 16_650_000
    assert runrate["projected_month_end_usd_micros"] >= 16_650_000
    assert (runrate["actual_spend_usd_micros"], runrate["coverage_percent"]) == (0, 0.0)


# ------------------------------------------------------------------ spend by person

@pytest.mark.parametrize("user_id,expected", [
    ("usr_0123", True), ("admin", False), ("legacy/shared", False), ("system", False), ("", False), (None, False)])
def test_the_shared_login_and_system_ids_name_nobody(user_id, expected):
    assert sa.is_person(user_id) is expected


def test_a_job_belongs_to_the_latest_run_started_before_it():
    attr = sa.Attribution(runs={"ep01": [("2026-11-10T09:00:00", "usr_a", "member"),
                                         ("2026-11-12T09:00:00", "usr_b", "member")]},
                          owners={"ep01": "usr_owner"})
    assert attr.attribute("ep01", None, "2026-11-11 08:00:00") == ("usr_a", "run_starter")
    assert attr.attribute("ep01", None, "2026-11-12T09:00:00+00:00") == ("usr_b", "run_starter")
    assert attr.attribute("ep01", None, "2026-11-09T00:00:00+00:00") == ("usr_owner", "episode_owner")
    assert attr.attribute("ep01") == ("usr_b", "run_starter")
    assert attr.attribute("ep02", "usr_job") == ("usr_job", "job_owner")
    assert attr.attribute("ep02", "admin") == (None, "unattributed_shared_login")
    assert attr.attribute("ep02", "legacy/shared") == (None, "unattributed")


async def test_spend_goes_to_whoever_started_the_run_not_the_shared_login(people):
    one, two = people
    await create_episode(_db(), "ep01", user_id="admin")
    await create_episode(_db(), "ep02", user_id=two)
    await create_episode(_db(), "ep03", user_id="admin")
    await _runs(("ep01", "run-1", "2026-11-10T09:00:00+00:00", one, "member", "start"),
                ("ep01", "run-2", "2026-11-12T09:00:00+00:00", "admin", "admin", "start_again"),
                ("ep03", "run-1", "2026-11-10T09:00:00+00:00", None, None, "start"))
    await sa.set_spend_owner(_db(), "ep03", one, "usr_admin", "ran it on the studio laptop")
    await _job("ep01-a", "ep01", "L4_video", 300_000)
    await _job("ep01-b", "ep01", "L4_video", 200_000, created_at="2026-11-12T10:00:00+00:00")
    await _job("ep02-a", "ep02", "L2_drafts", 50_000)
    await _job("ep03-a", "ep03", "L4_video", 70_000)
    await _job("ep04-a", "ep04", "L2_drafts", 10_000, user_id="legacy/shared")
    truth = await cr.build_cost_truth(_db(), NOW)

    by_name = {p["name"]: p for p in truth["by_person"]}
    assert (by_name["Producer One"]["user_id"], by_name["Producer One"]["spend_usd_micros"],
            by_name["Producer One"]["episodes"], by_name["Producer One"]["attribution"]) == \
        (one, 370_000, 2, ["assigned", "run_starter"])
    assert (by_name["Editor Two"]["spend_usd_micros"], by_name["Editor Two"]["attribution"]) == \
        (50_000, ["episode_owner"])
    assert (by_name["Unattributed"]["user_id"], by_name["Unattributed"]["spend_usd_micros"]) == (None, 210_000)
    assert "admin" not in {p["user_id"] for p in truth["by_person"]}
    assert truth["unattributed_usd_micros"] == {"unattributed_shared_login": 200_000, "unattributed": 10_000}
    episodes = {e["episode_id"]: e for e in truth["by_episode"]}
    assert (episodes["ep03"]["attribution"], episodes["ep03"]["person"]["name"]) == ("assigned", "Producer One")
    assert episodes["ep01"]["person"]["name"] == "Producer One"
    assert episodes["ep01"]["attribution"] == "unattributed_shared_login"
    shared = next(w for w in cr.cost_warnings(truth) if w["code"] == "shared_login_spend")
    assert "$0.20" in shared["message"]


async def test_assignment_is_replaced_and_cleared(people):
    one, two = people
    await sa.set_spend_owner(_db(), "ep01", one, "usr_admin")
    saved = await sa.set_spend_owner(_db(), "ep01", two, "usr_admin", "handed over")
    assert (saved["user_id"], saved["note"]) == (two, "handed over")
    async with aiosqlite.connect(_db()) as db:
        assert (await sa.load_attribution(db)).assigned == {"ep01": two}
    assert await sa.clear_spend_owner(_db(), "ep01") is True
    assert await sa.clear_spend_owner(_db(), "ep01") is False


async def test_run_starts_are_recorded_once(people):
    one, _ = people
    await sa.record_run_start(_db(), "ep01", "run-1", one, "member", "start")
    await sa.record_run_start(_db(), "ep01", "run-1", "someone-else", "member", "start")
    async with aiosqlite.connect(_db()) as db:
        attr = await sa.load_attribution(db)
    assert [(by, role) for _, by, role in attr.runs["ep01"]] == [(one, "member")]


# ------------------------------------------------------------------ finished episodes

def _manifest(root, episode_id, duration, dry_run=False):
    path = root / episode_id / "delivery" / "delivery_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"picture_duration_s": duration, "dry_run": dry_run}))


async def test_cost_per_finished_episode_and_minute(people, tmp_path):
    await _job("a", "ep01", "L4_video", 600_000, actual=600_000, source=ac.SOURCE_HF_USAGE)
    await _job("b", "ep02", "L4_video", 300_000)
    await _job("c", "ep03", "L4_video", 900_000)
    await _job("d", "ep04", "L4_video", 200_000)
    root = tmp_path / "episodes"
    _manifest(root, "ep01", 90.0)
    _manifest(root, "ep02", 60.0, dry_run=True)
    async with aiosqlite.connect(_db()) as db:
        await db.executemany(
            "INSERT INTO audit_log (episode_id, action, details, user) VALUES (?, 'approve_g_final_cut', '', 'u')",
            [("ep01-episode",), ("ep02",), ("ep04",)])
        await db.commit()
    truth = await cr.build_cost_truth(_db(), NOW, episodes_root=root)

    finished = truth["per_finished"]
    assert (finished["finished_episodes"], finished["finished_with_duration"], finished["finished_minutes"]) == \
        (2, 1, 1.5)
    assert (finished["finished_spend_usd_micros"], finished["per_episode_usd_micros"]) == (800_000, 400_000)
    assert finished["per_minute_usd_micros"] == 400_000, "ep01: $0.60 over a 90 s picture"
    assert finished["all_spend_per_finished_episode_usd_micros"] == 1_000_000
    assert finished["finished_coverage_pct"] == 75.0
    episodes = {e["episode_id"]: e for e in truth["by_episode"]}
    assert (episodes["ep01"]["finished"], episodes["ep01"]["duration_s"]) == (True, 90.0)
    assert episodes["ep01"]["final_cut_approved_at"]
    assert episodes["ep02"]["finished"] is False, "a dry-run delivery is not a finished episode"
    assert (episodes["ep03"]["finished"], episodes["ep03"]["final_cut_approved_at"]) == (False, None)
    assert (episodes["ep04"]["finished"], episodes["ep04"]["duration_s"]) == (True, None)


async def test_nothing_finished_means_no_per_episode_figure(people):
    await _job("a", "ep01", "L4_video", 600_000)
    finished = (await cr.build_cost_truth(_db(), NOW))["per_finished"]
    assert finished["finished_episodes"] == 0
    assert finished["per_episode_usd_micros"] is None and finished["per_minute_usd_micros"] is None


# ------------------------------------------------------------------ warnings

async def test_warnings_say_what_is_missing(people, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_USD_PER_CREDIT", "a lot")
    await _job("still-1", "ep01", "L2_drafts", 100_000)
    truth = await cr.build_cost_truth(_db(), NOW)
    alerts = {"configured": False, "warnings": ["No alert destination is configured: ...", "webhook: bad URL"]}
    balance = {"consistency": {"unchanged_despite_spend": True, "set_at": "2026-11-14T09:00:00+00:00",
                               "previous_set_at": "2026-11-13T09:00:00+00:00", "spent_between_usd": "16.65"},
               "schedule": {"enabled": True, "overdue": True, "last_run_at": None, "problem": None}}
    warnings = cr.cost_warnings(truth, alerts, balance)
    assert [w["code"] for w in warnings] == ["partial_coverage", "usage_never_synced", "shared_login_spend",
                                             "bad_rate", "alerts_unconfigured", "alerts_config",
                                             "balance_unchanged", "balance_schedule_overdue"]
    unchanged = next(w for w in warnings if w["code"] == "balance_unchanged")
    assert "$16.65" in unchanged["message"] and "web app credits" in unchanged["message"]


@pytest.mark.parametrize("schedule,code", [
    ({"enabled": True, "overdue": False, "problem": "BALANCE_CHECK_INTERVAL_MINUTES must be positive, got 0"},
     "balance_schedule_config"),
    ({"enabled": False, "overdue": False, "problem": None}, "balance_schedule_disabled"),
    ({"enabled": True, "overdue": False, "problem": None}, None),
])
def test_schedule_warnings(schedule, code):
    codes = [w["code"] for w in cr.cost_warnings(None, None, {"schedule": schedule})]
    assert codes == ([code] if code else [])


async def test_failed_and_unmatched_usage_syncs_are_warned_about(people, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    row = pu.UsageRow(bucket_start="2026-11-12T00:00:00Z", model="kling-video/v2.1/pro/image-to-video",
                      unit="generation", auth_method="", quantity=Decimal("2"), unit_price=None,
                      cost=Decimal("1.30"), currency="USD")
    async with aiosqlite.connect(_db()) as db:
        await pu.store_usage_rows(db, [row])
        await pu.allocate_usage(db, now=NOW, env={"HIGGSFIELD_API_KEY": "key-studio:secret-not-real"})
        await db.execute("INSERT INTO cost_sync_runs (provider, ran_at, ok, summary, error) VALUES "
                         "('higgsfield', '2026-11-15T11:00:00+00:00', 0, '{}', 'GET /models/usage -> HTTP 401')")
        await db.commit()
    truth = await cr.build_cost_truth(_db(), NOW)
    reported = truth["provider_reported"]
    assert reported["higgsfield_month_usd_micros"]["unmatched"] == 1_300_000
    assert reported["unmatched_buckets"][0]["reason"].startswith("unmatched: no studio generations")
    assert reported["last_sync"]["ok"] is False
    codes = {w["code"]: w["message"] for w in cr.cost_warnings(truth)}
    assert "HTTP 401" in codes["usage_sync_failed"] and "$1.30" in codes["unmatched_provider_spend"]
