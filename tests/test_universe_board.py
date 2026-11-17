"""Live mood boards (hfvg/universe_boards.py, hfvg/activities/universe_board.py,
hfvg/workflows/universe_board.py, docs/WORLDS.md): every paid picture goes through the guarded still
path on the Universe's own budget line (provider estimate within the approved share, balance gate,
credit and USD reserves, a per-job ledger row in the Universe owner's name, commit or release,
reconciler); dry runs make placeholders without spend; the workflow turns the pictures into the next
board version (a redo answers its feedback) on its own task queue. The provider is faked: no credits
are spent."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment

from hfvg import cast_store, universe_boards, world_store
from hfvg.activities import universe_board as board_activity
from hfvg.providers.base import ProviderJob, ProviderJobStatus
from tests.cast_helpers import attestation, image_bytes

OWNER_ID = "usr_5e6f7a8b9c0d"
OUTPUT = "https://cdn.test/board-out.png"
BASE = "https://api.higgsfield.ai"
PRICE = 390_000
NOTE = "Salt-crusted lanterns that hum at low tide"
MONEY_TABLES = ("budget_lines", "budget_transactions", "job_ledger", "credit_lines", "credit_transactions",
                "provider_jobs")


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    for var in ("MODEL_PATH_STILL", "CAST_UPLOAD_MAX_BYTES", "CAST_MIN_IMAGE_PX"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(board_activity, "POLL_INTERVAL_S", 0)


async def _live_db(tmp_path, monkeypatch, *, balance: str | None = "100.00") -> str:
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import init_studio_db
    from tests.ledger_helpers import set_balance

    db_path = str(tmp_path / "board.db")
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    if balance is not None:
        await set_balance(db_path, usd=balance, episode_id="ep-unrelated")
    for key, value in {"DATABASE_PATH": db_path, "DRY_RUN": "false",
                       "HIGGSFIELD_API_KEY": "test_id:test_secret", "HIGGSFIELD_BASE_URL": BASE}.items():
        monkeypatch.setenv(key, value)
    return db_path


class FakeProvider:
    """HiggsfieldStillProvider stand-in: records every call; statuses are returned in order."""

    instances: list["FakeProvider"] = []
    estimate = (PRICE, 6.5)
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
        return f"prov-board-{FakeProvider.submitted}"

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
    FakeProvider.estimate = (PRICE, 6.5)
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    FakeProvider.submitted = 0
    monkeypatch.setattr(higgsfield_still, "HiggsfieldStillProvider", FakeProvider)
    return FakeProvider


@pytest.fixture
def http():
    """Provider HTTP besides the faked provider: the balance monitor's usage lookup and downloads."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
        router.get(OUTPUT).mock(return_value=httpx.Response(200, content=image_bytes(size=(1024, 1024), seed=77)))
        yield router


def _universe(photos: int = 1) -> str:
    sid = world_store.create_universe("Lantern Bay", OWNER_ID, actor=OWNER_ID)["series"]["series_id"]
    world_store.add_note(sid, "premise", NOTE, OWNER_ID)
    for k in range(photos):
        world_store.add_reference_photo(sid, image_bytes(seed=20 + k), filename=f"ref{k}.png",
                                        attestation=attestation(), caption=f"foggy pier {k}", actor=OWNER_ID)
    return sid


def _request(sid: str, *, approved: int = 400_000, feedback: str = "") -> list[dict]:
    """A live board request as api/world_routes records it once the owner approved the estimate:
    its picture jobs, each with its share of the approved total."""
    plan = world_store.plan_board(sid, feedback)
    request_id = cast_store._new_id("mr")
    jobs = [{**job, "estimated_usd_micros": PRICE, "approved_usd_micros": approved}
            for job in universe_boards.plan_jobs(plan, request_id=request_id, series_id=sid,
                                                 owner_user_id=OWNER_ID)]
    world_store.start_board_request(sid, plan, jobs, request_id=request_id, workflow_id=f"universe-board-{request_id}",
                                    estimate_usd_micros=PRICE * len(jobs),
                                    approved_total_usd_micros=approved * len(jobs),
                                    model_path=jobs[0]["model_path"], requested_by=OWNER_ID)
    return jobs


