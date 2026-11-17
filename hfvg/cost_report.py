"""The dashboard's cost truth: what providers charged next to what was estimated (docs/ACTUAL-COSTS.md).

Spend of a job is its best number: the provider-reported charge where one is recorded (actual),
else what the dollar guard committed (an estimate). Committed jobs count; jobs still holding a
reservation are reported separately as in flight; released jobs cost nothing. Coverage is the share
of spend dollars that are provider-reported. There is one month-end projection (month_spend,
monthly_projection): the cost truth's and the ledger run-rate's are the same numbers.

"Actual vs estimate" compares only jobs that have both numbers, so a job without an actual never
reads as an exact estimate.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from hfvg.actual_costs import (
    HIGGSFIELD_USD_PER_CREDIT_ENV,
    elevenlabs_rate_str,
    ensure_cost_schema,
    higgsfield_rate,
    pct,
)
from hfvg.spend_attribution import load_attribution

STAGES = {
    "L1_refs": "references",
    "L2_drafts": "draft_stills",
    "L3_final_stills": "final_stills",
    "L4_video": "video_clips",
    "L5_audio": "audio",
    "L6_reserve": "assembly",
    "L7_revision": "revisions",
    "el_vo_takes": "voice_over",
    "el_sfx": "sound_effects",
    "el_music_one_take": "music",
    "U1_mood_board": "mood_boards",
}
UNATTRIBUTED = "unattributed"


def provider_of(line_name: str | None) -> str:
    return "elevenlabs" if (line_name or "").startswith("el_") else "higgsfield"


def stage_of(line_name: str | None) -> str:
    return STAGES.get(line_name or "", line_name or "unknown")


class Totals:
    """Running sums of committed jobs for one group."""

    def __init__(self) -> None:
        self.jobs = self.spend = self.actual = self.with_actual = 0
        self.estimate = self.with_estimate = 0
        self.compared = self.compared_estimate = self.compared_actual = 0
        self.backfilled = 0

    def add(self, job: dict[str, Any]) -> None:
        actual, estimate = job["actual_usd_micros"], job["estimate_usd_micros"]
        self.jobs += 1
        self.spend += actual if actual is not None else job["usd_micros"]
        if actual is not None:
            self.actual += actual
            self.with_actual += 1
            if job["actual_backfilled"]:
                self.backfilled += actual
        if estimate is not None:
            self.estimate += estimate
            self.with_estimate += 1
        if actual is not None and estimate is not None:
            self.compared += 1
            self.compared_estimate += estimate
            self.compared_actual += actual

    def out(self) -> dict[str, Any]:
        delta = self.compared_actual - self.compared_estimate if self.compared else None
        return {
            "jobs": self.jobs,
            "spend_usd_micros": self.spend,
            "actual_usd_micros": self.actual,
            "jobs_with_actual": self.with_actual,
            "coverage_pct": pct(self.actual, self.spend),
            "estimate_usd_micros": self.estimate,
            "jobs_with_estimate": self.with_estimate,
            "compared_jobs": self.compared,
            "compared_estimate_usd_micros": self.compared_estimate,
            "compared_actual_usd_micros": self.compared_actual,
            "delta_usd_micros": delta,
            "delta_pct": pct(delta, self.compared_estimate) if delta is not None else None,
            "backfilled_usd_micros": self.backfilled,
        }


def _month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start, end


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


IN_FLIGHT = ("reserved", "pending_reconcile")


def month_spend(job: dict[str, Any]) -> tuple[int, int | None] | None:
    """What a job adds to the month's spend so far, as (spend, actual or None); None when nothing.

    A committed job adds its best number. A job still in flight adds only a charge its provider has
    already reported: its hold is not spend until it is committed. A released job costs nothing.
    The dashboard's projection and the ledger run-rate (CostTracker.get_monthly_runrate) both use
    this, and the headline (spend_headline) uses it for committed jobs.
    """
    actual = job["actual_usd_micros"]
    if job["status"] == "committed":
        return (actual if actual is not None else job["usd_micros"] or 0), actual
    if job["status"] in IN_FLIGHT and actual is not None:
        return actual, actual
    return None


PROJECTION_BASIS = ("committed spend so far this month (provider-reported where recorded, the guard's "
                    "estimates for the rest, plus charges already reported for jobs in flight; holds are "
                    "not spend) divided by the days elapsed, times the days in the month")


def monthly_projection(spend_by_time: list[tuple[datetime, int, int | None]], now: datetime) -> dict[str, Any]:
    """Month-end projection from this month's spend so far: (time, spend, actual or None) per job."""
    start, end = _month_bounds(now)
    month = [(s, a) for t, s, a in spend_by_time if start <= t < end and t <= now]
    spend = sum(s for s, _ in month)
    actual = sum(a for _, a in month if a is not None)
    elapsed_days = max((now - start).total_seconds() / 86400, 1.0)
    days_in_month = (end - start).days
    daily = spend / elapsed_days
    return {
        "month": start.strftime("%Y-%m"),
        "days_elapsed": round(elapsed_days, 2),
        "days_in_month": days_in_month,
        "spend_so_far_usd_micros": spend,
        "actual_so_far_usd_micros": actual,
        "coverage_pct": pct(actual, spend),
        "daily_rate_usd_micros": int(daily),
        "projected_month_end_usd_micros": int(daily * days_in_month) if spend else 0,
        "basis": PROJECTION_BASIS,
    }


