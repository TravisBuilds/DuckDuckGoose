"""Character sheet generation (hfvg/activities/cast_sheet.py, hfvg/workflows/cast_sheet.py): every paid
sheet image goes through the guarded path (provider estimate within the approved price, balance gate,
credit and USD reserves, per-job ledger row with the episode owner, commit or release, reconciler);
dry runs make placeholders without spend; locked versions are never touched."""

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
from temporalio.worker import Worker

from hfvg import cast_store
from hfvg.activities import cast_sheet as sheet_activity
from hfvg.cast_sheets import plan_jobs
from hfvg.providers.base import ProviderJob, ProviderJobStatus
from tests.cast_helpers import complete_sheet, image_bytes, new_character, upload

EP = "ep71sheet"
OWNER_ID = "usr_0a1b2c3d4e5f"
OUTPUT = "https://cdn.test/sheet-out.png"
BASE = "https://api.higgsfield.ai"


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("MODEL_PATH_STILL", raising=False)
    monkeypatch.setattr(sheet_activity, "POLL_INTERVAL_S", 0)


async def _live_db(tmp_path, monkeypatch, *, balance: str | None = "100.00") -> str:
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import create_episode, init_studio_db, set_live_mode
    from tests.ledger_helpers import set_balance

    db_path = str(tmp_path / "sheet.db")
    await init_studio_db(db_path)
    await create_episode(db_path, EP, user_id=OWNER_ID)
    await BudgetLedger(db_path).init_db()
    await set_live_mode(db_path, EP, True)
    if balance is not None:
        await set_balance(db_path, usd=balance, episode_id=EP)
    for key, value in {"DATABASE_PATH": db_path, "DRY_RUN": "false", "SMOKE_TEST_OVERRIDE": "true",
                       "HIGGSFIELD_API_KEY": "test_id:test_secret", "HIGGSFIELD_BASE_URL": BASE}.items():
        monkeypatch.setenv(key, value)
    return db_path


class FakeProvider:
    """HiggsfieldStillProvider stand-in: records every call; statuses are returned in order."""

    instances: list["FakeProvider"] = []
    estimate = (390_000, 6.5)
    statuses: list[ProviderJobStatus] = [ProviderJobStatus.COMPLETED]

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
        self.calls.append(("submit", {"image_urls": image_urls, "idempotency_key": idempotency_key, **params}))
        return "prov-sheet-1"

    async def get_job_status(self, provider_job_id):
        status = self.statuses[min(self.polls, len(self.statuses) - 1)]
        self.polls += 1
        self.calls.append(("poll", provider_job_id))
        return ProviderJob(job_id=provider_job_id, status=status, error="boom" if status == "failed" else None,
                           output_url=OUTPUT if status == ProviderJobStatus.COMPLETED else None)

    async def close(self):
        pass


@pytest.fixture
def provider(monkeypatch):
    from hfvg.providers import higgsfield_still
    FakeProvider.instances = []
    FakeProvider.estimate = (390_000, 6.5)
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    monkeypatch.setattr(higgsfield_still, "HiggsfieldStillProvider", FakeProvider)
    return FakeProvider


@pytest.fixture
def http():
    """Provider HTTP besides the faked provider: the balance monitor's usage lookup and downloads."""
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"usage": []}))
        yield router


def _calls(kind: str) -> list:
    return [c for p in FakeProvider.instances for c in p.calls if c[0] == kind]


def _job(character: dict, stage="options", *, approved=400_000, live=True, **kw) -> dict:
    plan = plan_jobs(cast_store.get_character(character["character_id"]), 1, stage, episode_id=EP, live=live, **kw)
    job = {**plan["jobs"][0], "approved_usd_micros": approved}
    cast_store.record_sheet_job({**job, "workflow_id": "cast-sheet-test", "requested_by": "t",
                                 "estimated_usd_micros": approved})
    return job


async def _run(job: dict) -> dict:
    return await ActivityEnvironment().run(sheet_activity.generate_cast_sheet_image_activity, job)


async def _refused(job: dict, kind: str | None = None) -> ApplicationError:
    with pytest.raises(ApplicationError) as err:
        await _run(job)
    assert err.value.non_retryable
    if kind:
        assert err.value.type == kind, err.value
    return err.value


def _sheet_status(job_id: str) -> str:
    with cast_store.connect() as conn:
        return conn.execute("SELECT status FROM cast_sheet_jobs WHERE job_id = ?", (job_id,)).fetchone()[0]


async def _credit_txns(db_path: str, job_id: str) -> list[str]:
    from hfvg.credit_ledger import CreditLedger
    await CreditLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT txn_type FROM credit_transactions WHERE job_id = ? ORDER BY rowid",
                              (job_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]