async def _run(job: dict, beats: list | None = None) -> dict:
    env = ActivityEnvironment()
    if beats is not None:
        env.on_heartbeat = lambda *details: beats.append(details)
    return await env.run(board_activity.generate_universe_board_image_activity, job)


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
    return world_store.board_job(job_id)["status"]


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


async def _line(db_path: str, sid: str) -> dict:
    from hfvg.budget import BudgetLedger
    return await BudgetLedger(db_path).get_line_status(universe_boards.ledger_key(sid), universe_boards.LINE)


def _money_rows(db_path: str) -> dict:
    import sqlite3
    with sqlite3.connect(db_path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in MONEY_TABLES if t in names}


# ------------------------------------------------------------------------------------- planning


def test_a_live_board_is_one_still_call_per_picture_on_the_universe_line_with_its_reference_photos():
    from hfvg.config import max_refs_for
    sid = _universe(photos=7)
    plan = world_store.plan_board(sid)
    jobs = universe_boards.plan_jobs(plan, request_id="mr_0123456789ab", series_id=sid, owner_user_id=OWNER_ID)
    assert [j["job_id"] for j in jobs] == [f"uboard-0123456789ab-{k}" for k in range(world_store.BOARD_IMAGES)]
    assert {j["ledger_key"] for j in jobs} == {f"universe-{sid}"} and universe_boards.LINE == "U1_mood_board"
    assert {j["owner_user_id"] for j in jobs} == {OWNER_ID}
    per_picture = min(max_refs_for(jobs[0]["model_path"]), 7)
    assert per_picture > 0 and all(len(j["refs"]) == per_picture for j in jobs)
    # every reference photo reaches some picture, unchanged on disk
    assert {s for j in jobs for s in j["ref_shas"]} == {p["sha256"] for p in world_store.list_reference_photos(sid)}
    for job in jobs:
        for path, sha in zip(job["refs"], job["ref_shas"]):
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == sha
        assert job["prompt"].startswith(f"Picture {job['position'] + 1} of {len(jobs)}") and NOTE in job["prompt"]
        assert len(job["prompt"]) <= universe_boards.PICTURE_PROMPT_MAX
    assert len({j["prompt"] for j in jobs}) == len(jobs)


def test_a_model_the_server_cannot_route_is_refused_before_any_job(monkeypatch):
    sid = _universe()
    plan = world_store.plan_board(sid)
    monkeypatch.setenv("MODEL_PATH_STILL", "no-such/model-for-boards")
    with pytest.raises(universe_boards.BoardPlanError, match="not in the model caps registry") as err:
        universe_boards.plan_jobs(plan, request_id="mr_0123456789ab", series_id=sid, owner_user_id=OWNER_ID)
    assert err.value.status == 409


# ------------------------------------------------------------------------------------- dry run, refusals


async def test_dry_run_makes_a_placeholder_without_spend(provider):
    sid = _universe()
    job = _request(sid)[0]
    first = await _run(job)
    assert first == {"job_id": job["job_id"], "status": "completed", "usd_micros": 0, "placeholder": True}
    row = world_store.board_job(job["job_id"])
    assert row["status"] == "completed" and row["placeholder"] == 1 and row["sha256"]
    assert cast_store.image_file(row["sha256"], row["ext"]).is_file()
    assert FakeProvider.instances == []
    again = await _run(job)  # idempotent: a kept picture is returned, nothing is made twice
    assert again == {"job_id": job["job_id"], "status": "reused", "usd_micros": 0}


async def test_a_job_that_is_not_recorded_or_whose_request_ended_makes_nothing(provider):
    sid = _universe()
    jobs = _request(sid)
    stranger = {**jobs[0], "job_id": "uboard-ffffffffffff-0"}
    with pytest.raises(ApplicationError, match="not recorded for this board") as err:
        await _run(stranger)
    assert err.value.non_retryable
    world_store.finish_board_request(jobs[0]["request_id"], "stopped by a test")
    with pytest.raises(ApplicationError, match="ended; nothing is generated") as err:
        await _run(jobs[1])
    assert err.value.non_retryable and _status(jobs[1]["job_id"]) == "refused"
    assert FakeProvider.instances == []


@pytest.mark.parametrize("approved", [0, None, True, -5])
async def test_a_job_without_an_approved_price_spends_nothing(provider, approved):
    sid = _universe()
    job = {**_request(sid)[0], "approved_usd_micros": approved}
    with pytest.raises(ApplicationError, match="approved estimate") as err:
        await _run(job)
    assert err.value.non_retryable and FakeProvider.instances == [] and _status(job["job_id"]) == "refused"


