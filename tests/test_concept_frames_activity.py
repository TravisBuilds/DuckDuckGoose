"""First frame options (hfvg/activities/concept_frame.py, hfvg/workflows/concept_frame.py,
docs/CONCEPT-FRAMES.md): every paid option goes through the guarded still path on the episode's draft
stills line (provider estimate within the approved share, balance gate, credit and USD reserves, a
per-job ledger row in the episode owner's name, commit or release, reconciler); dry runs make
placeholders without spend; the workflow makes a round's options in order on its own task queue and
stops at the first refusal. The provider is faked: no credits are spent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from hfvg import cast_store, concept_frames, paths
from hfvg.activities import concept_frame as frame_activity
from hfvg.providers.base import ProviderJob, ProviderJobStatus
from tests.cast_helpers import image_bytes, new_place, new_prop, prop_photo

EP = "ep82frames"
OWNER_ID = "usr_6a7b8c9d0e1f"
OUTPUT = "https://cdn.test/frame-out.png"
BASE = "https://api.higgsfield.ai"
PRICE = 30_000
MONEY_TABLES = ("budget_lines", "budget_transactions", "job_ledger", "credit_lines", "credit_transactions",
                "provider_jobs")


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    for var in ("MODEL_PATH_STILL", "CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX", "CONCEPT_FRAME_OPTIONS",
                "SMOKE_TEST_OVERRIDE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(frame_activity, "POLL_INTERVAL_S", 0)


async def _live_db(tmp_path, monkeypatch, *, balance: str | None = "100.00", live: bool = True) -> str:
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import create_episode, init_studio_db, set_live_mode
    from tests.ledger_helpers import set_balance

    db_path = str(tmp_path / "frames.db")
    await init_studio_db(db_path)
    await create_episode(db_path, EP, user_id=OWNER_ID)
    await BudgetLedger(db_path).init_db()
    await set_live_mode(db_path, EP, live)
    if balance is not None:
        await set_balance(db_path, usd=balance, episode_id="ep-unrelated")
    for key, value in {"DATABASE_PATH": db_path, "DRY_RUN": "false", "SMOKE_TEST_OVERRIDE": "true",
                       "HIGGSFIELD_API_KEY": "test_id:test_secret", "HIGGSFIELD_BASE_URL": BASE}.items():
        monkeypatch.setenv(key, value)
    return db_path


class FakeProvider:
    """HiggsfieldStillProvider stand-in: records every call; statuses are returned in order."""

    instances: list["FakeProvider"] = []
    estimate = (PRICE, 0.5)
    statuses: list[ProviderJobStatus] = [ProviderJobStatus.COMPLETED]
    submitted = 0

    def __init__(self, model_path=None, **_kw):
        self.model_path = model_path
        self.calls: list[tuple] = []
        self.polls = 0
        FakeProvider.instances.append(self)

    async def upload_reference(self, path, index):
        self.calls.append(("upload", Path(path).name))
        return {"public_url": f"https://cdn.test/ref-{Path(path).name}"}

    async def estimate_usd_and_credits(self, **params):
        self.calls.append(("estimate", params))
        return self.estimate

    async def submit_image(self, image_urls, idempotency_key, **params):
        FakeProvider.submitted += 1
        self.calls.append(("submit", {"image_urls": image_urls, "idempotency_key": idempotency_key, **params}))
        return f"prov-frame-{FakeProvider.submitted}"

    async def get_job_status(self, provider_job_id):
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        self.calls.append(("poll", provider_job_id))
        return ProviderJob(job_id=provider_job_id, status=status,
                           error="boom" if status == ProviderJobStatus.FAILED else None,
                           output_url=OUTPUT if status == ProviderJobStatus.COMPLETED else None)

    async def close(self):
        pass


@pytest.fixture
def provider(monkeypatch):
    from hfvg.providers import higgsfield_still
    FakeProvider.instances = []
    FakeProvider.estimate = (PRICE, 0.5)
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    FakeProvider.submitted = 0
    monkeypatch.setattr(higgsfield_still, "HiggsfieldStillProvider", FakeProvider)
    return FakeProvider


@pytest.fixture
def http():
    """Provider HTTP besides the faked provider: the balance monitor's usage lookup and downloads."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
        router.get(OUTPUT).mock(return_value=httpx.Response(200, content=image_bytes(size=(576, 1024), seed=77)))
        yield router