def _month_timeline(jobs: list[dict[str, Any]]) -> list[tuple[datetime, int, int | None]]:
    timeline = []
    for job in jobs:
        counted, created = month_spend(job), _ts(job["created_at"])
        if counted is not None and created is not None:
            timeline.append((created, *counted))
    return timeline


async def month_projection_from_db(db_path: str, now: datetime, user_id: str | None = None) -> dict[str, Any]:
    """monthly_projection over the job ledger (optionally one owner's jobs), by the month_spend rule."""
    async with aiosqlite.connect(db_path, uri=True) as db:
        query = "SELECT status, usd_micros, actual_usd_micros, created_at FROM job_ledger"
        params: tuple[str, ...] = ()
        if user_id:
            query, params = query + " WHERE user_id = ?", (user_id,)
        async with db.execute(query, params) as cur:
            jobs = [{"status": s, "usd_micros": u, "actual_usd_micros": a, "created_at": c}
                    async for s, u, a, c in cur]
    return monthly_projection(_month_timeline(jobs), now)


def spend_headline(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """The headline spend (dashboard card, spend_summary, CSV and JSON exports): committed jobs at their
    best number, with how much of it is provider-reported."""
    spend = actual = with_actual = committed = 0
    for job in jobs:
        counted = month_spend(job) if job["status"] == "committed" else None
        if counted is None:
            continue
        committed += 1
        spend += counted[0]
        if counted[1] is not None:
            actual += counted[1]
            with_actual += 1
    label = "estimated" if not with_actual else ("actual" if with_actual == committed else "actual and estimated")
    return {"spend_usd_micros": spend, "actual_usd_micros": actual, "coverage_pct": pct(actual, spend),
            "label": label, "basis": "committed jobs: provider-reported charges where recorded, the guard's "
                                     "committed estimates for the rest"}


def episode_delivery(episode_id: str, root: Path | None = None) -> dict[str, Any]:
    """The finished picture's length from the episode's delivery manifest (None when unknown)."""
    from hfvg.paths import episode_dir
    path = (root / episode_id if root is not None else episode_dir(episode_id)) / "delivery" / "delivery_manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"duration_s": None, "dry_run": None}
    try:
        duration = float(manifest.get("picture_duration_s"))
    except (TypeError, ValueError):
        duration = None
    return {"duration_s": duration if duration and duration > 0 else None, "dry_run": bool(manifest.get("dry_run"))}


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
    async with db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)) as cur:
        return await cur.fetchone() is not None


async def _final_cut_approvals(db: aiosqlite.Connection) -> dict[str, str]:
    if not await _table_exists(db, "audit_log"):
        return {}
    out: dict[str, str] = {}
    async with db.execute(
        "SELECT episode_id, MAX(timestamp) FROM audit_log WHERE action = 'approve_g_final_cut' GROUP BY episode_id"
    ) as cur:
        async for episode_id, at in cur:
            key = episode_id[: -len("-episode")] if episode_id.endswith("-episode") else episode_id
            out[key] = max(at or "", out.get(key, ""))
    return out


