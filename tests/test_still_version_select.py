"""GAP-26 (ep05v2 2026-10-27): a G2 re-roll came out WORSE than the version before it (V23 v3: paws
above the water) and the reviewer could only keep the latest still: the parent tracked one handle per
shot. select_still_version(shot, version, reason) keeps an earlier still version: that version's child
gets stills_approved and animates ITS still (clip launch, end frame, DB row, manifest outcome, ledger
job ids); every other still child of the shot gets still_superseded at G2 and ends without any write.
Also: a still child parked in human review no longer dies on the 24 h review timer."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)


def _acts(h, clips, extra=()):
    names = {getattr(a, "__temporal_activity_definition").name for a in extra}
    base = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name
            not in ({"save_asset_locally"} | names)]
    return base + [clips.activity(), *extra]


def _escalating_still_review():
    @activity.defn(name="review_still")
    async def review(asset_url, rubric, episode_id=None, shot_id=None):
        return {"passed": False, "escalate": True, "issues": ["human review required"]}
    return review


async def _to_g2_with_retry(env, h, tmp_path, ep):
    beatmap = _write_episode(tmp_path, ep, lint_ok("A duck waddles through the snow"))
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _wait(handle, lambda s: True)
    for _ in range(200):
        st = await env.client.get_workflow_handle(f"{ep}-shot-A01").query("get_state")
        if st.get("still_url"):
            break
        await asyncio.sleep(0.05)
    r = await handle.execute_update("retry_shot", args=["A01", "warmer light"])
    assert r["status"] == "queued", r
    await _wait(handle, lambda s: not s["pending_retries"], what="retry landed")
    for _ in range(200):
        st2 = await env.client.get_workflow_handle(f"{ep}-shot-A01-retry1").query("get_state")
        if st2.get("still_url"):
            break
        await asyncio.sleep(0.05)
    return handle, st["still_url"], st2["still_url"]


def _db_row(db_path, ep, shot="A01"):
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT status, still_url, clip_url FROM shots WHERE id=?", (f"{ep}-{shot}",)).fetchone()


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_reviewer_keeps_earlier_still_version_for_clip_manifest_and_db(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            handle, v1_url, v2_url = await _to_g2_with_retry(env, h, tmp_path, "ep94")
            assert v1_url and v2_url and v1_url != v2_url
            versions = await handle.query("still_versions")
            assert versions["A01"]["current_version"] == 2
            assert [r["version"] for r in versions["A01"]["versions"]] == [1, 2]
            plan = await handle.query("still_version_plan", args=["A01", 1])
            assert plan["workflow_id"] == "ep94-shot-A01" and plan["status"] == "running"

            r = await handle.execute_update("select_still_version", args=["A01", 1, "v2 is worse"])
            assert r["version"] == 1 and r["previous_version"] == 2 and r["workflow_id"] == "ep94-shot-A01"
            st = await handle.query("get_state")
            assert st["shot_workflow_ids"]["A01"] == "ep94-shot-A01"
            assert st["current_still_versions"]["A01"] == 1 and st["still_selections"]["A01"]["version"] == 1
            clips_before = len(h.clip_calls)
            await handle.execute_update("approve_g2_scene_stills")

            kept = await _child_result(env, "ep94-shot-A01", timeout=90)
            dropped = await _child_result(env, "ep94-shot-A01-retry1", timeout=90)
            assert dropped["status"] == "superseded" and dropped["version"] == 2, dropped
            assert kept["still_url"] == v1_url, kept
            new_clips = h.clip_calls[clips_before:]
            assert len(new_clips) == 1, new_clips  # ONE clip: the kept version's; the dropped one never animates
            assert new_clips[0][2] == v1_url and new_clips[0][5] == 1, new_clips[0][:6]
            state = await _wait(handle, lambda s: (s["shot_outcomes"].get("A01") or {}).get("clip_url"),
                                timeout=90, what="outcome")
            assert state["shot_outcomes"]["A01"]["still_url"] == v1_url  # manifest / assembly source
            row = _db_row(dry_env, "ep94")
            assert row[1] == v1_url and row[2], row
            sv = await handle.query("still_versions")
            assert {r["version"]: r["status"] for r in sv["A01"]["versions"]} == {1: "completed", 2: "superseded"}


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_without_selection_older_versions_end_superseded_and_latest_is_animated(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            handle, v1_url, v2_url = await _to_g2_with_retry(env, h, tmp_path, "ep95")
            await handle.execute_update("approve_g2_scene_stills")
            old = await _child_result(env, "ep95-shot-A01", timeout=90)
            new = await _child_result(env, "ep95-shot-A01-retry1", timeout=90)
            assert old["status"] == "superseded" and new["still_url"] == v2_url
            assert [c[2] for c in h.clip_calls] == [v2_url]
            state = await _wait(handle, lambda s: (s["shot_outcomes"].get("A01") or {}).get("clip_url"),
                                timeout=90, what="outcome")
            assert state["shot_outcomes"]["A01"]["still_url"] == v2_url


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_select_refusals(dry_env, tmp_path, no_counter, monkeypatch):  # noqa: F811
    from hfvg.config import config
    monkeypatch.setattr(config, "MAX_SHOT_RETRIES", 2)
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            handle, _, _ = await _to_g2_with_retry(env, h, tmp_path, "ep96")
            for args, needle in [(["A01", 3, "x"], "unknown still version v3"),
                                 (["A01", 1, "  "], "reason is required"),
                                 (["ZZ9", 1, "x"], "unknown shot"),
                                 (["A01", 0, "x"], "positive integer")]:
                with pytest.raises(WorkflowUpdateFailedError) as e:
                    await handle.execute_update("select_still_version", args=args)
                assert needle in str(e.value.cause), (args, e.value.cause)
            # a retry pending for the shot refuses, and a new re-roll clears an earlier selection
            await handle.execute_update("select_still_version", args=["A01", 1, "keep v1"])
            await handle.execute_update("retry_shot", args=["A01", "again"])
            with pytest.raises(WorkflowUpdateFailedError) as e:
                await handle.execute_update("select_still_version", args=["A01", 1, "x"])
            assert "retry is pending" in str(e.value.cause)
            st = await _wait(handle, lambda s: not s["pending_retries"], what="retry 2 landed")
            assert st["still_selections"] == {} and st["current_still_versions"]["A01"] == 3
            await handle.execute_update("select_still_version", args=["A01", 2, "v2 best"])
            await handle.execute_update("approve_g2_scene_stills")
            with pytest.raises(WorkflowUpdateFailedError) as e:
                await handle.execute_update("select_still_version", args=["A01", 1, "late"])
            assert "G2" in str(e.value.cause)
            for wid in ("ep96-shot-A01", "ep96-shot-A01-retry2"):
                assert (await _child_result(env, wid, timeout=90))["status"] == "superseded"
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_parked_review_survives_24h_and_superseded_child_writes_nothing(dry_env, tmp_path, no_counter):  # noqa: F811
    """Live-style review (review_still escalates): children park in the human-review wait. Past the
    24 h timer they keep waiting (they used to fail the workflow task forever), and a superseded one
    ends WITHOUT the still_complete DB write that used to overwrite the kept version's row."""
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_time_skipping() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips, [_escalating_still_review()])):
            handle, v1_url, v2_url = await _to_g2_with_retry(env, h, tmp_path, "ep97")
            await env.sleep(timedelta(hours=30))
            for wid in ("ep97-shot-A01", "ep97-shot-A01-retry1"):
                desc = await env.client.get_workflow_handle(wid).describe()
                assert desc.status.name == "RUNNING", (wid, desc.status)
                assert (await env.client.get_workflow_handle(wid).query("get_state"))["still_status"] == "done"
            await handle.execute_update("select_still_version", args=["A01", 1, "v2 worse"])
            await handle.execute_update("approve_g2_scene_stills")
            dropped = await _child_result(env, "ep97-shot-A01-retry1", timeout=60)
            assert dropped["status"] == "superseded"
            kept_state = None
            for _ in range(400):
                kept_state = await env.client.get_workflow_handle("ep97-shot-A01").query("get_state")
                if kept_state.get("human_approved_still"):
                    break
                await asyncio.sleep(0.05)
            assert kept_state["human_approved_still"], kept_state
            for _ in range(200):
                row = _db_row(dry_env, "ep97")
                if row and row[1] == v1_url:
                    break
                await asyncio.sleep(0.05)
            assert row[1] == v1_url, row  # the superseded v2 never overwrote the kept row
            await handle.terminate("test done")