async def test_a_job_booked_anywhere_but_its_universe_line_is_refused(provider):
    sid = _universe()
    job = {**_request(sid)[0], "ledger_key": "ep71sheet"}
    with pytest.raises(ApplicationError, match="mood board budget") as err:
        await _run(job)
    assert err.value.non_retryable and FakeProvider.instances == []


async def test_a_reference_photo_changed_since_the_plan_is_refused(provider):
    sid = _universe()
    job = _request(sid)[0]
    Path(job["refs"][0]).write_bytes(image_bytes(seed=99))
    with pytest.raises(ApplicationError, match="changed since the board was planned"):
        await _run(job)
    assert _status(job["job_id"]) == "refused"


# ------------------------------------------------------------------------------------- live: the guards


async def test_live_estimate_above_the_approved_share_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    FakeProvider.estimate = (500_000, 8.0)
    job = _request(sid, approved=400_000)[0]
    err = await _attempt(job)
    assert _calls("estimate") and _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == []
    line = await _line(db_path, sid)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert await _ledger_row(db_path, job["job_id"]) is None
    assert isinstance(err, ApplicationError) and err.non_retryable and err.type == "EstimateAboveApproved", err
    assert _status(job["job_id"]) == "failed"


async def test_live_estimate_failure_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()

    async def no_price(self, **params):
        raise ValueError("Failed to get USD/credits estimate for still generation: no usd")
    monkeypatch.setattr(FakeProvider, "estimate_usd_and_credits", no_price)
    job = _request(sid)[0]
    with pytest.raises(ValueError, match="estimate"):
        await _run(job)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    assert _status(job["job_id"]) == "failed"


async def test_live_balance_gate_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch, balance="0.10")
    sid = _universe()
    job = _request(sid)[0]
    err = await _attempt(job)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    line = await _line(db_path, sid)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert isinstance(err, ApplicationError) and err.non_retryable and err.type == "InsufficientBalance", err
    assert "balance insufficient" in str(err)


async def test_live_without_a_manual_balance_the_usd_reserve_refuses(tmp_path, monkeypatch, provider, http):
    """GC.01: the USD reserve needs a recorded balance; the credit hold taken before it is released."""
    db_path = await _live_db(tmp_path, monkeypatch, balance=None)
    sid = _universe()
    job = _request(sid)[0]
    with pytest.raises(ValueError, match="GC.01: no manual Higgsfield balance recorded"):
        await _run(job)
    assert _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path, sid)
    assert (line["reserved"], line["spent"]) == (0, 0)


async def test_live_the_universe_line_stop_refuses_the_reserve(tmp_path, monkeypatch, provider, http):
    from hfvg.budget import BudgetLedger
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    key = universe_boards.ledger_key(sid)
    ledger = BudgetLedger(db_path)
    assert await ledger.ensure_line(key, universe_boards.LINE, universe_boards.LINE_CAP_USD_MICROS) is True
    line = await _line(db_path, sid)
    assert (line["budget_cap"], line["stop_threshold"]) == (4_000_000, 3_200_000)
    assert await ledger.reserve(key, universe_boards.LINE, 3_000_000, "an earlier board", job_id="uboard-earlier-0")
    job = _request(sid)[0]
    err = await _attempt(job)
    assert _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    assert (await _line(db_path, sid))["reserved"] == 3_000_000
    assert isinstance(err, ApplicationError) and err.type == "BudgetReserveRefused", err
    # the line is never reset or raised by the next picture
    assert await ledger.ensure_line(key, universe_boards.LINE, 9_000_000) is False
    assert (await _line(db_path, sid))["budget_cap"] == 4_000_000


