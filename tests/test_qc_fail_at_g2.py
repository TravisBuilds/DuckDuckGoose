"""A still the QC judge FAILS waits at G2 for a person (docs/QC-JUDGES.md) instead of ending the shot.

The REAL EpisodeWorkflowV2 + ShotWorkflow run with the production activities in DRY_RUN (the Round 1
harness: real studio DB, ledgers and dry-run submits); only review_still is replaced, answering a
judge's FAIL (escalate False) for the first review of the chosen shots. A person keeps the
judge-failed still (selects its version or approves the shot) and it is animated like any kept still,
makes it again (retry_shot) or rejects it. G2 approval alone never takes it to a clip, and nothing
re-rolls it on its own."""
import asyncio

import aiosqlite
import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS
from tests.test_gate_correctness_round1 import TASK_QUEUE, Recorder, _activities, _refusal, _until, _wait, _write_episode
from tests.test_qc_judges_workflow import _patch_markers

JUDGE_FAIL = "QC judge: two ducks in frame, the prompt has one"


def _name(a):
    return getattr(a, "__temporal_activity_definition").name


class Judge:
    """review_still stand-in: FAILS the first review of each shot in ``fail`` (escalate False, as a
    judge's FAIL is) and passes every other review. ``hold`` holds a shot's first review until set."""

    def __init__(self, fail=(), hold=None):
        self.fail = set(fail)
        self.hold = dict(hold or {})
        self.reviews: list[tuple[str, str]] = []

    def activity(self):
        @activity.defn(name="review_still")
        async def review_still(asset_url, rubric, episode_id=None, shot_id=None):
            first = shot_id not in {s for s, _ in self.reviews}
            self.reviews.append((shot_id, asset_url))
            if first and shot_id in self.hold:
                await asyncio.wait_for(self.hold[shot_id].wait(), timeout=60)
            if first and shot_id in self.fail:
                return {"passed": False, "issues": [JUDGE_FAIL], "escalate": False}
            return {"passed": True, "issues": []}

        return review_still


@pytest.fixture
async def studio(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db

    db_path = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    await CreditLedger(db_path).init_db()
    return db_path


def _worker(env, rec, judge, extra=()):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    replaced = {"review_still", *(_name(a) for a in extra)}
    acts = [a for a in _activities(rec) if _name(a) not in replaced]
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=[*acts, judge.activity(), *extra])