# ---------------------------------------------------------------- shared validation (API/CLI)

def _plan(**kw):
    base = {"workflow_id": "ep-shot-V23-retry1", "status": "running", "version": 2,
            "prompt": "Three animals soak shoulder-deep in the onsen", "refs": [], "missing_refs": [],
            "shot_meta": None, "params": {"end_frame": False}}
    base.update(kw)
    return base


def _child(**kw):
    base = {"version": 2, "still_status": "done", "still_url": "https://x/v2.jpg", "still_rejected": None,
            "still_superseded": None, "end_still_url": None}
    base.update(kw)
    return base


def _out(tmp_path, *names, ep="ep"):
    d = tmp_path / "episodes" / ep / "outputs" / "V23"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(b"\xff\xd8jpeg")
    return d


def _problems(tmp_path, plan=None, child=None, live=False, version=2):
    from hfvg.still_selection import selection_problems
    return selection_problems(episode_id="ep", shot_id="V23", version=version,
                              plan=_plan() if plan is None else plan, child_state=_child() if child is None else child,
                              live=live, data_dir=tmp_path)


def test_selection_ok_when_file_on_disk_and_preflight_clean(tmp_path):
    _out(tmp_path, "still_v2.jpg")
    assert _problems(tmp_path) == []


@pytest.mark.parametrize("plan, child, files, needle", [
    ({}, {}, [], "still v2 not on disk"),
    ({"params": {"end_frame": True}}, {"end_still_url": "u"}, ["still_v2.jpg"], "end frame of v2 not on disk"),
    ({"params": {"end_frame": True}}, {}, ["still_v2.jpg", "still_v2_end.jpg"], "_end still did not land"),
    ({"status": "completed"}, {}, ["still_v2.jpg"], "already ended"),
    ({}, {"still_status": "failed"}, ["still_v2.jpg"], "no landed still"),
    ({}, {"still_rejected": "bad"}, ["still_v2.jpg"], "was rejected"),
    ({}, {"version": 3}, ["still_v2.jpg"], "holds v3"),
    ({"prompt": "short"}, {}, ["still_v2.jpg"], "precheck: Prompt too short"),
    ({"prompt": "an explicit scene here"}, {}, ["still_v2.jpg"], "banned content"),
    ({"missing_refs": ["duck_cuff"]}, {}, ["still_v2.jpg"], "planned refs not on disk"),
    ({"refs": ["/nonexistent/ref.png"]}, {}, ["still_v2.jpg"], "ref not on disk"),
])
def test_selection_refusals(tmp_path, plan, child, files, needle):
    _out(tmp_path, *files)
    probs = _problems(tmp_path, _plan(**plan), _child(**child))
    assert any(needle in p for p in probs), probs