async def test_live_success_reserves_submits_commits_records_the_ledger_and_keeps_the_picture(
        tmp_path, monkeypatch, provider, http):
    from hfvg.cost_report import build_cost_truth
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe(photos=2)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING, ProviderJobStatus.COMPLETED]
    job = _request(sid)[0]
    beats: list = []
    result = await _run(job, beats)
    assert result == {"job_id": job["job_id"], "status": "completed", "provider_job_id": "prov-board-1",
                      "usd_micros": PRICE}
    kinds = [c[0] for p in FakeProvider.instances for c in p.calls]
    assert kinds == ["upload", "upload", "estimate", "submit", "poll", "poll"]
    submit = _calls("submit")[0][1]
    assert submit["idempotency_key"].startswith(job["job_id"] + "-")
    assert submit["image_urls"] == [f"https://cdn.test/ref-{Path(p).name}" for p in job["refs"]]
    assert (submit["prompt"], submit["aspect_ratio"]) == (job["prompt"], "1:1")
    assert beats, "the activity heartbeats while it polls the provider"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path, sid)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)
    row = await _ledger_row(db_path, job["job_id"])
    assert (row["episode_id"], row["user_id"], row["line_name"], row["tier"], row["status"], row["usd_micros"]) == (
        f"universe-{sid}", OWNER_ID, "U1_mood_board", "final", "committed", PRICE)
    picture = world_store.board_job(job["job_id"])
    assert picture["status"] == "completed" and picture["placeholder"] == 0
    assert cast_store.image_file(picture["sha256"], picture["ext"]).is_file()
    # cost truth: the mood board stage, in the Universe owner's name
    truth = await build_cost_truth(db_path)
    stages = {s["stage"]: s for s in truth["by_stage"]}
    assert stages["mood_boards"]["spend_usd_micros"] == PRICE
    person = next(p for p in truth["by_person"] if p["user_id"] == OWNER_ID)
    assert person["spend_usd_micros"] == PRICE and person["attribution"] == ["job_owner"]
    # the same job again: the kept picture is returned, nothing is reserved or submitted twice
    assert (await _run(job))["status"] == "reused"
    assert len(_calls("submit")) == 1 and await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]


async def test_a_submitted_picture_resumes_its_provider_job_instead_of_paying_again(
        tmp_path, monkeypatch, provider, http):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    job = _request(sid)[0]
    key = universe_boards.ledger_key(sid)
    ledger, credits = BudgetLedger(db_path), CreditLedger(db_path)
    await board_activity._ensure_budget_lines(db_path, key)
    assert await ledger.reserve(key, universe_boards.LINE, PRICE, "earlier attempt", job_id=job["job_id"])
    await credits.reserve_credits(key, job["job_id"], 6.5)
    await credits.persist_provider_job(job["job_id"], "prov-board-earlier")
    result = await _run(job)
    assert result["status"] == "completed" and result["provider_job_id"] == "prov-board-earlier"
    assert _calls("submit") == [] and _calls("upload") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path, sid)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)


async def test_a_provider_failure_releases_both_holds(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    job = _request(sid)[0]
    with pytest.raises(RuntimeError, match="failed"):
        await _run(job)
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path, sid)
    assert (line["reserved"], line["spent"]) == (0, 0)
    assert _status(job["job_id"]) == "failed"


async def test_poll_exhaustion_keeps_the_holds_and_the_reconciler_settles(tmp_path, monkeypatch, provider, http):
    from hfvg.activities.reconcile import job_type_of
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    monkeypatch.setattr(board_activity, "POLL_LIMIT", 2)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _request(sid)[0]
    err = await _attempt(job)
    assert isinstance(err, ApplicationError) and err.type == "MoodBoardPending", err
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve"]
    assert (await _line(db_path, sid))["reserved"] == PRICE
    assert _status(job["job_id"]) == "pending_reconcile"
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "pending_reconcile"
    assert job_type_of(job["job_id"]) == "universe_board"
    # the reconciler (as ReconcilePendingJobsWorkflow calls it): still running -> left pending
    pending = {"job_id": job["job_id"], "episode_id": f"universe-{sid}", "shot_id": None, "model": job["model_path"],
               "line_name": "U1_mood_board", "usd_micros": PRICE, "job_type": "universe_board", "phase": "poll"}
    env = ActivityEnvironment()
    assert await env.run(board_activity.reconcile_universe_board_job_activity, pending) == "still_pending"
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    FakeProvider.instances = []
    assert await env.run(board_activity.reconcile_universe_board_job_activity, pending) == "committed"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path, sid)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)
    assert _status(job["job_id"]) == "completed" and world_store.board_job(job["job_id"])["sha256"]
    assert _calls("submit") == []