async def _people(db: aiosqlite.Connection) -> dict[str, dict[str, Any]]:
    if not await _table_exists(db, "users"):
        return {}
    async with db.execute("SELECT user_id, name, email, role FROM users") as cur:
        return {uid: {"name": name, "email": email, "role": role} async for uid, name, email, role in cur}


async def _provider_reported(db: aiosqlite.Connection, now: datetime) -> dict[str, Any]:
    start, end = _month_bounds(now)
    month = (start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
    totals = {"total": 0, "allocated": 0, "unmatched": 0, "other_api_key": 0, "pending": 0}
    async with db.execute(
        "SELECT allocation, SUM(cost_usd_micros) FROM provider_usage_buckets WHERE provider = 'higgsfield' "
        "AND timeframe = 'day' AND substr(bucket_start, 1, 10) >= ? AND substr(bucket_start, 1, 10) < ? "
        "GROUP BY allocation", month,
    ) as cur:
        async for allocation, cost in cur:
            cost = int(cost or 0)
            totals["total"] += cost
            if allocation is None:
                totals["pending"] += cost
            elif allocation == "allocated":
                totals["allocated"] += cost
            elif allocation == "other_api_key":
                totals["other_api_key"] += cost
            else:
                totals["unmatched"] += cost
    async with db.execute(
        "SELECT substr(bucket_start, 1, 10), model, unit, quantity, cost_usd_micros, allocation "
        "FROM provider_usage_buckets WHERE provider = 'higgsfield' AND timeframe = 'day' "
        "AND allocation LIKE 'unmatched%' ORDER BY bucket_start DESC LIMIT 10"
    ) as cur:
        unmatched = [{"day": d, "model": m, "unit": u, "quantity": q, "cost_usd_micros": c, "reason": a}
                     async for d, m, u, q, c, a in cur]
    async with db.execute(
        "SELECT ran_at, ok, summary, error FROM cost_sync_runs WHERE provider = 'higgsfield' ORDER BY id DESC LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    last_sync = None
    if row:
        try:
            summary = json.loads(row[2])
        except ValueError:
            summary = {}
        last_sync = {"ran_at": row[0], "ok": bool(row[1]), "error": row[3], "mode": summary.get("mode"),
                     "summary": summary}
    return {"month": start.strftime("%Y-%m"),
            "higgsfield_month_usd_micros": totals,
            "unmatched_buckets": unmatched, "last_sync": last_sync}


def _rates() -> dict[str, Any]:
    from hfvg.providers.elevenlabs import USD_PER_CREDIT_ENV
    hf_rate, hf_problem = higgsfield_rate()
    return {
        "elevenlabs": {"usd_per_credit": elevenlabs_rate_str(), "env": USD_PER_CREDIT_ENV,
                       "configured": bool(os.getenv(USD_PER_CREDIT_ENV, "").strip())},
        "higgsfield": {"usd_per_credit": str(hf_rate) if hf_rate is not None else None,
                       "env": HIGGSFIELD_USD_PER_CREDIT_ENV, "problem": hf_problem,
                       "note": "the API bills in USD; the rate only converts charges reported in credits"},
    }


async def build_cost_truth(db_path: str, now: datetime | None = None,
                           episodes_root: Path | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path, uri=True) as db:
        await ensure_cost_schema(db)
        await db.commit()
        jobs: list[dict[str, Any]] = []
        if await _table_exists(db, "job_ledger"):
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT job_id, episode_id, user_id, model, line_name, status, usd_micros, estimate_usd_micros, "
                "estimate_source, actual_usd_micros, actual_source, actual_backfilled, created_at FROM job_ledger"
            ) as cur:
                jobs = [dict(r) async for r in cur]
            db.row_factory = None
        attribution = await load_attribution(db)
        approvals = await _final_cut_approvals(db)
        people = await _people(db)
        provider_reported = await _provider_reported(db, now)

    overall, in_flight = Totals(), {"jobs": 0, "held_usd_micros": 0}
    by_provider: dict[str, Totals] = defaultdict(Totals)
    by_stage: dict[str, Totals] = defaultdict(Totals)
    by_episode: dict[str, Totals] = defaultdict(Totals)
    by_episode_provider: dict[str, dict[str, Totals]] = defaultdict(lambda: defaultdict(Totals))
    by_person: dict[str, Totals] = defaultdict(Totals)
    person_episodes: dict[str, set[str]] = defaultdict(set)
    person_sources: dict[str, set[str]] = defaultdict(set)
    episode_people: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    by_source: dict[str, int] = defaultdict(int)
    daily: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    unattributed_reasons: dict[str, int] = defaultdict(int)

    for job in jobs:
        status = job["status"]
        if status in IN_FLIGHT:
            in_flight["jobs"] += 1
            in_flight["held_usd_micros"] += job["usd_micros"] or 0
            continue
        if status != "committed":
            continue
        job["usd_micros"] = job["usd_micros"] or 0
        episode_id, provider = job["episode_id"], provider_of(job["line_name"])
        person, why = attribution.attribute(episode_id, job["user_id"], job["created_at"])
        key = person or UNATTRIBUTED
        for group in (overall, by_provider[provider], by_stage[stage_of(job["line_name"])], by_episode[episode_id],
                      by_episode_provider[episode_id][provider], by_person[key]):
            group.add(job)
        spend = job["actual_usd_micros"] if job["actual_usd_micros"] is not None else job["usd_micros"]
        person_episodes[key].add(episode_id)
        person_sources[key].add(why)
        episode_people[episode_id][key] += spend
        if person is None:
            unattributed_reasons[why] += spend
        by_source[job["actual_source"] or "estimate_only"] += spend
        created = _ts(job["created_at"])
        if created is not None:
            day = daily[created.date().isoformat()]
            day[0] += spend
            day[1] += job["actual_usd_micros"] or 0

    def person_label(key: str) -> dict[str, Any]:
        info = people.get(key) or {}
        return {"user_id": None if key == UNATTRIBUTED else key,
                "name": info.get("name") or ("Unattributed" if key == UNATTRIBUTED else key),
                "email": info.get("email")}

    episodes_out = []
    finished_spend = finished_actual = 0
    minutes_spend = 0
    finished_minutes = 0.0
    finished: list[str] = []
    for episode_id, totals in by_episode.items():
        delivery = episode_delivery(episode_id, episodes_root) if episode_id in approvals else \
            {"duration_s": None, "dry_run": None}
        is_finished = episode_id in approvals and totals.spend > 0 and not delivery["dry_run"]
        top = max(episode_people[episode_id].items(), key=lambda kv: kv[1])[0] if episode_people[episode_id] else None
        _, why = attribution.attribute(episode_id)
        row = {"episode_id": episode_id, **totals.out(),
               "providers": {p: t.out() for p, t in sorted(by_episode_provider[episode_id].items())},
               "person": person_label(top) if top else None,
               "attribution": "assigned" if episode_id in attribution.assigned else why,
               "finished": is_finished, "final_cut_approved_at": approvals.get(episode_id),
               "duration_s": delivery["duration_s"]}
        if is_finished:
            finished.append(episode_id)
            finished_spend += totals.spend
            finished_actual += totals.actual
            if delivery["duration_s"]:
                finished_minutes += delivery["duration_s"] / 60
                minutes_spend += totals.spend
        episodes_out.append(row)
    episodes_out.sort(key=lambda r: r["spend_usd_micros"], reverse=True)

    per_finished = {
        "finished_episodes": len(finished),
        "finished_with_duration": sum(1 for r in episodes_out if r["finished"] and r["duration_s"]),
        "finished_minutes": round(finished_minutes, 3),
        "finished_spend_usd_micros": finished_spend,
        "finished_coverage_pct": pct(finished_actual, finished_spend),
        "per_episode_usd_micros": finished_spend // len(finished) if finished else None,
        "per_minute_usd_micros": int(minutes_spend / finished_minutes) if finished_minutes else None,
        "all_spend_per_finished_episode_usd_micros": overall.spend // len(finished) if finished else None,
        "definition": "finished = final cut approved with paid spend; minutes from the delivery manifest's "
                      "picture length. 'All spend per finished episode' also carries unfinished episodes.",
    }

    people_out = []
    for key, totals in by_person.items():
        people_out.append({**person_label(key), **totals.out(), "episodes": len(person_episodes[key]),
                           "attribution": sorted(person_sources[key])})
    people_out.sort(key=lambda r: r["spend_usd_micros"], reverse=True)

    days = sorted(daily)[-30:]
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "unit": "usd_micros",
        "totals": overall.out(),
        "in_flight": in_flight,
        "by_actual_source": dict(sorted(by_source.items())),
        "by_provider": [{"provider": p, **t.out()} for p, t in sorted(by_provider.items())],
        "by_stage": [{"stage": s, **t.out()} for s, t in sorted(by_stage.items(), key=lambda kv: -kv[1].spend)],
        "by_episode": episodes_out[:50],
        "by_person": people_out,
        "unattributed_usd_micros": dict(unattributed_reasons),
        "per_finished": per_finished,
        "monthly": monthly_projection(_month_timeline(jobs), now),
        "daily": [{"date": d, "spend_usd_micros": daily[d][0], "actual_usd_micros": daily[d][1]} for d in days],
        "provider_reported": provider_reported,
        "rates": _rates(),
    }


