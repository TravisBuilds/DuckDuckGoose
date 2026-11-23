"""A locked cast look revised mid-run (cast_store.revise_locked_version, an admin's revise-lock) is
put in force at G2 by resync_shot_plans: the shot plans take the revised words of the locked cast
entries (nothing else may change: the characters, versions, refs and the G1 plan stay), later stills
and recomposes inject and lint against them, and every still already made with the old words waits
for a person, also one approved before (fail closed). Kept anyway, the old still goes on to its clip.
Dry run, no spend."""

from __future__ import annotations

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from hfvg import cast_store
from hfvg.cast_prompt import cast_lint_violations, inject_cast_prompt
from hfvg.temporal_converter import temporal_data_converter
from hfvg.worker import PRODUCTION_WORKFLOWS
from hfvg.workflows.episode_v2 import CAST_REVISION_RESYNC_PATCH
from tests.cast_helpers import complete_sheet, new_character
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import (
    MOVING, TASK_QUEUE, ClipFactory, _write_episode, needs_ffmpeg, no_counter,  # noqa: F401
)
from tests.test_plan_resync import _rewrite, _still_landed
from tests.test_qc_judges_workflow import _patch_markers
from tests.test_recompose_and_edit import _worker
from tests.test_retry_prompt_preflight import SECRET, client  # noqa: F401  (fixture)

WHY = "its still was made before a cast lock it shows was revised"
PIP = {"character": "pip", "version": 1, "name": "Pip", "clause": "Pip: a small round robot.", "locked": True}
MOSS = {"character": "moss", "version": 2, "name": "Moss", "clause": "Moss: a grey moth.", "locked": True}


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON", "SHOTS_JSON_PATH", "REFS_JSON_PATH"):
        monkeypatch.delenv(var, raising=False)


def _sent(call) -> tuple[str, dict]:
    """The still prompt as the submit sends it (cast words injected) and the shot_meta it lints with."""
    meta = call[9]
    return inject_cast_prompt(call[2], meta)[0], meta


# ---------- unit ----------

def test_a_resync_waits_for_pending_retries_and_recomposes():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2()
    wf.state.current_gate = "G2_SCENE_STILLS"
    assert wf._resync_problem("new look") is None
    wf.state.pending_retries["A01"] = {"attempt": 2}
    assert wf._resync_problem("new look") == "still retries pending for: A01"
    wf.state.pending_retries.clear()
    wf.pending_recompose["A01"] = {"n": 1}
    assert wf._resync_problem("new look") == "a recompose is pending for: A01"


def test_only_stills_waiting_at_g2_with_other_words_are_flagged_and_a_new_revision_flags_again():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2()
    old, new, newer = ({"count": 1, "cast": [{**PIP, "clause": c}, MOSS]}
                       for c in (PIP["clause"], "Pip: a tall robot.", "Pip: a tall robot in a red scarf."))
    wf.still_version_children = {"A01": [{"version": 1, "workflow_id": "w1", "kind": "shot"},
                                         {"version": 2, "workflow_id": "w2", "kind": "retry"},
                                         {"version": 3, "workflow_id": "w3", "kind": "retry"}]}
    wf.still_version_plans = {"w1": {"shot_meta": old}, "w2": {"shot_meta": new}, "w3": {"shot_meta": old}}
    wf.still_child_status = {"w1": "running", "w2": "running", "w3": "failed"}
    wf.cast_clause_revisions = {"pip:1": new["cast"][0]["clause"]}
    assert wf._flag_old_lock_stills(["A01"]) == {"A01": ["w1"]}
    assert wf.old_lock_stills == {"w1": {"shot_id": "A01", "version": 1, "characters": ["Pip"], "decided": False}}
    wf._still_decided("w1")
    assert wf.old_lock_stills["w1"]["decided"] is True
    wf.cast_clause_revisions["pip:1"] = newer["cast"][0]["clause"]
    assert wf._flag_old_lock_stills(["A01"]) == {"A01": ["w1", "w2"]}
    assert wf.old_lock_stills["w1"]["decided"] is False and wf.old_lock_stills["w2"]["version"] == 2
    assert "w3" not in wf.old_lock_stills
    # a recompose built on any of those plans uses the words in force; other entries stay as they are
    assert wf._revised_cast_meta(old) == newer and wf._revised_cast_meta(new) == newer
    assert wf._revised_cast_meta({"count": 1}) == {"count": 1}
    assert EpisodeWorkflowV2()._revised_cast_meta(old) is old
    assert {"old_lock_stills", "cast_clause_revisions"}.isdisjoint(EpisodeWorkflowV2.REOPEN_SKIP_ATTRS)


