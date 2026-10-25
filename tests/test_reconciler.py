"""G-REC-2 reconciler + G-REC-3 stall watchdog.

Reconciler: re-poll pending_reconcile jobs by harness job id (live poll path, provider HTTP mocked
with respx), land late assets (download, normalize, attach), commit or release budget
idempotently (never double-charged), scheduled by the worker (Temporal Schedule) and runnable
from the CLI. Watchdog: configurable threshold, alert visible in the API, routed to the
reconciler."""

from __future__ import annotations

import asyncio
import io
import json
import sys
from datetime import datetime, timedelta, timezone

import aiosqlite
import httpx
import pytest
import respx
from click.testing import CliRunner
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

BASE = "https://api.higgsfield.ai"
TASK_QUEUE = "reconcile-tasks"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _jpeg() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(out, format="JPEG")
    return out.getvalue()


async def _set_updated(db_path, job_id, minutes_ago):
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE job_ledger SET updated_at = ?, created_at = ? WHERE job_id = ?",
                         (ts, ts, job_id))
        await db.commit()


async def _job_status(db_path, job_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status FROM job_ledger WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None


async def _audits(db_path, action):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT details FROM audit_log WHERE action = ? ORDER BY id", (action,)) as cur:
            return [json.loads(r[0]) for r in await cur.fetchall()]


@pytest.fixture
async def live_db(tmp_path, monkeypatch):
    """Live-mode episode ep99 (G1.08 approved) with standard lines; provider HTTP is mocked."""
    from tests.ledger_helpers import set_balance
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

    db_path = str(tmp_path / "studio.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await BudgetLedger(db_path).init_db()
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)
    credits = CreditLedger(db_path)
    await credits.init_db()
    await credits.create_episode_line("ep99", cap_credits=500)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", BASE)
    monkeypatch.setenv("HFVG_EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.delenv("STALL_THRESHOLD_MINUTES", raising=False)
    return db_path


async def _submitted(db_path, job_id, shot_id, usd, provider_id=None, line="L4_video", pending=True):
    """A job as submit leaves it: USD hold + per-job row + credit hold + provider id mapping."""
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger

    ledger = BudgetLedger(db_path)
    assert await ledger.reserve("ep99", line, usd, f"submit {job_id}", job_id=job_id)
    model = "kling-video/v3.0/pro/image-to-video" if job_id.startswith("clip-") else "seedream"
    await ledger.record_job("ep99", job_id, shot_id, model, "final", line, usd)
    credits = CreditLedger(db_path)
    await credits.reserve_credits("ep99", job_id, 7.5)
    await credits.persist_provider_job(job_id, provider_id or f"prov-{job_id}")
    if pending:
        await ledger.mark_job_status(job_id, "pending_reconcile")


def _status_route(router, job_id, body):
    return router.get(f"{BASE}/requests/prov-{job_id}/status").mock(return_value=httpx.Response(200, json=body))


def _reconcile_worker(env):
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[ReconcilePendingJobsWorkflow],
                  activities=PRODUCTION_ACTIVITIES)


async def _run_pass(env, run_id, threshold=None):
    from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow
    return await asyncio.wait_for(env.client.execute_workflow(
        ReconcilePendingJobsWorkflow.run, args=[100, threshold], id=run_id, task_queue=TASK_QUEUE),
        timeout=50)


# ─────────────────────────────────────────────────────────────────────────────
# Stall watchdog (G-REC-3)
# ─────────────────────────────────────────────────────────────────────────────

class TestStallWatchdog:
    @pytest.mark.asyncio
    async def test_threshold_alert_and_routing(self, live_db):
        """A reserved job idle for >= threshold gets an alert + audit and is routed to the
        reconciler (pending_reconcile, hold kept); a fresh job is untouched."""
        from hfvg.activities.reconcile import list_stall_alerts, raise_stall_alerts
        from hfvg.budget import BudgetLedger

        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False)
        await _submitted(live_db, "clip-ep99-A02-v1", "A02", 336_000, pending=False)
        await _set_updated(live_db, "clip-ep99-A01-v1", 60)
        await _set_updated(live_db, "clip-ep99-A02-v1", 5)
        reserved_before = (await BudgetLedger(live_db).get_line_status("ep99", "L4_video"))["reserved"]

        stalled = await raise_stall_alerts(live_db, 45, route=True)
        assert [s["job_id"] for s in stalled] == ["clip-ep99-A01-v1"], stalled
        assert stalled[0]["minutes_stalled"] >= 59 and stalled[0]["threshold_minutes"] == 45
        assert await _job_status(live_db, "clip-ep99-A01-v1") == "pending_reconcile"
        assert await _job_status(live_db, "clip-ep99-A02-v1") == "reserved"
        alerts = await list_stall_alerts(live_db, "ep99")
        assert [(a["job_id"], a["status"]) for a in alerts] == [("clip-ep99-A01-v1", "routed")]
        assert (await BudgetLedger(live_db).get_line_status("ep99", "L4_video"))["reserved"] == reserved_before
        audit = await _audits(live_db, "job_stalled")
        assert audit and audit[0]["job_id"] == "clip-ep99-A01-v1" and audit[0]["routed_to_reconciler"] is True

    @pytest.mark.asyncio
    async def test_finished_jobs_never_alert(self, live_db):
        from hfvg.activities.reconcile import raise_stall_alerts
        from hfvg.budget import BudgetLedger

        ledger = BudgetLedger(live_db)
        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False)
        await _submitted(live_db, "clip-ep99-A02-v1", "A02", 336_000, pending=False)
        await ledger.commit("ep99", "L4_video", 336_000, job_id="clip-ep99-A01-v1")
        await ledger.release("ep99", "L4_video", 336_000, job_id="clip-ep99-A02-v1")
        for job in ("clip-ep99-A01-v1", "clip-ep99-A02-v1"):
            await _set_updated(live_db, job, 600)
        assert await raise_stall_alerts(live_db, 45) == []
        assert await _job_status(live_db, "clip-ep99-A01-v1") == "committed"
        assert await _job_status(live_db, "clip-ep99-A02-v1") == "released"

    @pytest.mark.asyncio
    async def test_threshold_configurable_and_validated(self, live_db, monkeypatch):
        from hfvg.activities.reconcile import find_stalled_jobs, raise_stall_alerts, stall_threshold_minutes
        from hfvg.workflows.reconcile import ensure_reconcile_schedule

        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False)
        await _set_updated(live_db, "clip-ep99-A01-v1", 60)
        assert stall_threshold_minutes() == 45
        monkeypatch.setenv("STALL_THRESHOLD_MINUTES", "90")
        assert await raise_stall_alerts(live_db, None, route=False) == []
        monkeypatch.setenv("STALL_THRESHOLD_MINUTES", "30")
        assert [s["job_id"] for s in await raise_stall_alerts(live_db, None, route=False)] == ["clip-ep99-A01-v1"]
        assert await _job_status(live_db, "clip-ep99-A01-v1") == "reserved", "route=False must not route"
        assert len(await find_stalled_jobs(live_db, 61)) == 0
        for bad in ("0", "-5"):
            monkeypatch.setenv("STALL_THRESHOLD_MINUTES", bad)
            err = None
            try:
                stall_threshold_minutes()
            except ValueError as e:
                err = e
            assert err is not None and "positive" in str(err)
        err = None
        try:
            await ensure_reconcile_schedule(None, TASK_QUEUE, interval_minutes=0)
        except ValueError as e:
            err = e
        assert err is not None and "positive" in str(err)

    @pytest.mark.asyncio
    async def test_alerts_visible_in_api(self, live_db, monkeypatch):
        """GET /api/stalls and /api/episodes/{id}/stalls list alerts (status filter validated);
        the episode state carries the count of unresolved alerts."""
        from unittest.mock import AsyncMock
        from tests.test_gate_correctness_round1 import _api_client
        from hfvg.activities.reconcile import raise_stall_alerts, record_reconcile_outcome

        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False)
        await _submitted(live_db, "clip-ep99-A02-v1", "A02", 336_000, pending=False)
        await _set_updated(live_db, "clip-ep99-A01-v1", 60)
        await _set_updated(live_db, "clip-ep99-A02-v1", 50)
        assert len(await raise_stall_alerts(live_db, 45)) == 2
        await ActivityEnvironment().run(record_reconcile_outcome, {
            "job_id": "clip-ep99-A02-v1", "episode_id": "ep99", "shot_id": "A02", "job_type": "clip"},
            "released", "test")

        client, _, wf_handle = await _api_client(live_db, monkeypatch)
        body = client.get("/api/stalls").json()
        assert body["active_count"] == 1 and body["threshold_minutes"] == 45, body
        assert {a["job_id"]: a["status"] for a in body["alerts"]} == {
            "clip-ep99-A01-v1": "routed", "clip-ep99-A02-v1": "resolved"}
        resolved = client.get("/api/episodes/ep99/stalls", params={"status": "resolved"}).json()
        assert [a["job_id"] for a in resolved["alerts"]] == ["clip-ep99-A02-v1"], resolved
        assert resolved["alerts"][0]["resolution"] == "released"
        routed = client.get("/api/stalls", params={"status": "routed"}).json()
        assert [a["job_id"] for a in routed["alerts"]] == ["clip-ep99-A01-v1"], routed
        assert client.get("/api/stalls", params={"status": "bogus"}).status_code == 422
        assert client.get("/api/episodes/ep98/stalls").json()["alerts"] == []
        wf_handle.query = AsyncMock(return_value={"episode_id": "ep99", "stage": "G3", "approvals": {}})
        state = client.get("/api/episodes/ep99")
        assert state.status_code == 200 and state.json()["active_stall_alerts"] == 1, state.text
        sys.modules.pop("api.main", None)

    def test_cli_stalled_reports_and_routes(self, live_db):
        from hfvg.cli import cli

        asyncio.run(_submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False))
        asyncio.run(_set_updated(live_db, "clip-ep99-A01-v1", 60))
        runner = CliRunner()
        out = runner.invoke(cli, ["stalled", "--threshold", "45"])
        assert out.exit_code == 0, out.output
        assert "Found 1 stalled job(s)" in out.output and "clip-ep99-A01-v1" in out.output
        assert asyncio.run(_job_status(live_db, "clip-ep99-A01-v1")) == "reserved", "read-only by default"
        out = runner.invoke(cli, ["stalled", "--threshold", "45", "--route"])
        assert out.exit_code == 0, out.output
        assert "routed to reconciler" in out.output
        assert asyncio.run(_job_status(live_db, "clip-ep99-A01-v1")) == "pending_reconcile"
        out = runner.invoke(cli, ["stalled", "--threshold", "120"])
        assert "No stalled jobs found" in out.output