def _episode() -> None:
    """Two shots at the harbour cabin; the first holds the brass key. Each place and prop has a photo."""
    library = ("episode", EP)
    cabin = new_place(library, "Harbour cabin")
    prop_photo(cabin, seed=4, color=(40, 60, 200))
    key = new_prop(library, "Brass key", description="a small brass key on a red string")
    prop_photo(key, seed=5, color=(200, 160, 40))
    shots = [{"id": sid, "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
              "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16", "ref_keys": [],
              "prompt": prompt, "place": "harbour-cabin", **extra}
             for sid, prompt, extra in (("A01", "A brass key glints on the cabin table by a storm lamp.",
                                         {"props": ["brass-key"]}),
                                        ("A02", "The cabin door swings open onto the foggy quay.", {}))]
    folder = paths.episode_dir(EP)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "shots.json").write_text(json.dumps({"episode_id": EP, "shots": shots}), encoding="utf-8")


def _request(shot: str = "A01", *, approved: int = 40_000, change: str | None = None) -> list[dict]:
    """A live round as api/concept_frame_routes.py records it once the owner approved the price:
    its option jobs, each with its share of the approved total."""
    plan = concept_frames.plan_round(EP, shot, change=change, live=True)
    set_id = concept_frames.new_set_id()
    jobs = [{**job, "estimated_usd_micros": PRICE, "approved_usd_micros": approved}
            for job in concept_frames.plan_jobs(plan, set_id=set_id)]
    concept_frames.start_set(plan, jobs, set_id=set_id, workflow_id=f"concept-frames-{set_id}",
                             estimate_usd_micros=PRICE * len(jobs), approved_total_usd_micros=approved * len(jobs),
                             requested_by=OWNER_ID)
    return jobs


async def _run(job: dict, beats: list | None = None) -> dict:
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)
    return await env.run(frame_activity.generate_concept_frame_activity, job)


async def _attempt(job: dict) -> Exception | None:
    """What the activity stopped with (None when it finished). The spend checks come before the
    error type, so a skipped guard fails on the spend it lets through, whatever breaks after it."""
    try:
        await _run(job)
    except Exception as e:
        return e
    return None


def _calls(kind: str) -> list:
    return [c for p in FakeProvider.instances for c in p.calls if c[0] == kind]


def _status(job_id: str) -> str:
    return concept_frames.option(job_id)["status"]


async def _credit_txns(db_path: str, job_id: str) -> list[str]:
    from hfvg.credit_ledger import CreditLedger
    await CreditLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT txn_type FROM credit_transactions WHERE job_id = ? ORDER BY rowid",
                              (job_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def _ledger_row(db_path: str, job_id: str) -> dict | None:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def _line(db_path: str) -> dict:
    from hfvg.budget import BudgetLedger
    return await BudgetLedger(db_path).get_line_status(EP, concept_frames.LINE)


def _money_rows(db_path: str) -> dict:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MONEY_TABLES if t in names}


# ------------------------------------------------------------------------------------- before any work

async def test_dry_run_makes_a_placeholder_without_spend(provider):
    _episode()
    jobs = _request()
    assert [j["job_id"] for j in jobs] == [concept_frames.option_id_for(jobs[0]["set_id"], k) for k in range(3)]
    assert len(jobs[0]["refs"]) == 2 and jobs[0]["model_path"] == "xai/grok-imagine-image-2.0"
    before = _money_rows(cast_store.db_path())
    result = await _run(jobs[0])
    assert result == {"job_id": jobs[0]["job_id"], "status": "completed", "usd_micros": 0, "placeholder": True}
    row = concept_frames.option(jobs[0]["job_id"])
    assert (row["status"], row["placeholder"], row["width"], row["height"]) == ("completed", 1, 576, 1024)
    assert FakeProvider.instances == [] and _money_rows(cast_store.db_path()) == before
    assert (await _run(jobs[0]))["status"] == "reused"