async def test_the_reconciler_releases_a_failed_board_picture(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    monkeypatch.setattr(board_activity, "POLL_LIMIT", 1)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _request(sid)[0]
    assert (await _attempt(job)).type == "MoodBoardPending"
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    pending = {"job_id": job["job_id"], "episode_id": f"universe-{sid}", "model": job["model_path"],
               "line_name": "U1_mood_board", "usd_micros": PRICE, "job_type": "universe_board"}
    assert await ActivityEnvironment().run(board_activity.reconcile_universe_board_job_activity, pending) == "released"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    assert (await _line(db_path, sid))["reserved"] == 0
    assert _status(job["job_id"]) == "failed"


# ------------------------------------------------------------------------------------- workflows


async def _board_workflow(env, jobs: list[dict], queue: str = "uboard-test") -> dict:
    from hfvg.workflows.universe_board import UniverseBoardWorkflow, universe_task_queue
    request_id = jobs[0]["request_id"]
    return await asyncio.wait_for(env.client.execute_workflow(
        UniverseBoardWorkflow.run, {"request_id": request_id, "series_id": jobs[0]["series_id"], "jobs": jobs},
        id=f"universe-board-{request_id}", task_queue=universe_task_queue(queue)), timeout=40)


async def test_the_board_workflow_makes_the_board_and_a_redo_answers_its_feedback(provider):
    """Dry run, on the production workers (the mood board workflow polls its own task queue)."""
    from contextlib import AsyncExitStack

    from hfvg.worker import production_workers
    sid = _universe()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with AsyncExitStack() as stack:
            for worker in production_workers(env.client, "uboard-test"):
                await stack.enter_async_context(worker)
            first = await _board_workflow(env, _request(sid))
            assert first["status"] == "completed" and first["error"] is None, first
            assert [r["status"] for r in first["results"]] == ["completed"] * world_store.BOARD_IMAGES
            boards = world_store.list_boards(sid, with_prompt=True)
            assert [(b["version"], b["status"], b["dry_run"]) for b in boards] == [(1, "ready", True)]
            assert first["board_id"] == boards[0]["board_id"] and len(boards[0]["images"]) == world_store.BOARD_IMAGES
            redo = await _board_workflow(env, _request(sid, feedback="warmer light, fewer boats"))
    assert redo["status"] == "completed", redo
    boards = world_store.list_boards(sid, with_prompt=True)
    assert [(b["version"], b["status"]) for b in boards] == [(1, "redo_requested"), (2, "ready")]
    assert boards[0]["redo_feedback"] == "warmer light, fewer boats"
    assert boards[1]["answers_feedback"] == "warmer light, fewer boats"
    assert "Change from the last board: warmer light, fewer boats" in boards[1]["prompt"]
    assert world_store.open_board_request(sid) is None and FakeProvider.instances == []


async def test_the_live_board_workflow_stops_at_the_first_refusal_and_spends_nothing_more(
        tmp_path, monkeypatch, provider, http):
    from temporalio.worker import Worker

    from hfvg.worker import UNIVERSE_ACTIVITIES, UNIVERSE_WORKFLOWS
    from hfvg.workflows.universe_board import universe_task_queue
    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    jobs = _request(sid)
    jobs[1]["approved_usd_micros"] = PRICE - 1  # the second picture's price is above its approved share
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=universe_task_queue("uboard-live"),
                          workflows=UNIVERSE_WORKFLOWS, activities=UNIVERSE_ACTIVITIES):
            done = await _board_workflow(env, jobs, "uboard-live")
    assert done["status"] == "failed" and "price went up" in done["error"], done
    assert [r["status"] for r in done["results"]] == ["completed", "failed"]
    assert [_status(j["job_id"]) for j in jobs] == ["completed", "failed", "skipped", "skipped"]
    assert len(_calls("submit")) == 1
    assert [await _credit_txns(db_path, j["job_id"]) for j in jobs] == [["reserve", "commit"], [], [], []]
    line = await _line(db_path, sid)
    assert (line["spent"], line["reserved"]) == (PRICE, 0)
    request = world_store.open_board_request(sid)
    assert request["status"] == "failed" and request["made"] == 1 and "price went up" in request["error"]
    assert world_store.list_boards(sid) == []