# ─────────────────────────────────────────────────────────────────────────────
# Reconciler workflow (G-REC-2)
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcilerWorkflow:
    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_reconcile_commits_releases_lands_and_never_double_charges(self, live_db, tmp_path):
        """A completed clip -> commit + land + attach; B failed -> release; C running -> left
        pending; D completed still -> commit + land (normalized PNG). A second pass and a direct
        repeat commit charge nothing more."""
        from hfvg.activities.studio_generation import commit_job_budget
        from hfvg.budget import BudgetLedger
        from hfvg.credit_ledger import CreditLedger

        ledger, credits = BudgetLedger(live_db), CreditLedger(live_db)
        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000)
        await _submitted(live_db, "clip-ep99-A02-v1", "A02", 400_000)
        await _submitted(live_db, "clip-ep99-A03-v1", "A03", 500_000)
        await _submitted(live_db, "still-ep99-A04-v1", "A04", 50_000, line="L3_final_stills")
        video_before = await ledger.get_line_status("ep99", "L4_video")
        stills_before = await ledger.get_line_status("ep99", "L3_final_stills")

        async with await WorkflowEnvironment.start_local() as env:
            async with _reconcile_worker(env):
                with respx.mock(assert_all_called=False) as router:
                    _status_route(router, "clip-ep99-A01-v1",
                                  {"status": "completed", "video": {"url": "https://cdn.test/a01.mp4"}})
                    _status_route(router, "clip-ep99-A02-v1", {"status": "failed", "error": "provider exploded"})
                    _status_route(router, "clip-ep99-A03-v1", {"status": "in_progress"})
                    _status_route(router, "still-ep99-A04-v1",
                                  {"status": "completed", "images": [{"url": "https://cdn.test/a04.jpg"}]})
                    router.get("https://cdn.test/a01.mp4").mock(return_value=httpx.Response(200, content=MP4))
                    router.get("https://cdn.test/a04.jpg").mock(return_value=httpx.Response(200, content=_jpeg()))
                    first = await _run_pass(env, "reconcile-1")
                    second = await _run_pass(env, "reconcile-2")

        assert (first["total"], first["committed"], first["released"], first["landed"],
                first["still_pending"], first["errors"]) == (4, 2, 1, 2, 1, []), first
        assert (second["total"], second["committed"], second["released"], second["landed"],
                second["still_pending"]) == (1, 0, 0, 0, 1), second
        assert await _job_status(live_db, "clip-ep99-A01-v1") == "committed"
        assert await _job_status(live_db, "clip-ep99-A02-v1") == "released"
        assert await _job_status(live_db, "clip-ep99-A03-v1") == "pending_reconcile"
        assert await _job_status(live_db, "still-ep99-A04-v1") == "committed"

        video = await ledger.get_line_status("ep99", "L4_video")
        stills = await ledger.get_line_status("ep99", "L3_final_stills")
        assert video["spent"] - video_before["spent"] == 336_000, (video_before, video)
        assert video_before["reserved"] - video["reserved"] == 336_000 + 400_000, "C's hold stays"
        assert stills["spent"] - stills_before["spent"] == 50_000
        # repeat commit (e.g. the shot workflow committing late) is a no-op
        await ActivityEnvironment().run(commit_job_budget, "ep99", "A01", "clip-ep99-A01-v1", "clip",
                                        "L4_video", 336_000, None)
        assert (await ledger.get_line_status("ep99", "L4_video"))["spent"] == video["spent"]
        cred = await credits.get_episode_status("ep99")
        assert cred["spent_credits"] == pytest.approx(15.0), cred
        assert cred["reserved_credits"] == pytest.approx(7.5), cred

        async with aiosqlite.connect(live_db) as db:
            async with db.execute("SELECT shot_id, clip_url, still_url, status FROM shots "
                                  "WHERE episode_id = 'ep99' ORDER BY shot_id") as cur:
                shots = {r[0]: r[1:] for r in await cur.fetchall()}
            async with db.execute("SELECT job_id, local_path, attached, status FROM late_assets") as cur:
                late = {r[0]: r[1:] for r in await cur.fetchall()}
        assert shots["A01"][0] == "https://cdn.test/a01.mp4" and shots["A01"][2] == "needs_review", shots
        assert shots["A04"][1] == "https://cdn.test/a04.jpg", shots
        a01_path, attached, status = late["clip-ep99-A01-v1"]
        assert status == "landed" and attached == 1 and open(a01_path, "rb").read() == MP4
        a04_path = late["still-ep99-A04-v1"][0]
        assert a04_path.endswith(".png") and open(a04_path, "rb").read().startswith(b"\x89PNG"), a04_path
        assert len(await _audits(live_db, "reconcile_committed")) == 2
        assert len(await _audits(live_db, "reconcile_released")) == 1

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_failed_landing_is_retried_without_recommit(self, live_db):
        """The late still's first download is not an image: committed once, landing recorded as
        failed; the next pass re-lands it (no re-poll, no second commit) as a normalized PNG."""
        from hfvg.budget import BudgetLedger

        ledger = BudgetLedger(live_db)
        await _submitted(live_db, "still-ep99-A04-v1", "A04", 50_000, line="L3_final_stills")
        before = await ledger.get_line_status("ep99", "L3_final_stills")
        async with await WorkflowEnvironment.start_local() as env:
            async with _reconcile_worker(env):
                with respx.mock(assert_all_called=False) as router:
                    poll = _status_route(router, "still-ep99-A04-v1",
                                         {"status": "completed", "images": [{"url": "https://cdn.test/a04"}]})
                    router.get("https://cdn.test/a04").mock(side_effect=[
                        httpx.Response(200, content=b"<html>oops</html>"),
                        httpx.Response(200, content=_jpeg())])
                    first = await _run_pass(env, "reconcile-1")
                    second = await _run_pass(env, "reconcile-2")
                    polls = poll.call_count
        assert (first["committed"], first["landed"], first["land_failed"]) == (1, 0, 1), first
        assert (second["committed"], second["landed"], second["land_failed"]) == (0, 1, 0), second
        assert polls == 1, "a committed job is never re-polled"
        after = await ledger.get_line_status("ep99", "L3_final_stills")
        assert after["spent"] - before["spent"] == 50_000
        async with aiosqlite.connect(live_db) as db:
            async with db.execute("SELECT local_path, status, attempts FROM late_assets") as cur:
                local_path, status, attempts = await cur.fetchone()
        assert status == "landed" and attempts == 2
        assert local_path.endswith(".png") and open(local_path, "rb").read().startswith(b"\x89PNG\r\n\x1a\n")

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_content_block_and_cancel_on_reconcile_release(self, live_db):
        """A late nsfw block and a late cancel are terminal: both holds released, never committed."""
        from hfvg.budget import BudgetLedger

        ledger = BudgetLedger(live_db)
        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000)
        await _submitted(live_db, "clip-ep99-A02-v1", "A02", 336_000)
        before = await ledger.get_line_status("ep99", "L4_video")
        async with await WorkflowEnvironment.start_local() as env:
            async with _reconcile_worker(env):
                with respx.mock(assert_all_called=False) as router:
                    _status_route(router, "clip-ep99-A01-v1", {"status": "nsfw", "error": "flagged"})
                    _status_route(router, "clip-ep99-A02-v1", {"status": "canceled"})
                    result = await _run_pass(env, "reconcile-1")
        assert (result["released"], result["committed"], result["still_pending"]) == (2, 0, 0), result
        assert await _job_status(live_db, "clip-ep99-A01-v1") == "released"
        assert await _job_status(live_db, "clip-ep99-A02-v1") == "released"
        after = await ledger.get_line_status("ep99", "L4_video")
        assert after["spent"] == before["spent"] and before["reserved"] - after["reserved"] == 672_000