async def test_a_job_that_is_not_recorded_or_whose_round_ended_makes_nothing(provider):
    _episode()
    jobs = _request()
    stranger = {**jobs[0], "set_id": "cf_000000000000", "job_id": "cframe-000000000000-0"}
    err = await _attempt(stranger)
    assert isinstance(err, ApplicationError) and err.type == "FrameRefused" and "not recorded" in str(err)
    concept_frames.finish_set(jobs[0]["set_id"])
    err = await _attempt(jobs[1])
    assert isinstance(err, ApplicationError) and err.non_retryable and "ended" in str(err)
    assert _status(jobs[1]["job_id"]) == "refused" and concept_frames.option(jobs[1]["job_id"])["sha256"] is None


@pytest.mark.parametrize("approved", [0, -5, None, True, "40000"])
async def test_a_job_without_an_approved_price_spends_nothing(provider, approved):
    _episode()
    job = {**_request()[0], "approved_usd_micros": approved}
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "FrameRefused" and "approved" in str(err)
    assert FakeProvider.instances == [] and _status(job["job_id"]) == "refused"


async def test_a_job_whose_id_does_not_match_its_round_or_misses_a_key_is_refused(provider):
    _episode()
    job = _request()[0]
    err = await _attempt({**job, "position": 2})
    assert isinstance(err, ApplicationError) and "does not match its round" in str(err)
    err = await _attempt({k: v for k, v in job.items() if k != "ref_shas"})
    assert isinstance(err, ApplicationError) and "missing ['ref_shas']" in str(err)
    assert FakeProvider.instances == []


async def test_a_reference_photo_changed_since_the_plan_is_refused(provider):
    _episode()
    job = _request()[0]
    Path(job["refs"][0]).write_bytes(image_bytes(seed=99))
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "FrameRefused" and "changed" in str(err)
    assert _status(job["job_id"]) == "refused" and FakeProvider.instances == []


@pytest.mark.parametrize("model", ["gpt_image_2", "someone/unknown-model"])
async def test_a_model_the_server_cannot_route_is_refused_before_any_work(provider, model):
    _episode()
    job = {**_request()[0], "model_path": model}
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "FrameRefused"
    assert FakeProvider.instances == [] and _status(job["job_id"]) == "refused"


async def test_words_the_studio_cannot_generate_are_refused_by_the_activity_too(provider):
    _episode()
    job = {**_request()[0], "prompt": "A brass key, gore everywhere."}
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and "content policy" in str(err)
    assert FakeProvider.instances == [] and _status(job["job_id"]) == "refused"


# ------------------------------------------------------------------------------------- live: the guards

async def test_live_needs_the_episode_in_live_mode(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch, live=False)
    _episode()
    job = _request()[0]
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "NotLive", err
    assert FakeProvider.instances == [] and await _credit_txns(db_path, job["job_id"]) == []
    assert _status(job["job_id"]) == "refused"