async def _ledger_row(db_path: str, job_id: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            return dict(await cur.fetchone())


async def _line(db_path: str) -> dict:
    from hfvg.budget import BudgetLedger
    return await BudgetLedger(db_path).get_line_status(EP, "L1_refs")


def _respx_output(router) -> None:
    router.get(OUTPUT).mock(return_value=httpx.Response(200, content=image_bytes(size=(768, 1152), seed=77)))


# ------------------------------------------------------------------------------------- dry run, refusals


async def test_dry_run_makes_a_placeholder_without_spend(provider):
    pip = new_character(("episode", EP))
    job = _job(pip, live=False, source="text")
    first = await _run(job)
    assert first["status"] == "completed" and first["placeholder"] is True and first["usd_micros"] == 0
    image = cast_store.get_image(first["image_id"])
    assert image["placeholder"] is True and image["role"] == cast_store.OPTION_ROLE
    assert FakeProvider.instances == []
    again = await _run(job)  # idempotent: a stored image is reused, nothing is made twice
    assert again == {"job_id": job["job_id"], "status": "reused", "image_id": first["image_id"], "usd_micros": 0}
    assert _sheet_status(job["job_id"]) == "completed"


async def test_a_locked_version_is_never_generated(provider):
    pip = new_character(("episode", EP))
    job = _job(pip, live=False, source="text")
    complete_sheet(cast_store.get_character(pip["character_id"]))
    cast_store.approve_version(pip["character_id"], 1, "t")
    err = await _refused(job)
    assert "locked" in str(err) and _sheet_status(job["job_id"]) == "refused"


@pytest.mark.parametrize("approved", [0, None, True, -5])
async def test_a_job_without_an_approved_price_spends_nothing(provider, approved):
    pip = new_character(("episode", EP))
    job = _job(pip, live=False, source="text")
    job["approved_usd_micros"] = approved
    err = await _refused(job)
    assert "approved estimate" in str(err)
    assert FakeProvider.instances == [] and _sheet_status(job["job_id"]) == "refused"


async def test_a_changed_reference_is_refused(provider):
    pip = new_character(("episode", EP))
    up = upload(pip, seed=3)
    job = _job(pip, live=False, source="upload")
    cast_store.image_path(up).write_bytes(image_bytes(seed=99))
    err = await _refused(job)
    assert "changed since the job was planned" in str(err)


# ------------------------------------------------------------------------------------- live: the guards


async def test_live_needs_a_live_episode(tmp_path, monkeypatch, provider, http):
    from hfvg.studio_db import set_live_mode
    db_path = await _live_db(tmp_path, monkeypatch)
    await set_live_mode(db_path, EP, False)
    job = _job(new_character(("episode", EP)), source="text")
    await _refused(job)
    assert FakeProvider.instances == [] and await _credit_txns(db_path, job["job_id"]) == []


async def test_live_estimate_above_the_approval_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    FakeProvider.estimate = (500_000, 8.0)
    job = _job(new_character(("episode", EP)), source="text", approved=400_000)
    before = await _line(db_path)
    await _refused(job, "EstimateAboveApproved")
    assert _calls("estimate") and _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == []
    assert await _line(db_path) == before
    assert _sheet_status(job["job_id"]) == "failed"


async def test_live_estimate_failure_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)

    async def no_price(self, **params):
        raise ValueError("Failed to get USD/credits estimate for still generation: no usd")
    monkeypatch.setattr(FakeProvider, "estimate_usd_and_credits", no_price)
    job = _job(new_character(("episode", EP)), source="text")
    with pytest.raises(ValueError, match="estimate"):
        await _run(job)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    assert _sheet_status(job["job_id"]) == "failed"


async def test_live_balance_gate_spends_nothing(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch, balance="0.10")
    job = _job(new_character(("episode", EP)), source="text")
    before = await _line(db_path)
    err = await _refused(job, "InsufficientBalance")
    assert "balance insufficient" in str(err)
    assert _calls("submit") == [] and await _credit_txns(db_path, job["job_id"]) == []
    assert await _line(db_path) == before


async def test_live_without_a_manual_balance_the_usd_reserve_refuses(tmp_path, monkeypatch, provider, http):
    """GC.01: the USD reserve needs a recorded balance; the credit hold taken before it is released."""
    db_path = await _live_db(tmp_path, monkeypatch, balance=None)
    job = _job(new_character(("episode", EP)), source="text")
    with pytest.raises(ValueError, match="GC.01: no manual Higgsfield balance recorded"):
        await _run(job)
    assert _calls("submit") == []
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path)
    assert line["reserved"] == 0 and line["spent"] == 0