class TestLandLateAsset:
    @pytest.mark.asyncio
    async def test_never_overwrites_existing_asset(self, live_db):
        """A shot that already has a clip keeps it; the late clip is landed and listed only."""
        from hfvg.activities.reconcile import land_late_asset

        async with aiosqlite.connect(live_db) as db:
            await db.execute("""INSERT INTO shots (id, episode_id, shot_id, status, clip_url, created_at, updated_at)
                                VALUES ('ep99-A01', 'ep99', 'A01', 'approved', 'https://cdn.test/newer.mp4',
                                        datetime('now'), datetime('now'))""")
            await db.commit()
        job = {"job_id": "clip-ep99-A01-v1", "episode_id": "ep99", "shot_id": "A01", "job_type": "clip"}
        with respx.mock() as router:
            router.get("https://cdn.test/late.mp4").mock(return_value=httpx.Response(200, content=MP4))
            landed = await ActivityEnvironment().run(land_late_asset, job, "https://cdn.test/late.mp4")
            again = await ActivityEnvironment().run(land_late_asset, job, "https://cdn.test/late.mp4")
            downloads = router.calls.call_count
        assert landed["landed"] is True and landed["attached"] is False, landed
        assert again.get("idempotent") is True and downloads == 1
        async with aiosqlite.connect(live_db) as db:
            async with db.execute("SELECT clip_url, status, qc_results FROM shots WHERE id = 'ep99-A01'") as cur:
                clip_url, status, qc = await cur.fetchone()
        assert clip_url == "https://cdn.test/newer.mp4" and status == "approved"
        assert [a["job_id"] for a in json.loads(qc)["late_assets"]] == ["clip-ep99-A01-v1"]

    @pytest.mark.asyncio
    async def test_end_frame_still_listed_not_attached(self, live_db):
        from hfvg.activities.reconcile import land_late_asset

        job = {"job_id": "still-ep99-A01_end-v1", "episode_id": "ep99", "shot_id": "A01_end", "job_type": "still"}
        with respx.mock() as router:
            router.get("https://cdn.test/end.jpg").mock(return_value=httpx.Response(200, content=_jpeg()))
            landed = await ActivityEnvironment().run(land_late_asset, job, "https://cdn.test/end.jpg")
        assert landed["landed"] is True and landed["attached"] is False, landed
        async with aiosqlite.connect(live_db) as db:
            async with db.execute("SELECT still_url, qc_results FROM shots WHERE id = 'ep99-A01'") as cur:
                still_url, qc = await cur.fetchone()
        assert still_url is None, "an end frame must never replace the shot's start still"
        assert json.loads(qc)["late_assets"][0]["shot_id"] == "A01_end"

    @pytest.mark.asyncio
    async def test_wrong_container_fails_and_gives_up_after_max_attempts(self, live_db):
        """A clip URL that serves HTML is never landed; the job is retried by later passes until
        MAX_LAND_ATTEMPTS, then dropped from the work list."""
        from hfvg.activities.reconcile import MAX_LAND_ATTEMPTS, get_pending_reconcile_jobs, land_late_asset
        from hfvg.budget import BudgetLedger

        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000, pending=False)
        await BudgetLedger(live_db).commit("ep99", "L4_video", 336_000, job_id="clip-ep99-A01-v1")
        job = {"job_id": "clip-ep99-A01-v1", "episode_id": "ep99", "shot_id": "A01", "job_type": "clip"}
        env = ActivityEnvironment()
        with respx.mock() as router:
            router.get("https://cdn.test/a01.mp4").mock(return_value=httpx.Response(200, content=b"<html/>"))
            for attempt in range(1, MAX_LAND_ATTEMPTS + 1):
                result = await env.run(land_late_asset, job, "https://cdn.test/a01.mp4")
                assert result["landed"] is False and result["attempts"] == attempt, result
                work = await env.run(get_pending_reconcile_jobs, 100)
                expected = [] if attempt >= MAX_LAND_ATTEMPTS else [("clip-ep99-A01-v1", "land")]
                assert [(j["job_id"], j["phase"]) for j in work] == expected, (attempt, work)
        assert "not an MP4/WebM" in result["error"]
        async with aiosqlite.connect(live_db) as db:
            async with db.execute("SELECT clip_url FROM shots WHERE id = 'ep99-A01'") as cur:
                row = await cur.fetchone()
        assert row is None or row[0] is None