async def test_live_needs_a_passing_smoke_test(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    monkeypatch.delenv("SMOKE_TEST_OVERRIDE")
    _episode()
    job = _request()[0]
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "SmokeTestRequired", err
    assert FakeProvider.instances == [] and await _credit_txns(db_path, job["job_id"]) == []


async def test_live_needs_the_provider_key(tmp_path, monkeypatch, provider, http):
    await _live_db(tmp_path, monkeypatch)
    monkeypatch.delenv("HIGGSFIELD_API_KEY")
    _episode()
    job = _request()[0]
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "MissingCredentials", err
    assert FakeProvider.instances == [] and _status(job["job_id"]) == "refused"


async def test_live_estimate_above_the_approved_share_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    FakeProvider.estimate = (50_000, 0.8)
    job = _request(approved=40_000)[0]
    err = await _attempt(job)
    assert _calls("estimate") and _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == []
    line = await _line(db_path)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert await _ledger_row(db_path, job["job_id"]) is None
    assert isinstance(err, ApplicationError) and err.non_retryable and err.type == "EstimateAboveApproved", err
    assert _status(job["job_id"]) == "failed"


async def test_live_estimate_failure_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()

    async def no_price(self, **params):
        raise ValueError("Failed to get USD/credits estimate for still generation: no usd")
    monkeypatch.setattr(FakeProvider, "estimate_usd_and_credits", no_price)
    job = _request()[0]
    with pytest.raises(ValueError, match="estimate"):
        await _run(job)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    assert _status(job["job_id"]) == "failed"


async def test_live_balance_gate_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch, balance="0.01")
    _episode()
    job = _request()[0]
    err = await _attempt(job)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    line = await _line(db_path)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert isinstance(err, ApplicationError) and err.non_retryable and err.type == "InsufficientBalance", err
    assert "balance insufficient" in str(err)


async def test_live_without_a_manual_balance_the_usd_reserve_refuses(tmp_path, monkeypatch, provider, http):
    """GC.01: the USD reserve needs a recorded balance; the credit hold taken before it is released."""
    db_path = await _live_db(tmp_path, monkeypatch, balance=None)
    _episode()
    job = _request()[0]
    with pytest.raises(ValueError, match="GC.01: no manual Higgsfield balance recorded"):
        await _run(job)
    assert _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path)
    assert (line["reserved"], line["spent"]) == (0, 0)


async def test_live_the_draft_line_stop_refuses_the_reserve(tmp_path, monkeypatch, provider, http):
    from hfvg.budget import BudgetLedger
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    await frame_activity._ensure_budget_lines(db_path, EP)
    line = await _line(db_path)
    ledger = BudgetLedger(db_path)
    held = line["stop_threshold"] - PRICE + 1
    assert await ledger.reserve(EP, concept_frames.LINE, held, "earlier drafts", job_id="draft-earlier")
    job = _request()[0]
    err = await _attempt(job)
    assert _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    assert (await _line(db_path))["reserved"] == held
    assert isinstance(err, ApplicationError) and err.type == "BudgetReserveRefused", err
    # the line is never reset or raised by the next option
    await frame_activity._ensure_budget_lines(db_path, EP)
    assert (await _line(db_path))["budget_cap"] == line["budget_cap"]


async def test_live_success_reserves_submits_commits_records_the_ledger_and_keeps_the_picture(
        tmp_path, monkeypatch, provider, http):
    from hfvg.cost_report import build_cost_truth
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING, ProviderJobStatus.COMPLETED]
    job = _request()[0]
    beats: list = []
    result = await _run(job, beats)
    assert result == {"job_id": job["job_id"], "status": "completed", "provider_job_id": "prov-frame-1",
                      "usd_micros": PRICE}
    kinds = [c[0] for p in FakeProvider.instances for c in p.calls]
    assert kinds == ["upload", "upload", "estimate", "submit", "poll", "poll"]
    submit = _calls("submit")[0][1]
    assert submit["idempotency_key"].startswith(job["job_id"] + "-")
    assert submit["image_urls"] == [f"https://cdn.test/ref-{Path(p).name}" for p in job["refs"]]
    assert (submit["prompt"], submit["aspect_ratio"], submit["resolution"], submit["quality"]) == (
        job["prompt"], "9:16", "1k", "medium")
    assert beats, "the activity heartbeats while it polls the provider"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)
    row = await _ledger_row(db_path, job["job_id"])
    assert (row["episode_id"], row["shot_id"], row["user_id"], row["line_name"], row["tier"], row["status"],
            row["usd_micros"]) == (EP, "A01", OWNER_ID, "L2_drafts", "draft", "committed", PRICE)
    option = concept_frames.option(job["job_id"])
    assert option["status"] == "completed" and option["placeholder"] == 0
    assert cast_store.image_file(option["sha256"], option["ext"]).is_file()
    truth = await build_cost_truth(db_path)
    stages = {s["stage"]: s["spend_usd_micros"] for s in truth["by_stage"]}
    assert stages.get("draft_stills") == PRICE, stages
    person = next(p for p in truth["by_person"] if p["user_id"] == OWNER_ID)
    assert person["spend_usd_micros"] == PRICE
    # the same job again: the kept picture is returned, nothing is reserved or submitted twice
    assert (await _run(job))["status"] == "reused"
    assert len(_calls("submit")) == 1 and await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]