def test_selection_unknown_version_and_bad_input(tmp_path):
    from hfvg.still_selection import selection_problems
    assert "no still v9" in selection_problems(episode_id="ep", shot_id="V23", version=9, plan=None,
                                               child_state=None, live=False, data_dir=tmp_path)[0]
    assert "positive integer" in _problems(tmp_path, version=True)[0]


def test_selection_live_lints_that_versions_prompt(tmp_path, monkeypatch):
    from tests.test_picture_assembly_round3 import _write_episode as _we  # noqa: F401
    _out(tmp_path, "still_v2.jpg")
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    # live without a lint kit fails closed
    assert any("shot_meta is required" in p for p in _problems(tmp_path, live=True))
    assert any("No prompt lint kit" in p for p in _problems(tmp_path, _plan(shot_meta={"count": 3}), live=True))
    assert _problems(tmp_path, _plan(shot_meta={"count": 3})) == []  # dry: warn only, like the submit
    kit = tmp_path / "kit.json"
    kit.write_text(json.dumps({"lint": {"count_template": "exactly {n} animals",
                                        "realtime_pattern": "real[- ]time", "locked_camera_clause": "locked camera"}}))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(kit))
    probs = _problems(tmp_path, _plan(shot_meta={"count": 3, "characters": ["R", "H", "M"]}), live=True)
    assert any(p.startswith("lint:") for p in probs), probs


