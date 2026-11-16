"""Shot plan edits at G1 (replan_shots): while the run waits for shot plan approval, a creator's
change to a shot's prompts or to its cast, props and place is re-parsed, checked (plan_replan),
preflighted and re-priced before it is in force, so the still submit after approval sends the new
prompt and the props' photos. Anything structural, a replan after approval and an approval during a
replan are refused, and a refusal changes nothing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment

from hfvg.plan_resync import plan_replan
from hfvg.prop_store import photo_path
from tests.cast_helpers import new_place, new_prop, prop_photo
from tests.lint_helpers import lint_ok
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, _write_episode, no_counter  # noqa: F401
from tests.test_plan_resync import NEW_CLIP, OLD_CLIP, _acts, _still_landed

CABIN_KEY = "cast:img:" + "a" * 64 + ".png"


def _shot(**kw):
    base = {"shot_id": "A01", "prompt": "p", "clip_prompt": "c", "qc_fail_if": ["x"], "end_prompt": None,
            "shot_meta": {"count": 1, "text_policy": None}, "ref_keys": ["R1"], "refs": ["/refs/r1.png"],
            "missing_refs": [], "trim": {"in": 0.0, "out": 6.0}, "tags": [], "pre_approved_still": None,
            "params": {"model": "kling", "duration": 6, "end_frame": False}}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    for var in ("MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON", "SHOTS_JSON_PATH", "REFS_JSON_PATH"):
        monkeypatch.delenv(var, raising=False)


# ---------- unit: what may change before approval ----------

def test_picks_and_prompts_are_changes():
    picked = _shot(prompt="p2", end_prompt="e2", clip_prompt="c2",
                   shot_meta={"count": 1, "text_policy": "no_text",
                              "props": [{"slug": "cabin", "kind": "place", "name": "Cabin", "clause": "Cabin."}]},
                   ref_keys=["R1", CABIN_KEY], refs=["/refs/r1.png", "/cast/cabin.png"])
    r = plan_replan([_shot()], [picked])
    assert r["problems"] == []
    assert set(r["changes"]["A01"]) == {"prompt", "end_prompt", "clip_prompt", "shot_meta", "ref_keys", "refs"}
    assert r["changes"]["A01"]["ref_keys"] == ["R1", CABIN_KEY]


def test_an_unchanged_package_changes_nothing():
    assert plan_replan([_shot()], [_shot()]) == {"changes": {}, "problems": []}


@pytest.mark.parametrize("new, word", [
    (_shot(trim={"in": 1.0, "out": 6.0}), "trim"),
    (_shot(tags=["night"]), "tags"),
    (_shot(pre_approved_still={"path": "x.png"}), "pre_approved_still"),
    (_shot(params={"model": "kling", "duration": 8, "end_frame": False}), "duration"),
    (_shot(params={"model": "kling", "duration": 6, "end_frame": False, "quality": "high"}), "quality"),
    (_shot(shot_meta={"count": 2, "text_policy": None}), "count"),
    (_shot(ref_keys=["R2"]), "refs.json"),
    (_shot(ref_keys=[CABIN_KEY]), "refs.json"),
    (_shot(prompt="   "), "empty"),
])
def test_structural_changes_and_an_empty_prompt_are_refused(new, word):
    r = plan_replan([_shot()], [new])
    assert r["problems"] and word in " ".join(r["problems"]), r


def test_a_different_shot_list_is_refused():
    r = plan_replan([_shot()], [_shot(), _shot(shot_id="A02")])
    assert r["problems"] and "A02" in r["problems"][0] and "new run" in r["problems"][0]
    assert plan_replan([_shot(), _shot(shot_id="A02")], [_shot()])["problems"]


def _refusal(validate, *args) -> str:
    """The ApplicationError text a validator refuses with ("" when it accepts)."""
    try:
        validate(*args)
    except ApplicationError as e:
        return str(e)
    return ""


def test_approval_waits_while_a_replan_is_checked():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2()
    wf.state.current_gate = "G1_SHOT_PLAN"
    assert _refusal(wf.validate_g1_approval) == ""
    assert _refusal(wf.validate_replan_shots, "new props") == ""
    wf._replanning = True
    assert "being checked" in _refusal(wf.validate_g1_approval)
    assert "another shot plan change" in _refusal(wf.validate_replan_shots, "again")
    assert wf._replan_problem("again", running=True) is None


@pytest.mark.parametrize("setup, word", [
    (lambda wf: setattr(wf.state, "current_gate", "G2_SCENE_STILLS"), "not at G1"),
    (lambda wf: setattr(wf, "approved_g1_shot_plan", True), "already approved"),
    (lambda wf: wf.passed_gates.append("G1_SHOT_PLAN"), "already approved"),
])
def test_a_replan_outside_the_open_g1_gate_is_refused(setup, word):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2()
    wf.state.current_gate = "G1_SHOT_PLAN"
    setup(wf)
    assert word in _refusal(wf.validate_replan_shots, "new props")
    assert "_replanning" in EpisodeWorkflowV2.REOPEN_SKIP_ATTRS


# ---------- integration (dry) ----------

def _rewrite(beatmap: str, **fields):
    p = Path(beatmap).with_name("shots.json")
    data = json.loads(p.read_text())
    data["shots"][0].update(fields)
    p.write_text(json.dumps(data))


async def _start(env, ep, beatmap):
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[ep, beatmap, True, None, None, None], id=ep, task_queue=TASK_QUEUE)
    return handle, await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_props_picked_at_g1_reach_the_still_submit(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            ep = "ep83"
            cabin = new_place(("episode", ep), description="a small blue wooden cabin on stilts at the end of a pier")
            umbrella = new_prop(("episode", ep), description="a red umbrella with a curved wooden handle")
            photos = [str(photo_path(prop_photo(cabin, seed=1, color=(40, 60, 220)))),
                      str(photo_path(prop_photo(umbrella, seed=2)))]
            beatmap = _write_episode(tmp_path, ep, lint_ok("A duck stands on a pier"))
            handle, st = await _start(env, ep, beatmap)
            assert "cast" not in st["g1_plan"] and st["replan_log"] == []

            new_prompt = lint_ok("A duck holds an umbrella on a pier")
            _rewrite(beatmap, prompt=new_prompt, props=[umbrella["slug"]], place=cabin["slug"])
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("replan_shots", args=["  "])
            r = await handle.execute_update("replan_shots", args=["props picked"])
            assert r["success"] and {"prompt", "shot_meta", "ref_keys", "refs"} <= set(r["changed"]["A01"]), r
            st = await handle.query("get_state")
            assert st["replan_log"] == [{"reason": "props picked", "changed": r["changed"], "preflight_errors": []}]
            assert (st["g1_plan"].get("cast") or {}).get("A01", {}).get("props") == [
                {"slug": cabin["slug"], "kind": "place", "name": "Harbour cabin"},
                {"slug": umbrella["slug"], "kind": "prop", "name": "Red umbrella"}]
            assert st["replanning"] is False

            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _still_landed(env, f"{ep}-shot-A01")
            prompt, refs, meta = h.still_calls[0][2], h.still_calls[0][4], h.still_calls[0][9]
            assert "holds an umbrella" in prompt and "stands on a pier" not in prompt, prompt
            assert [r for r in refs if r in photos] == photos, refs
            assert [p["name"] for p in meta["props"]] == ["Harbour cabin", "Red umbrella"]

            with pytest.raises(WorkflowUpdateFailedError) as ei:
                await handle.execute_update("replan_shots", args=["too late"])
            assert "not at G1 gate" in str(ei.value.cause)
            assert len((await handle.query("get_state"))["replan_log"]) == 1
            await handle.terminate("test done")


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_a_refused_replan_changes_nothing_and_model_limits_hold_approval(dry_env, tmp_path, no_counter):  # noqa: F811
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    async with await WorkflowEnvironment.start_local() as env:
        from temporalio.worker import Worker
        from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
        from hfvg.workflows.shot import ShotWorkflow
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_acts(h, clips)):
            ep = "ep84"
            beatmap = _write_episode(tmp_path, ep, lint_ok("A duck stands in snow"), clip_prompt=OLD_CLIP)
            handle, st = await _start(env, ep, beatmap)
            plan_before = st["g1_plan"]

            _rewrite(beatmap, prompt=lint_ok("A duck stands in deep snow"), duration=8)
            with pytest.raises(WorkflowUpdateFailedError) as ei:
                await handle.execute_update("replan_shots", args=["longer shot"])
            assert "duration" in str(ei.value.cause)
            st = await handle.query("get_state")
            assert st["replan_log"] == [] and st["g1_plan"] == plan_before and st["replanning"] is False

            # a change that breaks a model limit is in force, and approval waits for the fix
            _rewrite(beatmap, prompt=lint_ok("A duck stands in snow"), duration=6,
                     clip_prompt=OLD_CLIP + " The snow keeps falling." * 120)
            r = await handle.execute_update("replan_shots", args=["longer clip text"])
            assert r["changed"] == {"A01": ["clip_prompt"]} and "2500" in r["preflight_errors"][0], r
            with pytest.raises(WorkflowUpdateFailedError) as ei:
                await handle.execute_update("approve_g1_shot_plan")
            assert "break a model limit" in str(ei.value.cause)
            _rewrite(beatmap, clip_prompt=NEW_CLIP)
            r = await handle.execute_update("replan_shots", args=["shorter clip text"])
            assert r["preflight_errors"] == [] and len((await handle.query("get_state"))["replan_log"]) == 2
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await handle.terminate("test done")