async def test_a_submitted_option_resumes_its_provider_job_instead_of_paying_again(
        tmp_path, monkeypatch, provider, http):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    job = _request()[0]
    ledger, credits = BudgetLedger(db_path), CreditLedger(db_path)
    await frame_activity._ensure_budget_lines(db_path, EP)
    assert await ledger.reserve(EP, concept_frames.LINE, PRICE, "earlier attempt", job_id=job["job_id"])
    await credits.reserve_credits(EP, job["job_id"], 0.5)
    await credits.persist_provider_job(job["job_id"], "prov-frame-earlier")
    result = await _run(job)
    assert result["status"] == "completed" and result["provider_job_id"] == "prov-frame-earlier"
    assert _calls("submit") == [] and _calls("upload") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)


async def test_a_provider_failure_releases_both_holds(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    job = _request()[0]
    with pytest.raises(RuntimeError, match="failed"):
        await _run(job)
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert _status(job["job_id"]) == "failed"


async def test_poll_exhaustion_keeps_the_holds_and_the_reconciler_settles(tmp_path, monkeypatch, provider, http):
    from hfvg.activities.reconcile import job_type_of
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    monkeypatch.setattr(frame_activity, "POLL_LIMIT", 2)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _request()[0]
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "FramePending", err
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve"]
    assert (await _line(db_path))["reserved"] == PRICE
    assert _status(job["job_id"]) == "pending_reconcile"
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "pending_reconcile"
    assert job_type_of(job["job_id"]) == "concept_frame"
    # the reconciler (as ReconcilePendingJobsWorkflow calls it): still running -> left pending
    pending = {"job_id": job["job_id"], "episode_id": EP, "shot_id": "A01", "model": job["model_path"],
               "line_name": "L2_drafts", "usd_micros": PRICE, "job_type": "concept_frame", "phase": "poll"}
    env = ActivityEnvironment()
    assert await env.run(frame_activity.reconcile_concept_frame_job_activity, pending) == "still_pending"
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    FakeProvider.instances = []
    assert await env.run(frame_activity.reconcile_concept_frame_job_activity, pending) == "committed"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)
    assert concept_frames.option(job["job_id"])["sha256"]
    assert _calls("submit") == []


