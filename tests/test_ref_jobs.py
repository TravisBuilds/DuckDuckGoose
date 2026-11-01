"""GAP-42 (ep05v2 r7): versioned ref-edit jobs could only run at the start of an episode (before G1).
A studio ref fix after the final cut had no harness path. generate_refs (update, parked at G_FINAL_CUT)
runs refs.json to_generate edit entries with an explicit version through generate_ref_edit_activity
(one attempt each); extra_refs add further image_urls after the source; lock_meta injects the prompt
kit's continuity locks and enforces the duck style split before any spend."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from hfvg.activities.ref_generation import ref_edit_specs_for
from hfvg.prompt_lint import PromptLintError, apply_ref_edit_locks
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, needs_ffmpeg, no_counter  # noqa: F401
from tests.test_recompose_and_edit import _to_final_cut
from tests.test_ref_edit import FakeBudget, FakeStill, _png, live_env  # noqa: F401

LOCK = "SYNTH DUCK LOCK: plush toy duck, tiny tail nub at most; NO tail feathers. Refs: duck body only."


def _entry(tmp_path, key="ab_front", version=1, **kw):
    src = tmp_path / "bare.png"
    if not src.exists():
        src.write_bytes(_png())
    e = {"key": key, "prompt": "Front view of the plush duck on a plain warm studio background",
         "output_path": str(tmp_path / "refs" / f"{key}.png"), "edit_from": str(src), "version": version}
    e.update(kw)
    return e


# ---------------------------------------------------------------- spec loader (pure)

def test_specs_in_request_order_with_defaults(tmp_path):
    doc = {"to_generate": [_entry(tmp_path, "a"), _entry(tmp_path, "b", 2, extra_refs=[str(tmp_path / "bare.png")]),
                           {"key": "other", "prompt": "p", "output_path": "x"}]}
    specs = ref_edit_specs_for(doc, ["b", "a"], True)
    assert [s["key"] for s in specs] == ["b", "a"]
    assert specs[0]["version"] == 2 and specs[0]["extra_refs"] == [str(tmp_path / "bare.png")]
    assert specs[1]["extra_refs"] is None
    assert (specs[1]["resolution"], specs[1]["quality"], specs[1]["aspect_ratio"]) == ("2k", "medium", "9:16")


@pytest.mark.parametrize("change, match", [
    ({"version": None}, "explicit integer version"),
    ({"version": 0}, "explicit integer version"),
    ({"version": True}, "explicit integer version"),
    ({"edit_from": None}, "need edit_from"),
    ({"prompt": " "}, "prompt is required"),
    ({"extra_refs": ["https://x/y.png"]}, "not URLs"),
    ({"extra_refs": "a.png"}, "list of paths"),
    ({"lock_meta": "duck"}, "lock_meta"),
])
def test_spec_refusals(tmp_path, change, match):
    e = _entry(tmp_path)
    e.update(change)
    with pytest.raises(ValueError, match=match):
        ref_edit_specs_for({"to_generate": [e]}, ["ab_front"], True)


def test_spec_unknown_dup_missing_extra_and_extra_is_output(tmp_path):
    with pytest.raises(ValueError, match="no entry"):
        ref_edit_specs_for({"to_generate": [_entry(tmp_path)]}, ["nope"], True)
    with pytest.raises(ValueError, match="two entries"):
        ref_edit_specs_for({"to_generate": [_entry(tmp_path), _entry(tmp_path)]}, ["ab_front"], True)
    e = _entry(tmp_path, extra_refs=[str(tmp_path / "missing.png")])
    with pytest.raises(ValueError, match="not on disk"):
        ref_edit_specs_for({"to_generate": [e]}, ["ab_front"], True)
    assert ref_edit_specs_for({"to_generate": [e]}, ["ab_front"], False)  # dry: allowed
    e = _entry(tmp_path)
    e["extra_refs"] = [e["output_path"]]
    with pytest.raises(ValueError, match="extra ref is the output"):
        ref_edit_specs_for({"to_generate": [e]}, ["ab_front"], False)


# ---------------------------------------------------------------- locks on ref-edit prompts

@pytest.fixture
def kit(tmp_path, monkeypatch):
    from tests.lint_helpers import SYNTHETIC_LINT_KIT
    k = {"style": "s", "lint": dict(SYNTHETIC_LINT_KIT),
         "continuity_locks": [{"id": "duck_identity", "when": "duck", "kinds": ["still", "clip"], "text": LOCK}]}
    p = tmp_path / "kit.json"
    p.write_text(json.dumps(k))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(p))
    return p


def test_ref_locks_injected_once_and_realism_refused(kit):
    out, ids = apply_ref_edit_locks("ep99", "Front view of the plush duck", {"duck": True}, live=True)
    assert ids == ["duck_identity"] and out.endswith(LOCK)
    again, ids2 = apply_ref_edit_locks("ep99", out, {"duck": True}, live=True)
    assert again == out and ids2 == []
    with pytest.raises(PromptLintError, match="realism"):
        apply_ref_edit_locks("ep99", "A photoreal duck, front view", {"duck": True}, live=True)
    # dry: warns, never raises; no lock_meta: unchanged
    apply_ref_edit_locks("ep99", "A photoreal duck, front view", {"duck": True}, live=False)
    assert apply_ref_edit_locks("ep99", "A photoreal duck", None, live=True) == ("A photoreal duck", [])


@pytest.mark.asyncio
async def test_live_edit_sends_extras_after_source_with_locks(live_env, kit, tmp_path):  # noqa: F811
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    extra = tmp_path / "turn.png"
    extra.write_bytes(_png(color=(1, 2, 3)))
    r = await generate_ref_edit_activity("ep99", "ab_side", "Side profile of the plush duck", live_env["out"], 1,
                                         live_env["src"], extra_refs=[str(extra)], lock_meta={"duck": True})
    p = FakeStill.instances[0]
    uploads = [c[1] for c in p.calls if c[0] == "upload"]
    assert uploads == [live_env["src"], str(extra)]
    submit = next(c[1] for c in p.calls if c[0] == "submit")
    assert len(submit["image_urls"]) == 2
    assert LOCK in submit["prompt"]
    # extras are part of the job identity (a different extra set never reuses the job id)
    assert r["job_id"].startswith("ref-ep99-ab_side-v1-edit")
    import hashlib
    assert not r["job_id"].endswith(hashlib.sha256(Path(live_env["src"]).read_bytes()).hexdigest()[:8])
    side = json.loads((Path(live_env["out"]).parent / ".versions" / "DUCK-cuff.json").read_text())
    assert side["extra_refs"] == [str(extra)] and side["locks"] == ["duck_identity"]


@pytest.mark.asyncio
async def test_live_edit_realism_refused_before_upload_or_estimate(live_env, kit):  # noqa: F811
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    with pytest.raises(PromptLintError):
        await generate_ref_edit_activity("ep99", "ab", "A realistic duck stands", live_env["out"], 1,
                                         live_env["src"], lock_meta={"duck": True})
    assert FakeStill.instances == [] and FakeBudget.calls == []


@pytest.mark.asyncio
async def test_live_edit_too_many_refs_refused(live_env):  # noqa: F811
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    with pytest.raises(ValueError, match="exceed"):
        await generate_ref_edit_activity("ep99", "ab", "p", live_env["out"], 1, live_env["src"],
                                         extra_refs=[live_env["src"]] * 5)
    assert FakeStill.instances == [] and FakeBudget.calls == []


# ---------------------------------------------------------------- workflow update

@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_generate_refs_update_at_final_cut(dry_env, tmp_path, no_counter):  # noqa: F811
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from tests.test_ep05_review_gaps import _escalating_review
    calls = []
    doc = {"to_generate": [_entry(tmp_path, "ab_front", 1), _entry(tmp_path, "ab_side", 3,
                                                                      extra_refs=[str(tmp_path / "bare.png")],
                                                                      lock_meta={"duck": True})]}

    @activity.defn(name="load_ref_edit_specs_activity")
    async def fake_load(episode_id, keys, is_live):
        return ref_edit_specs_for(doc, keys, is_live)

    @activity.defn(name="generate_ref_edit_activity")
    async def fake_edit(episode_id, key, prompt, out, version, edit_from, res, q, ar, extra, lock_meta):
        calls.append((key, version, extra, lock_meta, activity.info().attempt))
        if key == "ab_side":
            raise ValueError("QC: feathers")  # a failed job is recorded, never retried
        return {"status": "completed", "job_id": f"ref-{episode_id}-{key}-v{version}", "output_path": out,
                "usd_micros": 60_000, "credits_used": 1}

    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    acts = [a for a in h.activities("kling") if getattr(a, "__temporal_activity_definition").name
            not in ("save_asset_locally", "review_clip", "load_ref_edit_specs_activity", "generate_ref_edit_activity")
            ] + [clips.activity(), _escalating_review(), fake_load, fake_edit]
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=acts):
            handle = await _to_final_cut(env, h, tmp_path, "ep95")
            stills, clips_n = len(h.still_calls), len(h.clip_calls)
            for bad in ([["ab_front"], " "], [[], "r"], [["a", "a"], "r"]):
                with pytest.raises(WorkflowUpdateFailedError):
                    await handle.execute_update("generate_refs", args=bad)
            res = await handle.execute_update("generate_refs", args=[["ab_front", "ab_side"], "Studio AB fix"])
            rows = res["results"]
            assert [(r["key"], r["version"], r["status"]) for r in rows] == [
                ("ab_front", 1, "completed"), ("ab_side", 3, "failed")]
            assert "feathers" in rows[1]["error"]
            assert calls == [("ab_front", 1, None, None, 1),
                             ("ab_side", 3, [str(tmp_path / "bare.png")], {"duck": True}, 1)]  # one attempt
            state = await handle.query("get_state")
            assert [r["key"] for r in state["ref_jobs_log"]] == ["ab_front", "ab_side"]
            assert state["ref_jobs_running"] is False and state["current_gate"] == "G_FINAL_CUT"
            assert (len(h.still_calls), len(h.clip_calls)) == (stills, clips_n)
            await handle.signal("approve_g_final_cut")
            await _wait(handle, lambda s: s["approvals"]["g_final_cut"],
                        timeout=60, what="approved")
            with pytest.raises(WorkflowUpdateFailedError):
                await handle.execute_update("generate_refs", args=[["ab_front"], "late"])