async def test_the_reconcile_workflow_settles_board_pictures(tmp_path, monkeypatch):
    """ReconcilePendingJobsWorkflow routes uboard-... jobs to their own activity (real provider
    client against mocked HTTP)."""
    from temporalio.worker import Worker

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow

    db_path = await _live_db(tmp_path, monkeypatch)
    sid = _universe()
    job = _request(sid)[0]
    key = universe_boards.ledger_key(sid)
    ledger, credits = BudgetLedger(db_path), CreditLedger(db_path)
    await board_activity._ensure_budget_lines(db_path, key)
    assert await ledger.reserve(key, universe_boards.LINE, PRICE, "board", job_id=job["job_id"])
    await ledger.record_job(key, job["job_id"], None, job["model_path"], "final", universe_boards.LINE, PRICE,
                            user_id=OWNER_ID)
    await credits.reserve_credits(key, job["job_id"], 6.5)
    await credits.persist_provider_job(job["job_id"], "prov-board-9")
    await ledger.mark_job_status(job["job_id"], "pending_reconcile")
    world_store.set_board_job(job["job_id"], "pending_reconcile")
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue="reconcile-board", workflows=[ReconcilePendingJobsWorkflow],
                          activities=PRODUCTION_ACTIVITIES):
            with respx.mock(assert_all_called=False) as router:
                router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"time_series": [], "total": 0}))
                router.get(f"{BASE}/requests/prov-board-9/status").mock(return_value=httpx.Response(
                    200, json={"status": "completed", "images": [{"url": OUTPUT}]}))
                router.get(OUTPUT).mock(return_value=httpx.Response(200, content=image_bytes(size=(1024, 1024))))
                summary = await asyncio.wait_for(env.client.execute_workflow(
                    ReconcilePendingJobsWorkflow.run, args=[100, None], id="reconcile-board-1",
                    task_queue="reconcile-board"), timeout=50)
    assert (summary["total"], summary["committed"], summary["errors"]) == (1, 1, []), summary
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "committed"
    assert _status(job["job_id"]) == "completed" and world_store.board_job(job["job_id"])["sha256"]


async def test_the_api_sees_a_worker_only_while_one_polls_the_mood_board_queue():
    from temporalio.worker import Worker

    from hfvg.worker import UNIVERSE_ACTIVITIES, UNIVERSE_WORKFLOWS
    from hfvg.workflows.universe_board import universe_task_queue, worker_polls
    async with await WorkflowEnvironment.start_local() as env:
        assert await worker_polls(env.client, "uboard-polls") is False
        async with Worker(env.client, task_queue=universe_task_queue("uboard-polls"), workflows=UNIVERSE_WORKFLOWS,
                          activities=UNIVERSE_ACTIVITIES):
            seen = False
            for _ in range(50):
                seen = await worker_polls(env.client, "uboard-polls")
                if seen:
                    break
                await asyncio.sleep(0.1)
            assert seen
        # a worker on the main queue alone (an older worker) is not one
        async with Worker(env.client, task_queue="uboard-old", workflows=UNIVERSE_WORKFLOWS,
                          activities=UNIVERSE_ACTIVITIES):
            await asyncio.sleep(0.5)
            assert await worker_polls(env.client, "uboard-old") is False


def test_the_mood_board_worker_runs_every_activity_its_workflow_calls():
    from hfvg.activities.reconcile import job_type_of
    from hfvg.worker import PRODUCTION_ACTIVITIES, UNIVERSE_ACTIVITIES, UNIVERSE_WORKFLOWS
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow
    from hfvg.workflows.universe_board import UniverseBoardWorkflow, universe_task_queue
    from tests.test_worker_registration import (
        get_activity_names_from_list,
        get_workflow_activity_calls,
    )
    assert UNIVERSE_WORKFLOWS == [UniverseBoardWorkflow]
    assert get_workflow_activity_calls(UniverseBoardWorkflow) <= get_activity_names_from_list(UNIVERSE_ACTIVITIES)
    assert get_workflow_activity_calls(ReconcilePendingJobsWorkflow) <= get_activity_names_from_list(
        PRODUCTION_ACTIVITIES)
    assert "reconcile_universe_board_job_activity" in get_activity_names_from_list(PRODUCTION_ACTIVITIES)
    assert universe_task_queue("hfvg-tasks") == "hfvg-tasks-universe"
    assert job_type_of("uboard-0123456789ab-2") == "universe_board"