async def test_the_reconciler_releases_a_failed_option(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    monkeypatch.setattr(frame_activity, "POLL_LIMIT", 1)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _request()[0]
    assert (await _attempt(job)).type == "FramePending"
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    pending = {"job_id": job["job_id"], "episode_id": EP, "model": job["model_path"], "line_name": "L2_drafts",
               "usd_micros": PRICE, "job_type": "concept_frame"}
    assert await ActivityEnvironment().run(frame_activity.reconcile_concept_frame_job_activity, pending) == "released"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    assert (await _line(db_path))["reserved"] == 0
    assert _status(job["job_id"]) == "failed"


async def test_a_dry_run_reconciler_never_touches_money():
    pending = {"job_id": "cframe-0123456789ab-0", "episode_id": EP, "line_name": "L2_drafts", "usd_micros": 1}
    assert await ActivityEnvironment().run(frame_activity.reconcile_concept_frame_job_activity,
                                           pending) == "still_pending"


# ------------------------------------------------------------------------------------- workflows

async def _frames_workflow(env, jobs: list[dict], queue: str = "frames-test") -> dict:
    from hfvg.workflows.concept_frame import (
        ConceptFrameWorkflow,
        concept_frame_workflow_id,
        frames_task_queue,
    )
    set_id = jobs[0]["set_id"]
    return await asyncio.wait_for(env.client.execute_workflow(
        ConceptFrameWorkflow.run, {"set_id": set_id, "episode_id": EP, "shot_id": jobs[0]["shot_id"], "jobs": jobs},
        id=concept_frame_workflow_id(set_id), task_queue=frames_task_queue(queue)), timeout=40)


async def test_the_frames_workflow_makes_every_option_of_a_round(provider):
    """Dry run, on the production workers (the first frames workflow polls its own task queue)."""
    from contextlib import AsyncExitStack

    from hfvg.worker import production_workers
    _episode()
    jobs = _request()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            for worker in production_workers(env.client, "frames-test"):
                await stack.enter_async_context(worker)
            done = await _frames_workflow(env, jobs)
    assert (done["status"], done["made"], done["error"], done["reason"]) == ("completed", 3, None, None), done
    made = concept_frames.get_set(jobs[0]["set_id"])
    assert (made["status"], made["made"]) == ("ready", 3)
    assert [o["status"] for o in made["options"]] == ["completed"] * 3
    assert concept_frames.making_sets(EP) == [] and FakeProvider.instances == []


async def test_the_live_frames_workflow_stops_at_the_first_refusal_and_keeps_what_it_paid_for(
        tmp_path, monkeypatch, provider, http):
    from temporalio.worker import Worker

    from hfvg.worker import FRAMES_ACTIVITIES, FRAMES_WORKFLOWS
    from hfvg.workflows.concept_frame import frames_task_queue
    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    jobs = _request()
    jobs[1]["approved_usd_micros"] = PRICE - 1  # the second option's price is above its approved share
    price_went_up = frame_activity.OWNER_REASONS["EstimateAboveApproved"]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=frames_task_queue("frames-live"), workflows=FRAMES_WORKFLOWS,
                          activities=FRAMES_ACTIVITIES):
            done = await _frames_workflow(env, jobs, "frames-live")
            assert done["status"] == "partial" and done["reason"] == price_went_up, done
            assert [r["status"] for r in done["results"]] == ["completed", "failed"]
            assert [_status(j["job_id"]) for j in jobs] == ["completed", "failed", "skipped"]
            assert len(_calls("submit")) == 1
            assert [await _credit_txns(db_path, j["job_id"]) for j in jobs] == [["reserve", "commit"], [], []]
            assert ((await _line(db_path))["spent"], (await _line(db_path))["reserved"]) == (PRICE, 0)
            made = concept_frames.get_set(jobs[0]["set_id"])
            assert (made["status"], made["made"], made["error"]) == ("partial", 1, price_went_up)
            # a round whose first option is refused makes nothing: it fails with the plain reason
            again = _request()
            again[0]["approved_usd_micros"] = PRICE - 1
            stopped = await _frames_workflow(env, again, "frames-live")
    assert (stopped["status"], stopped["made"], stopped["reason"]) == ("failed", 0, price_went_up), stopped
    assert concept_frames.get_set(again[0]["set_id"])["status"] == "failed"
    assert len(_calls("submit")) == 1 and (await _line(db_path))["spent"] == PRICE


def test_the_owner_reads_a_plain_reason_for_every_stop():
    reasons = frame_activity.OWNER_REASONS
    assert frame_activity.owner_reason("BudgetReserveRefused") == reasons["BudgetReserveRefused"]
    for unknown in (None, "RuntimeError", 3):
        assert frame_activity.owner_reason(unknown) == frame_activity.PROVIDER_STOPPED
    for text in [*reasons.values(), frame_activity.PROVIDER_STOPPED]:
        assert "usd_micros" not in text and "cframe-" not in text and text.endswith(".")
        for word in ("grok", "soul", "higgsfield", "model"):
            assert word not in text.lower()