async def test_live_success_reserves_submits_commits_and_stores(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    _respx_output(http)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING, ProviderJobStatus.COMPLETED]
    pip = new_character(("episode", EP))
    up = upload(pip, seed=5)
    job = _job(pip, source="upload")
    before = await _line(db_path)
    result = await _run(job)
    assert result["status"] == "completed" and result["usd_micros"] == 390_000
    kinds = [c[0] for p in FakeProvider.instances for c in p.calls]
    assert kinds[:3] == ["upload", "estimate", "submit"] and kinds.count("poll") == 2
    submit = _calls("submit")[0][1]
    assert submit["idempotency_key"].startswith(job["job_id"] + "-")
    assert submit["image_urls"] == [f"https://cdn.test/ref-{Path(job['refs'][0]).name}"]
    assert Path(job["refs"][0]).read_bytes() == cast_store.image_path(up).read_bytes()
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path)
    assert line["spent"] - before["spent"] == 390_000 and line["reserved"] == before["reserved"]
    row = await _ledger_row(db_path, job["job_id"])
    assert (row["user_id"], row["line_name"], row["tier"], row["status"]) == (OWNER_ID, "L1_refs", "final", "committed")
    image = cast_store.get_image(result["image_id"])
    assert image["placeholder"] is False and image["job_id"] == job["job_id"]
    assert _sheet_status(job["job_id"]) == "completed"
    # the same job again: the stored image is returned, nothing is reserved or submitted twice
    assert (await _run(job))["status"] == "reused"
    assert len(_calls("submit")) == 1 and await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]


async def test_a_provider_failure_releases_both_holds(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    job = _job(new_character(("episode", EP)), source="text")
    before = await _line(db_path)
    with pytest.raises(RuntimeError, match="failed"):
        await _run(job)
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    line = await _line(db_path)
    assert (line["reserved"], line["spent"]) == (before["reserved"], before["spent"])
    assert _sheet_status(job["job_id"]) == "failed"


async def test_poll_exhaustion_keeps_the_holds_and_the_reconciler_settles(tmp_path, monkeypatch, provider, http):
    from hfvg.activities.reconcile import job_type_of
    db_path = await _live_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sheet_activity, "POLL_LIMIT", 2)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _job(new_character(("episode", EP)), source="text")
    before = await _line(db_path)
    await _refused(job, "CastSheetPending")
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve"]
    assert (await _line(db_path))["reserved"] - before["reserved"] == 390_000
    assert _sheet_status(job["job_id"]) == "pending_reconcile"
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "pending_reconcile"
    assert job_type_of(job["job_id"]) == "cast_sheet"
    # the reconciler (as ReconcilePendingJobsWorkflow calls it): still running -> left pending
    pending = {"job_id": job["job_id"], "episode_id": EP, "shot_id": None, "model": job["model_path"],
               "line_name": "L1_refs", "usd_micros": 390_000, "job_type": "cast_sheet", "phase": "poll"}
    env = ActivityEnvironment()
    assert await env.run(sheet_activity.reconcile_cast_sheet_job_activity, pending) == "still_pending"
    FakeProvider.statuses = [ProviderJobStatus.COMPLETED]
    FakeProvider.instances = []
    _respx_output(http)
    assert await env.run(sheet_activity.reconcile_cast_sheet_job_activity, pending) == "committed"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    line = await _line(db_path)
    assert line["spent"] - before["spent"] == 390_000 and line["reserved"] == before["reserved"]
    assert _sheet_status(job["job_id"]) == "completed" and _calls("submit") == []


async def test_the_reconciler_releases_a_failed_sheet_job(tmp_path, monkeypatch, provider, http):
    db_path = await _live_db(tmp_path, monkeypatch)
    monkeypatch.setattr(sheet_activity, "POLL_LIMIT", 1)
    FakeProvider.statuses = [ProviderJobStatus.PROCESSING]
    job = _job(new_character(("episode", EP)), source="text")
    before = await _line(db_path)
    await _refused(job, "CastSheetPending")
    FakeProvider.statuses = [ProviderJobStatus.FAILED]
    pending = {"job_id": job["job_id"], "episode_id": EP, "model": job["model_path"], "line_name": "L1_refs",
               "usd_micros": 390_000, "job_type": "cast_sheet"}
    assert await ActivityEnvironment().run(sheet_activity.reconcile_cast_sheet_job_activity, pending) == "released"
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "release"]
    assert (await _line(db_path))["reserved"] == before["reserved"]
    assert _sheet_status(job["job_id"]) == "failed"