# ─────────────────────────────────────────────────────────────────────────────
# Scheduling: worker registration, Temporal Schedule, CLI
# ─────────────────────────────────────────────────────────────────────────────

async def _describe(client, schedule_id):
    from temporalio.service import RPCError
    try:
        return await client.get_schedule_handle(schedule_id).describe()
    except RPCError:
        return None


class TestScheduling:
    def test_worker_registers_reconciler(self):
        from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS
        from hfvg.workflows.reconcile import ReconcilePendingJobsWorkflow

        names = {getattr(a, "__temporal_activity_definition").name for a in PRODUCTION_ACTIVITIES}
        assert ReconcilePendingJobsWorkflow in PRODUCTION_WORKFLOWS
        assert {"detect_stalled_jobs", "get_pending_reconcile_jobs", "land_late_asset",
                "record_reconcile_outcome", "poll_job_status", "commit_job_budget", "release_job_budget",
                "commit_credits_activity", "release_credits_activity",
                "attempt_moderation_recovery"} <= names, names

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_worker_creates_the_schedule_idempotently(self, monkeypatch):
        from hfvg.worker import setup_schedules
        from hfvg.workflows.reconcile import RECONCILE_SCHEDULE_ID, RECONCILE_WORKFLOW_ID

        async with await WorkflowEnvironment.start_local() as env:
            monkeypatch.setenv("RECONCILE_SCHEDULE_ENABLED", "false")
            assert await setup_schedules(env.client, TASK_QUEUE) is False
            assert await _describe(env.client, RECONCILE_SCHEDULE_ID) is None
            monkeypatch.setenv("RECONCILE_SCHEDULE_ENABLED", "true")
            monkeypatch.delenv("RECONCILE_INTERVAL_MINUTES", raising=False)
            assert await setup_schedules(env.client, TASK_QUEUE) is True
            desc = await _describe(env.client, RECONCILE_SCHEDULE_ID)
            assert desc is not None, "the worker must create the reconcile schedule"
            action = desc.schedule.action
            assert action.workflow == "ReconcilePendingJobsWorkflow" and action.id == RECONCILE_WORKFLOW_ID
            assert action.task_queue == TASK_QUEUE
            assert desc.schedule.spec.intervals[0].every == timedelta(minutes=15)
            monkeypatch.setenv("RECONCILE_INTERVAL_MINUTES", "5")
            assert await setup_schedules(env.client, TASK_QUEUE) is True  # restart: update, no error
            desc = await _describe(env.client, RECONCILE_SCHEDULE_ID)
            assert desc.schedule.spec.intervals[0].every == timedelta(minutes=5)

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_cli_reconcile_runs_a_pass_and_creates_schedule(self, live_db, monkeypatch):
        from hfvg.cli import cli
        from hfvg.config import config
        from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS
        from hfvg.workflows.reconcile import RECONCILE_SCHEDULE_ID

        await _submitted(live_db, "clip-ep99-A01-v1", "A01", 336_000)
        async with await WorkflowEnvironment.start_local() as env:
            monkeypatch.setattr(config, "TEMPORAL_HOST", env.client.service_client.config.target_host)
            monkeypatch.setattr(config, "TEMPORAL_NAMESPACE", env.client.namespace)
            monkeypatch.setattr(config, "TASK_QUEUE", TASK_QUEUE)
            async with Worker(env.client, task_queue=TASK_QUEUE, workflows=PRODUCTION_WORKFLOWS,
                              activities=PRODUCTION_ACTIVITIES):
                with respx.mock(assert_all_called=False) as router:
                    _status_route(router, "clip-ep99-A01-v1", {"status": "failed", "error": "boom"})
                    out = await asyncio.to_thread(CliRunner().invoke, cli, ["reconcile", "--max-jobs", "10"])
                assert out.exit_code == 0, (out.output, out.exception)
                assert "released: 1" in out.output, out.output
                assert await _job_status(live_db, "clip-ep99-A01-v1") == "released"
                out = await asyncio.to_thread(CliRunner().invoke, cli, ["reconcile", "--schedule", "--interval", "7"])
                assert out.exit_code == 0, (out.output, out.exception)
                assert f"Schedule {RECONCILE_SCHEDULE_ID} ensured" in out.output
                desc = await _describe(env.client, RECONCILE_SCHEDULE_ID)
                assert desc is not None and desc.schedule.spec.intervals[0].every == timedelta(minutes=7)
