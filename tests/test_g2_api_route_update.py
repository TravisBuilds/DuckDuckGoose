"""
G2 approval through the real API route (POST /api/episodes/{id}/approve).

approve_g2_scene_stills is a workflow *update* with a validator that refuses while a retry
is pending. The API route must therefore call execute_update (a signal to an update handler
is silently dropped -> 200 but G2 never passes) and map a validator refusal to HTTP 409
carrying the validator message (not 200, not 500).

Runs against a real Temporal test server + the real EpisodeWorkflowV2; only workflow lookup
(list_workflows, unsupported by the time-skipping server) is shimmed.
"""
import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.studio_db import init_studio_db, upsert_episode
from tests.test_harness_pr10_mutations import (
    PendingRetryWorkflow,
    QuickRetryWorkflow,
    _make_activities,
)

ADMIN_SECRET = "a" * 32


class _LookupShim:
    """Real Temporal client, but list_workflows yields the one episode workflow id."""

    def __init__(self, real, wf_id):
        self._real = real
        self._wf_id = wf_id

    def list_workflows(self, *a, **k):
        wf_id = self._wf_id

        async def gen():
            yield SimpleNamespace(id=wf_id)

        return gen()

    def get_workflow_handle(self, wf_id, *a, **k):
        return self._real.get_workflow_handle(wf_id, *a, **k)


async def _api(tmp_path, monkeypatch, episode_id):
    db_path = str(tmp_path / "studio.db")
    await init_studio_db(db_path)
    from hfvg.budget import BudgetLedger
    await BudgetLedger(db_path).init_db()
    await upsert_episode(db_path, episode_id, beatmap_path="test.md")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    import sys
    sys.modules.pop("api.main", None)
    import api.main as api_main
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://test")
    r = await client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert r.status_code == 200, r.text
    return api_main, client


async def _wait(handle, pred, budget=8.0):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    end = time.time() + budget
    st = None
    while time.time() < end:
        try:
            st = await asyncio.wait_for(handle.query(EpisodeWorkflowV2.get_state), 3)
            if pred(st):
                return st, True
        except Exception:
            pass
        await asyncio.sleep(0.1)
    return st, False


async def _to_failed_shot_and_retry(handle):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    st, ok = await _wait(handle, lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
    assert ok, f"never reached G1: {st}"
    await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
    st, ok = await _wait(handle, lambda s: len(s.get("failed_shots", [])) > 0)
    assert ok, f"A01 never failed: {st}"
    r = await handle.execute_update(EpisodeWorkflowV2.retry_shot, args=["A01"])
    assert r["status"] == "queued", r


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_api_g2_refused_with_409_while_retry_pending(tmp_path, monkeypatch):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    ep = "ep91"
    api_main, client = await _api(tmp_path, monkeypatch, ep)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="api-g2-409",
                          workflows=[EpisodeWorkflowV2, PendingRetryWorkflow], activities=_make_activities()):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=[ep, None, True, ["A01"], 40], id=ep, task_queue="api-g2-409")
            try:
                await _to_failed_shot_and_retry(handle)
                st, ok = await _wait(handle, lambda s: len(s.get("pending_retries", [])) > 0)
                assert ok, f"retry not pending: {st}"
                api_main.temporal_client = _LookupShim(env.client, ep)
                r = await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g2_scene_stills"})
                assert r.status_code == 409, f"expected 409 for G2 with pending retry, got {r.status_code}: {r.text}"
                detail = r.json().get("detail", "")
                assert "retry is pending" in detail.lower() and "A01" in detail, detail
                st, _ = await _wait(handle, lambda s: True)
                assert "G2_SCENE_STILLS" not in st.get("passed_gates", []), st.get("passed_gates")
            finally:
                await client.aclose()
                try:
                    await handle.terminate("test done")
                except Exception:
                    pass


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_api_g2_accepted_reaches_workflow_when_no_retry_pending(tmp_path, monkeypatch):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    ep = "ep92"
    api_main, client = await _api(tmp_path, monkeypatch, ep)
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="api-g2-ok",
                          workflows=[EpisodeWorkflowV2, QuickRetryWorkflow], activities=_make_activities()):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=[ep, None, True, ["A01"], 40], id=ep, task_queue="api-g2-ok")
            try:
                await _to_failed_shot_and_retry(handle)
                st, ok = await _wait(handle, lambda s: len(s.get("pending_retries", [])) == 0)
                assert ok, f"retry never cleared: {st}"
                api_main.temporal_client = _LookupShim(env.client, ep)
                r = await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g2_scene_stills"})
                assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
                st, ok = await _wait(handle, lambda s: "G2_SCENE_STILLS" in s.get("passed_gates", []))
                assert ok, f"API returned 200 but G2 never passed in the workflow: {st and st.get('passed_gates')}"
            finally:
                await client.aclose()
                try:
                    await handle.terminate("test done")
                except Exception:
                    pass