# ---------- API ----------

class _ResyncHandle:
    def __init__(self, result):
        self.result, self.updates = result, []

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, kw.get("args", a)))
        return self.result


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


def test_the_resync_route_says_how_many_stills_wait_because_their_look_changed(client, monkeypatch):  # noqa: F811
    api_main, c = client
    revisions = {"A01": [{"character": "pip", "version": 1, "name": "Pip", "before": "Pip: old.", "after": "Pip: new."}]}
    h = _ResyncHandle({"success": True, "action": "resync_shot_plans", "reason": "Pip's new look",
                       "changed": {"A01": ["shot_meta"], "A02": ["shot_meta"]}, "signalled": [],
                       "cast_revisions": revisions,
                       "old_lock_stills": {"A01": ["ep90-shot-A01", "ep90-shot-A01-retry1"], "A02": ["ep90-shot-A02"]}})
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep90/resync-plans", json={"reason": "Pip's new look"})
    assert r.status_code == 200, r.text
    assert r.json()["message"] == ("2 shot plan(s) re-synced (G2 itself is NOT approved); 3 still(s) made with "
                                   "the old cast words wait for your decision")
    assert r.json()["result"]["cast_revisions"] == revisions
    assert h.updates == [("resync_shot_plans", ["Pip's new look"])]
    h.result = {"success": True, "changed": {"A01": ["prompt"]}, "signalled": []}
    r = c.post("/api/episodes/ep90/resync-plans", json={"reason": "typo"})
    assert r.json()["message"] == "1 shot plan(s) re-synced (G2 itself is NOT approved)"


