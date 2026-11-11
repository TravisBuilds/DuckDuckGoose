"""The single studio run lock asks Temporal whether its holder's run is still open, with the client of
the worker running the activity (activity.client()). The call it made before does not exist in
temporalio, so every check fell back to the lock's age: a run waiting at a gate for over an hour lost
the lock to the next start, and a force-stopped run (which never releases it) blocked starts for up to
an hour. These run the real activity on a real worker against a Temporal dev server: nothing stands in
for the client, the describe or the lock table."""
import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities.run_lock import acquire_run_lock, release_run_lock
from hfvg.credit_ledger import CreditLedger

TASK_QUEUE = "run-lock-holder"
ONCE = RetryPolicy(maximum_attempts=1)


@workflow.defn(name="RunLockGateRun", sandboxed=False)
class GateRun:
    """An episode run in miniature, as EpisodeWorkflowV2 does it: takes the run lock at the start,
    waits at a gate until approved, releases the lock at the end."""

    def __init__(self) -> None:
        self.approved = False

    @workflow.run
    async def run(self, episode_id: str) -> dict:
        lock = await workflow.execute_activity(acquire_run_lock, args=[episode_id, workflow.info().workflow_id, "default"],
                                               start_to_close_timeout=timedelta(seconds=10), retry_policy=ONCE)
        await workflow.wait_condition(lambda: self.approved)
        await workflow.execute_activity(release_run_lock, args=[episode_id], start_to_close_timeout=timedelta(seconds=10),
                                        retry_policy=RetryPolicy(maximum_attempts=3))
        return lock

    @workflow.signal
    def approve(self) -> None:
        self.approved = True


@workflow.defn(name="RunLockNextStart", sandboxed=False)
class NextStart:
    """The next episode's start: takes the run lock, or says why it may not."""

    @workflow.run
    async def run(self, episode_id: str) -> dict:
        try:
            return await workflow.execute_activity(
                acquire_run_lock, args=[episode_id, workflow.info().workflow_id, "default"],
                start_to_close_timeout=timedelta(seconds=10), retry_policy=ONCE)
        except ActivityError as e:
            return {"refused": str(e.cause)}


@pytest.fixture
def lock_db(tmp_path, monkeypatch):
    db = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.delenv("RUN_LOCK_STALE_TIMEOUT_SECONDS", raising=False)  # the default hour
    return db


async def _locks(db: str) -> list[tuple[str, str]]:
    async with aiosqlite.connect(db) as conn:
        async with conn.execute("SELECT episode_id, workflow_id FROM run_locks ORDER BY episode_id") as cur:
            return [tuple(r) for r in await cur.fetchall()]


async def _held_by(db: str, episode_id: str, timeout: float = 20.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            if await _locks(db) == [(episode_id, episode_id)]:
                return
        except aiosqlite.OperationalError:  # the activity has not created the table yet
            pass
        await asyncio.sleep(0.1)
    raise AssertionError(f"{episode_id} never took the run lock: {await _locks(db)}")


async def _taken_hours_ago(db: str, hours: float) -> None:
    then = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    async with aiosqlite.connect(db) as conn:
        await conn.execute("UPDATE run_locks SET locked_at = ?", (then,))
        await conn.commit()


def _worker(env: WorkflowEnvironment) -> Worker:
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[GateRun, NextStart],
                  activities=[acquire_run_lock, release_run_lock])


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_run_waiting_at_a_gate_keeps_the_lock_past_the_stale_timeout(lock_db):
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            gate = await env.client.start_workflow(GateRun.run, "ep81", id="ep81", task_queue=TASK_QUEUE)
            await _held_by(lock_db, "ep81")
            await _taken_hours_ago(lock_db, 2)  # two hours at the gate, past the one-hour stale timeout
            out = await env.client.execute_workflow(NextStart.run, "ep82", id="ep82", task_queue=TASK_QUEUE)
            assert "already has episode ep81 running" in out.get("refused", ""), out
            assert await _locks(lock_db) == [("ep81", "ep81")]
            assert (await gate.describe()).status == WorkflowExecutionStatus.RUNNING

            await gate.signal(GateRun.approve)
            assert (await gate.result())["acquired"] is True
            assert await _locks(lock_db) == []
            out = await env.client.execute_workflow(NextStart.run, "ep82", id="ep82-after", task_queue=TASK_QUEUE)
            assert out.get("acquired") is True, out
            assert await _locks(lock_db) == [("ep82", "ep82-after")]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_force_stopped_run_gives_the_lock_to_the_next_start_at_once(lock_db):
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            gate = await env.client.start_workflow(GateRun.run, "ep83", id="ep83", task_queue=TASK_QUEUE)
            await _held_by(lock_db, "ep83")
            await gate.terminate(reason="force stop")
            assert (await gate.describe()).status == WorkflowExecutionStatus.TERMINATED
            assert await _locks(lock_db) == [("ep83", "ep83")]  # a terminated run never releases its lock

            out = await env.client.execute_workflow(NextStart.run, "ep84", id="ep84", task_queue=TASK_QUEUE)
            assert out.get("acquired") is True, out
            assert await _locks(lock_db) == [("ep84", "ep84")]


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_holder_temporal_cannot_describe_still_goes_by_the_lock_age(lock_db):
    ledger = CreditLedger(lock_db)
    await ledger.init_db()
    assert (await ledger.acquire_run_lock("ep85", "ep85-on-another-server", "default"))["acquired"]
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env):
            out = await env.client.execute_workflow(NextStart.run, "ep86", id="ep86", task_queue=TASK_QUEUE)
            assert "already has episode ep85 running" in out.get("refused", ""), out
            assert await _locks(lock_db) == [("ep85", "ep85-on-another-server")]

            await _taken_hours_ago(lock_db, 2)
            out = await env.client.execute_workflow(NextStart.run, "ep86", id="ep86-later", task_queue=TASK_QUEUE)
            assert out == {"acquired": True, "existing_episode": "ep85", "reclaimed": True}, out
            assert await _locks(lock_db) == [("ep86", "ep86-later")]
