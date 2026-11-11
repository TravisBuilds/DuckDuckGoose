"""POST /api/episodes for an episode whose run is still going: a 409 that says so, not a 500.

Temporal allows one open run per workflow id (the episode id), so starting it again raised
WorkflowAlreadyStartedError out of the route (500), after the request's beatmap had been written over
the running episode's. The start is refused before anything is written while the run is open, and a
start that only Temporal can see is open gets the same 409. Run on a Temporal dev server with the
production worker (dry run), the API in front.
"""
import os
import sys
from pathlib import Path

from httpx import ASGITransport, AsyncClient
from temporalio.client import WorkflowExecutionStatus

from tests.test_story_revise_live import ADMIN_SECRET, BEATMAP, studio, wait_for


def _body(ep: str, beatmap: str | None = None) -> dict:
    return {"episode_id": ep, "beatmap_content": beatmap or BEATMAP.format(ep=ep), "dry_run": True,
            "enable_story_shaping": True}


def _refused(ep: str, run_id: str) -> str:
    return f"Episode {ep} has a run in progress (run {run_id}); it cannot be started again until that run ends"


def _api() -> AsyncClient:
    """Admin client that answers a route's unhandled error as the 500 a browser gets."""
    app = sys.modules["api.main"].app
    return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
                       headers={"Authorization": f"Bearer {ADMIN_SECRET}"})


async def _waiting_at_the_story_gate(admin, ep: str) -> str:
    res = await admin.post("/api/episodes", json=_body(ep))
    assert res.status_code == 200, res.text
    await wait_for(admin, f"/api/episodes/{ep}/story-shape", lambda b: bool(b.get("story_shape")), "shaped")
    return res.json()["run_id"]


async def test_starting_an_episode_whose_run_is_going_is_refused_with_409(monkeypatch):
    from hfvg.worker import PRODUCTION_ACTIVITIES

    ep = "ep87run"
    async with studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=False) as (env, admin):
        first = await _waiting_at_the_story_gate(admin, ep)
        beatmap = Path(os.environ["EPISODES_DIR"]) / ep / "BEATMAP.md"
        before = beatmap.read_text(encoding="utf-8")
        async with _api() as api:
            res = await api.post("/api/episodes", json=_body(ep, "# Episode ep87run Beatmap\n\nanother plan\n"))
        assert res.status_code == 409, res.text
        assert res.json()["detail"] == _refused(ep, first)
        assert beatmap.read_text(encoding="utf-8") == before
        desc = await env.client.get_workflow_handle(ep).describe()
        assert (desc.run_id, desc.status) == (first, WorkflowExecutionStatus.RUNNING)

        await env.client.get_workflow_handle(ep).terminate(reason="stopped by the operator")
        await wait_for(admin, f"/api/episodes/{ep}", lambda s: bool(s.get("run") and s["run"]["ended"]), "ended")
        res = await admin.post("/api/episodes", json=_body(ep))
        assert res.status_code == 200, res.text
        assert res.json()["run_id"] not in ("", first)


async def test_a_run_only_temporal_sees_open_is_refused_with_409_too(monkeypatch):
    """The route's check found no open run (one started in between): Temporal refuses the start."""
    from hfvg.worker import PRODUCTION_ACTIVITIES

    ep = "ep88run"
    async with studio(monkeypatch, list(PRODUCTION_ACTIVITIES), live=False) as (env, admin):
        first = await _waiting_at_the_story_gate(admin, ep)
        api_main = sys.modules["api.main"]

        async def no_open_run_yet(episode_id):
            return None

        monkeypatch.setattr(api_main, "_open_episode_run", no_open_run_yet)
        async with _api() as api:
            res = await api.post("/api/episodes", json=_body(ep))
        assert res.status_code == 409, res.text
        assert res.json()["detail"] == _refused(ep, first)
        desc = await env.client.get_workflow_handle(ep).describe()
        assert (desc.run_id, desc.status) == (first, WorkflowExecutionStatus.RUNNING)