# ---------- integration (dry) ----------


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_a_revised_lock_resynced_at_g2_flags_old_stills_and_drives_later_stills(
        dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "character.png"
    ref.write_bytes(b"png")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            ep = "ep86"
            pip = new_character(("episode", ep))
            cid = pip["character_id"]
            complete_sheet(pip)
            old = cast_store.approve_version(cid, 1, "t")["locked_description"]
            beatmap = _write_episode(tmp_path, ep, lint_ok("Pip waits in the snow"))
            _rewrite(beatmap, cast=[pip["slug"]])
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            v1 = await _still_landed(env, f"{ep}-shot-A01")
            sent, meta = _sent(h.still_calls[0])
            assert meta["cast"] == [{"character": pip["slug"], "version": 1, "name": "Pip", "clause": old,
                                     "locked": True}]
            assert old in sent and cast_lint_violations(sent, meta) == []
            await handle.execute_update("approve_shot", args=["A01"])

            # nothing revised yet: a resync changes nothing and flags nothing
            r0 = await handle.execute_update("resync_shot_plans", args=["nothing new"])
            assert r0["changed"] == {} and "cast_revisions" not in r0, r0

            new = cast_store.revise_locked_version(
                cid, 1, description="a tall square robot with two blue eyes", outfit="a red scarf",
                reason="the director's new look", actor="admin-1", episode_id=ep)["locked_description"]
            assert new != old
            r = await handle.execute_update("resync_shot_plans", args=["Pip's new look"])
            assert r["changed"] == {"A01": ["shot_meta"]} and r["signalled"] == [], r
            assert r["cast_revisions"] == {"A01": [{"character": pip["slug"], "version": 1, "name": "Pip",
                                                    "before": old, "after": new}]}
            assert r["old_lock_stills"] == {"A01": [f"{ep}-shot-A01"]}
            st = await handle.query("get_state")
            assert st["resync_log"][-1]["cast_revisions"] == r["cast_revisions"]
            # approved before the revision, the old still still waits for a person
            assert st["shot_decisions"] == {"A01": "approved"}
            assert st["stills_needing_decision"] == {"A01": WHY}
            assert st["still_flags"]["A01"]["old_lock"] == {"shot_id": "A01", "version": 1, "characters": ["Pip"],
                                                             "decided": False}
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("approve_g2_scene_stills")
            assert WHY in str(refused.value.cause)

            # made again: the submit carries the revised words, and the prompt as sent passes lint with them
            n = len(h.still_calls)
            rr = await handle.execute_update("retry_shot", args=["A01", None])
            assert rr["status"] == "queued", rr
            await _wait(handle, lambda s: not s["pending_retries"], what="retry landed")
            await _still_landed(env, f"{ep}-shot-A01-retry1")
            sent, meta = _sent(h.still_calls[n])
            assert meta["cast"][0]["clause"] == new and new in sent and old not in sent, sent
            assert cast_lint_violations(sent, meta) == []
            # the old words alone no longer pass the lint of the revised plan
            assert cast_lint_violations(_sent(h.still_calls[0])[0], meta)
            st = await handle.query("get_state")
            assert st["stills_needing_decision"] == {} and "A01" not in st["still_flags"], st["still_flags"]

            # keeping the old still after all is the person's decision on it: it goes on to its clip
            await handle.execute_update("select_still_version", args=["A01", 1, "the old look still reads right"])
            st = await handle.query("get_state")
            assert st["stills_needing_decision"] == {}
            assert st["still_flags"]["A01"]["old_lock"]["decided"] is True
            n_clip = len(h.clip_calls)
            await handle.execute_update("approve_g2_scene_stills")
            kept = await _child_result(env, f"{ep}-shot-A01", timeout=90)
            assert kept["still_url"] == v1["still_url"] and kept.get("clip_url"), kept
            assert len(h.clip_calls) == n_clip + 1
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                        and (s["assembly"]["qc"] or {}).get("verdict"), timeout=120, what="edit 1")

            # a new composition after G2 is built on the kept still's plan, but with the words in force
            out = await handle.execute_update("recompose_shot", args=[
                "A01", "closer framing", lint_ok("Pip sits in the snow"), [str(ref)], ["character"]])
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="recompose still parked")
            sent, meta = _sent(h.still_calls[-1])
            assert meta["cast"][0]["clause"] == new and new in sent and old not in sent, sent
            assert (await handle.query("recompose_still_plans"))["A01"]["version"] == out["version"]

            # the accepted resync is recorded behind its own marker and replays as it ran
            history = await handle.fetch_history()
            taken = [p for p in _patch_markers(history) if p.startswith(f"{CAST_REVISION_RESYNC_PATCH}:")]
            assert len(taken) == 1, _patch_markers(history)
            replay = await Replayer(workflows=PRODUCTION_WORKFLOWS,
                                    data_converter=temporal_data_converter).replay_workflow(history)
            assert replay.replay_failure is None
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_until_a_resync_the_run_keeps_the_cast_words_it_planned_with(
        dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            ep = "ep87"
            pip = new_character(("episode", ep))
            cid = pip["character_id"]
            complete_sheet(pip)
            old = cast_store.approve_version(cid, 1, "t")["locked_description"]
            beatmap = _write_episode(tmp_path, ep, lint_ok("Pip waits in the snow"))
            _rewrite(beatmap, cast=[pip["slug"]])
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _still_landed(env, f"{ep}-shot-A01")

            # revised in the library only: the run keeps the words it planned with until a resync
            cast_store.revise_locked_version(cid, 1, description="a tall square robot", reason="new look",
                                             actor="admin-1", episode_id=ep)
            n = len(h.still_calls)
            await handle.execute_update("retry_shot", args=["A01", None])
            await _wait(handle, lambda s: not s["pending_retries"], what="retry landed")
            await _still_landed(env, f"{ep}-shot-A01-retry1")
            sent, meta = _sent(h.still_calls[n])
            assert meta["cast"][0]["clause"] == old and old in sent and cast_lint_violations(sent, meta) == []
            st = await handle.query("get_state")
            assert st["resync_log"] == [] and st["stills_needing_decision"] == {}
            await handle.terminate("test done")
