"""A run its worker dropped touches no other workflow when the garbage collector closes it.

A worker that stops (every Temporal test ends its Worker; a deploy stops the worker) drops its cached
runs without evicting them, and the garbage collector later closes their suspended coroutines on
whatever thread it happens to run. On a workflow thread in the middle of another run's activation,
workflow calls made by the closing coroutine land in that other run. EpisodeWorkflowV2.run released
its run lock in such a ``finally``: main's CI on 3ba171d (test-python 3.11) failed
test_g2_child_failure.py::test_b_all_shots_ok_g2_still_needs_approval when a dropped ep05 run
scheduled release_run_lock inside test_b's query answer ("Workflow completion had a legacy query
response along with other commands"); the query hung until the test timed out.
"""
import asyncio
import gc
import logging
import uuid
import weakref
from types import CoroutineType, SimpleNamespace

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
from tests.test_g2_child_failure import FakeShot, _acts, _wait_for


class _Suspend:
    def __await__(self):
        yield "suspended"


def _drive(monkeypatch, run_with_lock):
    """EpisodeWorkflowV2.run outside a worker, workflow calls stubbed: (coroutine, activities called)."""
    calls: list[str] = []

    async def execute_activity(name, *args, **kwargs):
        calls.append(name)
        return {}

    monkeypatch.setattr(workflow, "execute_activity", execute_activity)
    monkeypatch.setattr(workflow, "info", lambda: SimpleNamespace(workflow_id="ep-test"))
    monkeypatch.setattr(workflow, "logger", logging.getLogger("tests.dropped_run"))
    wf = EpisodeWorkflowV2()
    monkeypatch.setattr(wf, "_run_with_lock", run_with_lock)
    return wf.run("ep-test", None, True), calls


def test_closing_a_dropped_run_schedules_nothing(monkeypatch):
    async def parked(*args):
        await _Suspend()

    coro, calls = _drive(monkeypatch, parked)
    assert coro.send(None) == "suspended"
    assert calls == ["acquire_run_lock"]
    coro.close()  # what the garbage collector does to the coroutine of a dropped run
    assert calls == ["acquire_run_lock"], "closing a dropped run scheduled an activity"


@pytest.mark.parametrize("ending", ["returns", "raises", "is cancelled"])
def test_a_run_that_ends_still_releases_its_lock(monkeypatch, ending):
    async def parked(*args):
        await _Suspend()
        if ending == "raises":
            raise RuntimeError("boom")
        return {"status": "completed"}

    coro, calls = _drive(monkeypatch, parked)
    coro.send(None)
    if ending == "is cancelled":
        with pytest.raises(asyncio.CancelledError):
            coro.throw(asyncio.CancelledError())
    elif ending == "raises":
        with pytest.raises(RuntimeError):
            coro.send(None)
    else:
        with pytest.raises(StopIteration) as done:
            coro.send(None)
        assert done.value.value == {"status": "completed"}
    assert calls == ["acquire_run_lock", "release_run_lock"]


@workflow.defn(name="DroppedRunCollector", sandboxed=False)
class Collector:
    def __init__(self):
        self.collected: int | None = None
        self.done = False

    @workflow.run
    async def run(self) -> str:
        await workflow.wait_condition(lambda: self.done)
        return "done"

    @workflow.signal
    def collect_garbage(self):
        # not a query: a query handler runs read-only, so a misplaced call there only raises an error
        # the dropped run swallows; the rest of an activation takes the call as this run's command
        self.collected = gc.collect()

    @workflow.signal
    def finish(self):
        self.done = True

    @workflow.query
    def garbage_collected(self) -> bool:
        return self.collected is not None


def _suspended_episode_runs() -> list:
    # by name: the sandbox runs its own import of the workflow module, so the code object differs.
    # type(), not isinstance(): sandbox restriction proxies other tests leave behind raise on __class__
    return [o for o in gc.get_objects()
            if type(o) is CoroutineType and o.cr_frame is not None
            and o.cr_code.co_qualname == "EpisodeWorkflowV2.run"
            and o.cr_code.co_filename.endswith("episode_v2.py")]


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_a_dropped_run_closed_during_another_runs_activation_leaves_that_run_alone():
    """The CI failure, made deterministic: automatic collection is off, so the dropped run is closed
    exactly inside the other workflow's activation."""
    async with await WorkflowEnvironment.start_local() as env:
        gc.collect()
        gc.disable()
        try:
            # runs that other tests' workers still hold; kept referenced so the scan below sees only ours
            earlier = _suspended_episode_runs()
            tq = f"dropped-{uuid.uuid4().hex[:6]}"
            async with Worker(env.client, task_queue=tq, workflows=[EpisodeWorkflowV2, FakeShot],
                              activities=_acts(set())):
                h = await env.client.start_workflow(
                    EpisodeWorkflowV2.run, args=["ep05", None, True, ["A01", "A02"], 40],
                    id=f"ep05-{uuid.uuid4().hex[:6]}", task_queue=tq)
                await _wait_for(h, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            dropped = [weakref.ref(o) for o in _suspended_episode_runs() if not any(o is e for e in earlier)]
            assert len(dropped) == 1, "the stopped worker's run should be left for the garbage collector"

            ctq = f"collector-{uuid.uuid4().hex[:6]}"
            async with Worker(env.client, task_queue=ctq, workflows=[Collector]):
                ch = await env.client.start_workflow(Collector.run, id=f"collector-{uuid.uuid4().hex[:6]}",
                                                     task_queue=ctq)
                await ch.signal(Collector.collect_garbage)
                for _ in range(100):
                    if await ch.query(Collector.garbage_collected):
                        break
                    await asyncio.sleep(0.1)
                assert dropped[0]() is None, "the dropped run was not closed during the other workflow's activation"
                await ch.signal(Collector.finish)
                assert await ch.result() == "done"
                scheduled = [e.activity_task_scheduled_event_attributes.activity_type.name
                             async for e in ch.fetch_history_events()
                             if e.HasField("activity_task_scheduled_event_attributes")]
                assert scheduled == [], f"the dropped run scheduled {scheduled} in another workflow"
            await h.terminate("test done")
        finally:
            gc.enable()