async def test_the_reconcile_workflow_settles_first_frame_options(tmp_path, monkeypatch):
    """ReconcilePendingJobsWorkflow routes cframe-... jobs to their own activity (real provider
    client against mocked HTTP)."""
    from temporalio.worker import Worker

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow

    db_path = await _live_db(tmp_path, monkeypatch)
    _episode()
    job = _request()[0]
    ledger, credits = BudgetLedger(db_path), CreditLedger(db_path)
    await frame_activity._ensure_budget_lines(db_path, EP)
    assert await ledger.reserve(EP, concept_frames.LINE, PRICE, "first frame", job_id=job["job_id"])
    await ledger.record_job(EP, job["job_id"], "A01", job["model_path"], "draft", concept_frames.LINE, PRICE,
                            user_id=OWNER_ID)
    await credits.reserve_credits(EP, job["job_id"], 0.5)
    await credits.persist_provider_job(job["job_id"], "prov-frame-9")
    await ledger.mark_job_status(job["job_id"], "pending_reconcile")
    concept_frames.set_option(job["job_id"], "pending_reconcile")
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue="reconcile-frames", workflows=[ReconcilePendingJobsWorkflow],
                          activities=PRODUCTION_ACTIVITIES):
            with respx.mock(assert_all_called=False) as router:
                router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
                router.get(f"{BASE}/requests/prov-frame-9/status").mock(return_value=httpx.Response(
                    200, json={"status": "completed", "images": [{"url": OUTPUT}]}))
                router.get(OUTPUT).mock(return_value=httpx.Response(200, content=image_bytes(size=(576, 1024))))
                summary = await asyncio.wait_for(env.client.execute_workflow(
                    ReconcilePendingJobsWorkflow.run, args=[100, None], id="reconcile-frames-1",
                    task_queue="reconcile-frames"), timeout=50)
    assert (summary["total"], summary["committed"], summary["errors"]) == (1, 1, []), summary
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "committed"
    assert _status(job["job_id"]) == "completed" and concept_frames.option(job["job_id"])["sha256"]


async def test_the_api_sees_a_worker_only_while_one_polls_the_first_frames_queue():
    from temporalio.worker import Worker

    from hfvg.worker import FRAMES_ACTIVITIES, FRAMES_WORKFLOWS
    from hfvg.workflows.concept_frame import frames_task_queue, worker_polls
    async with await WorkflowEnvironment.start_local() as env:
        assert await worker_polls(env.client, "frames-polls") is False
        async with Worker(env.client, task_queue=frames_task_queue("frames-polls"), workflows=FRAMES_WORKFLOWS,
                          activities=FRAMES_ACTIVITIES):
            seen = False
            for _ in range(50):
                seen = await worker_polls(env.client, "frames-polls")
                if seen:
                    break
                await asyncio.sleep(0.1)
            assert seen
        # a worker on the main queue alone (an older worker) is not one
        async with Worker(env.client, task_queue="frames-old", workflows=FRAMES_WORKFLOWS,
                          activities=FRAMES_ACTIVITIES):
            await asyncio.sleep(0.5)
            assert await worker_polls(env.client, "frames-old") is False


def test_the_first_frames_worker_runs_every_activity_its_workflow_calls():
    from hfvg.activities.reconcile import job_type_of
    from hfvg.worker import FRAMES_ACTIVITIES, FRAMES_WORKFLOWS, PRODUCTION_ACTIVITIES
    from hfvg.workflows.concept_frame import ConceptFrameWorkflow, frames_task_queue
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow
    from tests.test_worker_registration import (
        get_activity_names_from_list,
        get_workflow_activity_calls,
    )
    assert FRAMES_WORKFLOWS == [ConceptFrameWorkflow]
    assert get_workflow_activity_calls(ConceptFrameWorkflow) <= get_activity_names_from_list(FRAMES_ACTIVITIES)
    assert get_workflow_activity_calls(ReconcilePendingJobsWorkflow) <= get_activity_names_from_list(
        PRODUCTION_ACTIVITIES)
    assert "reconcile_concept_frame_job_activity" in get_activity_names_from_list(PRODUCTION_ACTIVITIES)
    assert frames_task_queue("hfvg-tasks") == "hfvg-tasks-frames"
    assert job_type_of("cframe-0123456789ab-2") == "concept_frame"