def _truth_warnings(truth: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    totals = truth["totals"]
    if totals["spend_usd_micros"] and (totals["coverage_pct"] or 0) < 100:
        out.append({"level": "info", "code": "partial_coverage",
                    "message": f"{totals['coverage_pct'] or 0}% of committed spend has provider-reported numbers; "
                               "the rest are the guard's estimates."})
    sync = truth["provider_reported"]["last_sync"]
    dry_run = os.getenv("DRY_RUN", "true").strip().lower() in ("true", "1")
    if sync is None and not dry_run:
        out.append({"level": "warning", "code": "usage_never_synced",
                    "message": "The Higgsfield usage report has never been read, so Higgsfield jobs have no "
                               "actual cost yet (it runs with the balance check schedule, or Sync now)."})
    elif sync is not None and not sync["ok"]:
        out.append({"level": "warning", "code": "usage_sync_failed",
                    "message": f"Last Higgsfield usage sync ({sync['ran_at']}) did not run: {sync['error']}"})
    unmatched = truth["provider_reported"]["higgsfield_month_usd_micros"]["unmatched"]
    if unmatched:
        out.append({"level": "warning", "code": "unmatched_provider_spend",
                    "message": f"Higgsfield reports ${unmatched / 1_000_000:.2f} this month that could not be "
                               "matched to studio jobs (see Unmatched provider spend)."})
    shared = truth["unattributed_usd_micros"].get("unattributed_shared_login", 0)
    if shared:
        out.append({"level": "warning", "code": "shared_login_spend",
                    "message": f"${shared / 1_000_000:.2f} was spent by runs started with the shared admin "
                               "login; assign those episodes to a person."})
    problem = truth["rates"]["higgsfield"]["problem"]
    if problem:
        out.append({"level": "warning", "code": "bad_rate", "message": problem})
    return out


def cost_warnings(truth: dict[str, Any] | None, alerts: dict[str, Any] | None = None,
                  balance: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Plain-language problems the dashboard shows above the numbers."""
    out: list[dict[str, str]] = []
    if truth is not None:
        out += _truth_warnings(truth)
    if alerts is not None:
        for i, message in enumerate(alerts.get("warnings", [])):
            code = "alerts_unconfigured" if i == 0 and not alerts.get("configured") else "alerts_config"
            out.append({"level": "warning", "code": code, "message": message})
    consistency = (balance or {}).get("consistency") or {}
    if consistency.get("unchanged_despite_spend"):
        out.append({"level": "warning", "code": "balance_unchanged",
                    "message": f"The Higgsfield balance entered at {consistency['set_at']} equals the previous "
                               f"entry ({consistency['previous_set_at']}) although the studio spent "
                               f"${consistency['spent_between_usd']} in between: it was probably read from "
                               "another wallet (web app credits, not the API's USD balance) or a cached page."})
    schedule = (balance or {}).get("schedule") or {}
    if schedule.get("problem"):
        out.append({"level": "warning", "code": "balance_schedule_config", "message": schedule["problem"]})
    elif schedule.get("overdue"):
        out.append({"level": "warning", "code": "balance_schedule_overdue",
                    "message": "No scheduled balance check has run in the last two intervals "
                               f"(last: {schedule.get('last_run_at') or 'never'}); check that the worker is up."})
    elif schedule and not schedule.get("enabled"):
        out.append({"level": "warning", "code": "balance_schedule_disabled",
                    "message": "BALANCE_CHECK_SCHEDULE_ENABLED is false: balances are only read before paid "
                               "steps and on demand, and scheduled alerts do not fire."})
    return out