async def _to_g2(env, tmp_path, episode_id):
    beatmap = _write_episode(tmp_path, episode_id)
    handle = await env.client.start_workflow("EpisodeWorkflowV2", args=[episode_id, beatmap, True, None, None, None],
                                             id=episode_id, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    return handle


async def _rows(db_path, episode_id) -> dict[str, tuple]:
    """shot_id -> (status, still_url, rejection_reason)"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT shot_id, status, still_url, rejection_reason FROM shots "
                              "WHERE episode_id = ? ORDER BY shot_id", (episode_id,)) as cur:
            return {r[0]: tuple(r[1:]) for r in await cur.fetchall()}


async def _until_statuses(db_path, episode_id, want: dict[str, str], timeout=30.0):
    loop = asyncio.get_running_loop()
    end, rows = loop.time() + timeout, {}
    while loop.time() < end:
        rows = await _rows(db_path, episode_id)
        if all(rows.get(s, (None,))[0] == status for s, status in want.items()):
            return rows
        await asyncio.sleep(0.05)
    raise AssertionError(f"rows never reached {want}: {rows}")


async def _parked(handle, shot_id):
    return await _wait(handle, lambda s: shot_id in (s.get("stills_needing_decision") or {}),
                       what=f"{shot_id} waiting at G2 for a decision on its judge-failed still")


async def _result(env, workflow_id):
    return await asyncio.wait_for(env.client.get_workflow_handle(workflow_id).result(), timeout=30)


async def _finish(handle):
    await handle.signal("approve_g_final_cut")
    await asyncio.wait_for(handle.result(), timeout=30)


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_a_judge_failed_still_waits_at_g2_and_is_kept_by_selecting_it(studio, tmp_path):
    """A01's still fails the judge: the shot does not fail. It waits at G2 marked judge-failed with the
    judge's reason (DB row qc_failed, still kept), nothing re-rolls it, and G2 approval is refused until
    a person decides. Selecting the version keeps it: G2 is approved and that very still is animated.
    The histories carry the patch markers and replay on this code."""
    rec, judge = Recorder(), Judge(fail={"A01"})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, rec, judge):
            handle = await _to_g2(env, tmp_path, "ep41")
            st = await _parked(handle, "A01")
            rows = await _until_statuses(studio, "ep41", {"A01": "qc_failed", "A02": "still_complete"})
            assert rows["A01"][1], f"the judge-failed still is kept for the person: {rows}"
            assert st["failed_shots"] == [], st["failed_shots"]
            flag = st["still_flags"]["A01"]["qc_failed"]
            assert (flag["reason"], flag["decided"], flag["version"]) == (JUDGE_FAIL, False, 1), flag
            assert "A02" not in st["still_flags"]
            child = await env.client.get_workflow_handle("ep41-shot-A01").query("get_state")
            assert child["qc_failed"]["reason"] == JUDGE_FAIL and child["still_status"] == "done", child
            assert JUDGE_FAIL in child["still_error"] and child["qc_fail_kept"] is False, child
            versions = await handle.query("still_versions")
            assert [v["flags"] for v in versions["A01"]["versions"]] == [["qc_failed"]], versions
            assert [v["flags"] for v in versions["A02"]["versions"]] == [[]], versions

            refusal = await _refusal(handle.execute_update("approve_g2_scene_stills"))
            assert refusal is not None and "A01" in refusal and "QC judge" in refusal, refusal
            await asyncio.sleep(1.0)
            assert rec.clip_calls == [], "nothing is animated before G2"
            assert sorted(c[1] for c in rec.still_calls) == ["A01", "A02"], "a judge FAIL is never re-rolled on its own"

            await handle.execute_update("select_still_version", args=["A01", 1, "the second duck is a reflection"])
            st = await handle.query("get_state")
            assert st["stills_needing_decision"] == {} and st["still_flags"]["A01"]["qc_failed"]["decided"] is True
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")

            a01 = await _result(env, "ep41-shot-A01")
            assert a01["status"] == "completed", a01
            assert [c[2] for c in rec.clips_for("A01")] == [child["still_url"]], "the kept still itself is animated"
            assert len(rec.clips_for("A02")) == 1
            rows = await _rows(studio, "ep41")
            assert {s: r[0] for s, r in rows.items()} == {"A01": "completed", "A02": "completed"}, rows

            child_history = await env.client.get_workflow_handle("ep41-shot-A01").fetch_history()
            assert "qc-fail-parks-at-g2-v1" in _patch_markers(child_history)
            episode_history = await handle.fetch_history()
            assert {"g2-keep-qc-failed-still-v1", "g2-approve-kept-rows-v1"} <= set(_patch_markers(episode_history))
            await _finish(handle)
            for history in (child_history, await handle.fetch_history()):
                replay = await Replayer(workflows=PRODUCTION_WORKFLOWS,
                                        data_converter=temporal_data_converter).replay_workflow(history)
                assert replay.replay_failure is None


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_a_kept_judge_failed_still_flows_to_the_clip_run_and_g2_approves_the_rows_people_kept(studio, tmp_path):
    """A01's judge-failed still is kept by approving the shot: G2 animates it. A02 was rejected and
    then approved, and its row still says rejected (as on a run whose approve wrote nothing to the
    DB): G2 approval marks both rows approved and clears the stale rejection reason."""
    from hfvg.studio_db import approve_g2_shots as real_approve

    rec, judge = Recorder(), Judge(fail={"A01"})
    after_g2: dict = {}

    @activity.defn(name="approve_g2_shots")
    async def approve(*args):
        after_g2["args"] = args
        after_g2["count"] = await real_approve(*args)
        after_g2["rows"] = await _rows(studio, args[0])
        return after_g2["count"]

    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, rec, judge, extra=[approve]):
            handle = await _to_g2(env, tmp_path, "ep42")
            await _parked(handle, "A01")
            await _until_statuses(studio, "ep42", {"A01": "qc_failed", "A02": "still_complete"})
            await handle.execute_update("reject_shot", args=["A02", "goose off-model"])
            async with aiosqlite.connect(studio) as db:  # as the reject route records it
                await db.execute("UPDATE shots SET status = 'rejected', rejection_reason = 'goose off-model' "
                                 "WHERE episode_id = 'ep42' AND shot_id = 'A02'")
                await db.commit()
            await handle.execute_update("approve_shot", args=["A02"])
            await handle.execute_update("approve_shot", args=["A01"])
            assert (await handle.query("get_state"))["stills_needing_decision"] == {}
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")

            assert after_g2["args"] == ("ep42", [], ["A01", "A02"]), after_g2["args"]
            assert {s: (r[0], r[2]) for s, r in after_g2["rows"].items()} == {
                "A01": ("approved", None), "A02": ("approved", None)}, after_g2["rows"]
            assert after_g2["count"] == 2
            for shot_id in ("A01", "A02"):
                result = await _result(env, f"ep42-shot-{shot_id}")
                assert result["status"] == "completed", result
                assert len(rec.clips_for(shot_id)) == 1, rec.clip_calls
            assert {s: (r[0], r[2]) for s, r in (await _rows(studio, "ep42")).items()} == {
                "A01": ("completed", None), "A02": ("completed", None)}
            await _finish(handle)


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_without_a_decision_to_keep_it_a_judge_failed_still_is_never_animated(studio, tmp_path):
    """stills_approved reaching the waiting child with no decision to keep its still (the parent never
    sends it so: G2 is refused first; here it is sent straight to the child) fails the shot with the
    judge's reason, as a judge FAIL always did: no clip is submitted for it."""
    rec, judge = Recorder(), Judge(fail={"A01"})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, rec, judge):
            handle = await _to_g2(env, tmp_path, "ep43")
            await _parked(handle, "A01")
            await env.client.get_workflow_handle("ep43-shot-A01").signal("stills_approved")
            a01 = await _result(env, "ep43-shot-A01")
            assert a01["status"] == "failed" and JUDGE_FAIL in a01["error"], a01
            st = await _wait(handle, lambda s: s.get("failed_shots"), what="A01 failed")
            assert st["stills_needing_decision"] == {}, "an ended child has nothing waiting at G2"
            await _until_statuses(studio, "ep43", {"A02": "still_complete"})
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            assert rec.clips_for("A01") == [] and len(rec.clips_for("A02")) == 1, rec.clip_calls
            assert (await _rows(studio, "ep43"))["A01"][0] != "approved"
            await _finish(handle)


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_a_decision_made_before_the_judges_verdict_does_not_keep_the_still(studio, tmp_path):
    """The person approves A01 while its review still runs; then the judge FAILS it. That approval was
    not a decision on the judge-failed still: G2 is refused until the person decides again."""
    hold = asyncio.Event()
    rec, judge = Recorder(), Judge(fail={"A01"}, hold={"A01": hold})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, rec, judge):
            handle = await _to_g2(env, tmp_path, "ep44")
            await _until(lambda: any(s == "A01" for s, _ in judge.reviews), what="A01 under review")
            await handle.execute_update("approve_shot", args=["A01"])
            hold.set()
            await _parked(handle, "A01")
            await _until_statuses(studio, "ep44", {"A01": "qc_failed", "A02": "still_complete"})
            refusal = await _refusal(handle.execute_update("approve_g2_scene_stills"))
            assert refusal is not None and "A01" in refusal, refusal
            await handle.execute_update("approve_shot", args=["A01"])
            assert (await handle.query("get_state"))["stills_needing_decision"] == {}
            await handle.execute_update("approve_g2_scene_stills")
            assert (await _result(env, "ep44-shot-A01"))["status"] == "completed"
            assert len(rec.clips_for("A01")) == 1
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            await _finish(handle)


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_a_judge_failed_still_can_be_made_again_or_rejected(studio, tmp_path):
    """Both stills fail the judge. A01 is made again (retry_shot): the new still passes and is kept,
    the judge-failed one ends superseded at G2. A02 is rejected: it ends rejected, never animated."""
    rec, judge = Recorder(), Judge(fail={"A01", "A02"})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, rec, judge):
            handle = await _to_g2(env, tmp_path, "ep45")
            await _parked(handle, "A01")
            await _parked(handle, "A02")
            await handle.execute_update("retry_shot", args=["A01"])
            st = await _wait(handle, lambda s: not s.get("pending_retries") and "A01" not in s["still_flags"],
                             what="A01's new still reviewed")
            assert st["stills_needing_decision"] == {"A02": "the QC judge failed its still"}, st
            await handle.execute_update("reject_shot", args=["A02", "two ducks"])
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            assert (await _result(env, "ep45-shot-A01"))["status"] == "superseded"
            assert (await _result(env, "ep45-shot-A01-retry1"))["status"] == "completed"
            assert (await _result(env, "ep45-shot-A02"))["status"] == "rejected"
            assert len(rec.clips_for("A01")) == 1 and rec.clips_for("A02") == [], rec.clip_calls
            await _finish(handle)