# ---------------------------------------------------------------- API route

class _FakeHandle:
    def __init__(self, plan, child, update_result=None):
        self.plan, self.child, self.updates = plan, child, []
        self.update_result = update_result or {"success": True, "previous_version": 3}

    async def query(self, name, args=None):
        return self.plan if name == "still_version_plan" else self.child

    async def execute_update(self, name, args=None):
        self.updates.append((name, args))
        return self.update_result


class _FakeClient:
    def __init__(self, handle):
        self.handle = handle

    def get_workflow_handle(self, wid):
        return self.handle


@pytest.fixture
async def api(tmp_path, monkeypatch):
    import httpx
    from tests.test_round5_guards import ADMIN_SECRET, _login
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "episodes"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    import sys
    sys.modules.pop("api.main", None)
    import api.main as api_main
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(tmp_path / "studio.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://test")
    await _login(client)
    yield api_main, client
    await client.aclose()


@pytest.mark.asyncio
async def test_api_select_still_checks_disk_then_runs_update_and_audits(api, tmp_path):
    api_main, client = api
    plan = _plan()
    out = lambda *n: _out(tmp_path, *n, ep="ep42")
    plan["workflow_id"] = "ep-shot-V23-retry1"
    handle = _FakeHandle(plan, _child())
    api_main.temporal_client = _FakeClient(handle)
    r = await client.post("/api/episodes/ep42/shots/V23/select-still", json={"version": 2, "reason": "v3 paws up"})
    assert r.status_code == 409 and "not on disk" in r.json()["detail"], r.text
    assert handle.updates == []
    out("still_v2.jpg")
    r = await client.post("/api/episodes/ep42/shots/V23/select-still", json={"version": 2, "reason": "v3 paws up"})
    assert r.status_code == 200, r.text
    assert handle.updates == [("select_still_version", ["V23", 2, "v3 paws up"])]
    with sqlite3.connect(tmp_path / "studio.db") as c:
        rows = c.execute("SELECT action, details FROM audit_log").fetchall()
    assert rows and rows[-1][0] == "select_still_version" and "v2" in rows[-1][1] and "v3" in rows[-1][1]
    r = await client.post("/api/episodes/ep42/shots/V23/select-still", json={"version": 2, "reason": " "})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_api_select_still_maps_workflow_refusal_to_409(api, tmp_path):
    from temporalio.exceptions import ApplicationError
    api_main, client = api
    _out(tmp_path, "still_v2.jpg", ep="ep42")

    class Refusing(_FakeHandle):
        async def execute_update(self, name, args=None):
            raise ApplicationError("Cannot select V23 v2: G2 already approved", non_retryable=True)
    api_main.temporal_client = _FakeClient(Refusing(_plan(), _child()))
    r = await client.post("/api/episodes/ep42/shots/V23/select-still", json={"version": 2, "reason": "x"})
    assert r.status_code == 409 and "G2 already approved" in r.json()["detail"]


def test_cli_select_still_registered_and_main_guard_at_end():
    from click.testing import CliRunner
    from hfvg.cli import cli
    res = CliRunner().invoke(cli, ["select-still", "--help"])
    assert res.exit_code == 0 and "VERSION" in res.output and "--reason" in res.output
    src = (Path(__file__).parent.parent / "hfvg" / "cli.py").read_text()
    assert src.rstrip().endswith('cli()') and src.count('if __name__ == "__main__":') == 1