# ------------------------------------------------------------------------------------- workflows


async def test_the_sheet_workflow_runs_the_jobs_and_stops_at_the_first_failure(tmp_path, monkeypatch):
    from hfvg.workflows.cast_sheet import CastSheetWorkflow, cast_sheet_workflow_id
    pip = new_character(("episode", EP))
    plan = plan_jobs(cast_store.get_character(pip["character_id"]), 1, "options", episode_id=EP, live=False,
                     source="text", count=3)
    jobs = [{**j, "approved_usd_micros": 60_000} for j in plan["jobs"]]
    for j in jobs:
        cast_store.record_sheet_job({**j, "workflow_id": "w", "requested_by": "t"})
    activities = [sheet_activity.generate_cast_sheet_image_activity, sheet_activity.finish_cast_sheet_jobs_activity]
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="cast-sheet-test", workflows=[CastSheetWorkflow],
                          activities=activities):
            ok = await asyncio.wait_for(env.client.execute_workflow(
                CastSheetWorkflow.run, {"episode_id": EP, "jobs": jobs[:2]},
                id=cast_sheet_workflow_id(jobs[0]["job_id"]), task_queue="cast-sheet-test"), timeout=25)
            assert ok["status"] == "completed" and [r["status"] for r in ok["results"]] == ["completed"] * 2
            broken = [jobs[2] | {"approved_usd_micros": 0}, *[
                {**j, "job_id": j["job_id"] + "-x"} for j in jobs[:1]]]
            cast_store.record_sheet_job({**broken[1], "workflow_id": "w2", "requested_by": "t"})
            failed = await asyncio.wait_for(env.client.execute_workflow(
                CastSheetWorkflow.run, {"episode_id": EP, "jobs": broken},
                id="cast-sheet-broken", task_queue="cast-sheet-test"), timeout=25)
    assert failed["status"] == "failed"
    assert [r["status"] for r in failed["results"]] == ["failed", "skipped"]
    assert _sheet_status(jobs[2]["job_id"]) == "refused" and _sheet_status(broken[1]["job_id"]) == "skipped"
    assert len(cast_store.get_version(pip["character_id"], 1)["images"]) == 2


async def test_the_reconcile_workflow_settles_cast_sheet_jobs(tmp_path, monkeypatch):
    """ReconcilePendingJobsWorkflow routes castsheet-... jobs to their own activity (real provider
    client against mocked HTTP)."""
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow

    db_path = await _live_db(tmp_path, monkeypatch)
    pip = new_character(("episode", EP))
    job = _job(pip, source="text")
    ledger, credits = BudgetLedger(db_path), CreditLedger(db_path)
    await credits.init_db()
    await credits.create_episode_line(EP, cap_credits=500)
    assert await ledger.reserve(EP, "L1_refs", 390_000, "sheet", job_id=job["job_id"])
    await ledger.record_job(EP, job["job_id"], None, job["model_path"], "final", "L1_refs", 390_000,
                            user_id=OWNER_ID)
    await credits.reserve_credits(EP, job["job_id"], 6.5)
    await credits.persist_provider_job(job["job_id"], "prov-sheet-9")
    await ledger.mark_job_status(job["job_id"], "pending_reconcile")
    cast_store.update_sheet_job(job["job_id"], status="pending_reconcile")
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue="reconcile-cast", workflows=[ReconcilePendingJobsWorkflow],
                          activities=PRODUCTION_ACTIVITIES):
            with respx.mock(assert_all_called=False) as router:
                router.get(f"{BASE}/models/usage").mock(return_value=httpx.Response(200, json={"usage": []}))
                router.get(f"{BASE}/requests/prov-sheet-9/status").mock(return_value=httpx.Response(
                    200, json={"status": "completed", "images": [{"url": OUTPUT}]}))
                _respx_output(router)
                summary = await asyncio.wait_for(env.client.execute_workflow(
                    ReconcilePendingJobsWorkflow.run, args=[100, None], id="reconcile-cast-1",
                    task_queue="reconcile-cast"), timeout=50)
    assert (summary["total"], summary["committed"], summary["errors"]) == (1, 1, []), summary
    assert await _credit_txns(db_path, job["job_id"]) == ["reserve", "commit"]
    assert (await _ledger_row(db_path, job["job_id"]))["status"] == "committed"
    assert _sheet_status(job["job_id"]) == "completed"
    stored = [i for i in cast_store.get_version(pip["character_id"], 1)["images"] if i["job_id"] == job["job_id"]]
    assert len(stored) == 1 and stored[0]["placeholder"] is False
    assert json.loads(json.dumps(summary))["stalled"] == []
