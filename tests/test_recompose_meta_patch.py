"""GAP-32 (ep05v2 G3 round 2, 2026-10-27): Studio asked for V19 re-shot as a NEW composition with a
different cast in frame (one character alone -> two side by side). recompose_shot could change prompt + refs but not
the shot's declared cast, so the prompt lint (count clause), the headcount check and clip QC all checked
the OLD count and refused / mis-checked the new still. recompose_shot (and the API route) now take an
optional shot_meta patch limited to count / characters / duck; the child, lint and edit QC use the
merged meta. GAP-32b: recompose stills are not G2 still versions, so /g2-headcount never checked them;
the recompose_still_plans query + GET /recompose-headcount run the same headcount check on them.
No network, no spend."""
import json

import pytest

from tests.lint_helpers import lint_ok
from tests.test_retry_prompt_preflight import SECRET, client  # noqa: F401  (fixture)


# ---------- pure validation ----------

def test_meta_patch_validation():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2 as W
    assert W.recompose_meta_problem(None) is None
    assert W.recompose_meta_problem({"count": 2, "characters": ["H", "M"]}) is None
    assert W.recompose_meta_problem({"duck": False}) is None
    assert "non-empty" in W.recompose_meta_problem({})
    assert "may only change" in W.recompose_meta_problem({"count": 2, "water": False})
    assert "count" in W.recompose_meta_problem({"count": "2"})
    assert "count" in W.recompose_meta_problem({"count": True})
    assert "count" in W.recompose_meta_problem({"count": -1})
    assert "characters" in W.recompose_meta_problem({"characters": "HM"})
    assert "characters" in W.recompose_meta_problem({"characters": ["H", " "]})
    assert "duck" in W.recompose_meta_problem({"duck": 1})


# ---------- API route ----------

class _Handle:
    def __init__(self, meta, plans=None):
        self.meta, self.plans, self.updates = meta, plans or {}, []

    async def query(self, name, *args, **kw):
        if name == "recompose_still_plans":
            return self.plans
        assert name == "retry_prompt_plan"
        return {"found": True, "still_prompt": "x", "clip_prompt": "x", "clip_model": "kling",
                "shot_meta": self.meta}

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, kw.get("args", a)))
        return {"status": "queued", "workflow_id": "ep77-shot-A01-recompose1", "recompose": 1, "version": 2}


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


@pytest.fixture
def episode(tmp_path, monkeypatch):
    import tests.lint_helpers as lh
    ep = tmp_path / "eps" / "ep77"
    (ep / "refs").mkdir(parents=True)
    (ep / "refs" / "rowan.png").write_bytes(b"\x89PNG")
    (ep / "prompt_kit.json").write_text(json.dumps({"lint": lh.SYNTHETIC_LINT_KIT}))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    return tmp_path


META = {"count": 1, "exterior": False, "water": False, "characters": ["M"]}


def test_route_lints_against_the_patched_cast(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(dict(META))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    two = lint_ok("Low close-up of two animals", count=2)
    body = {"reason": "Studio", "prompt": two, "ref_keys": ["rowan"]}
    # without the patch the count clause does not match the declared cast: refused, nothing queued
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 409 and "Prompt lint refused" in r.json()["detail"], r.text
    assert h.updates == []
    # with it the prompt passes and the patch reaches the workflow update as its 7th argument
    body["shot_meta"] = {"count": 2, "characters": ["H", "M"]}
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 200, r.text
    name, args = h.updates[-1]
    assert name == "recompose_shot" and args[6] == {"count": 2, "characters": ["H", "M"]}, args
    # the patched cast is enforced, not just accepted: a 1-animal prompt is now refused
    body["prompt"] = lint_ok("Low close-up of one animal", count=1)
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json=body)
    assert r.status_code == 409 and len(h.updates) == 1, r.text


def test_route_without_patch_keeps_six_args(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(dict(META))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep77/shots/A01/recompose",
               json={"reason": "S", "prompt": lint_ok("One animal"), "ref_keys": ["rowan"]})
    assert r.status_code == 200, r.text
    assert len(h.updates[-1][1]) == 6  # replay-compatible with pre-GAP-32 histories


def test_route_refuses_bad_patch_before_anything(client, episode, monkeypatch):  # noqa: F811
    api_main, c = client
    h = _Handle(dict(META))
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    r = c.post("/api/episodes/ep77/shots/A01/recompose",
               json={"reason": "S", "prompt": lint_ok("x", count=2), "ref_keys": ["rowan"],
                     "shot_meta": {"count": 2, "water": True}})
    assert r.status_code == 422 and "may only change" in r.json()["detail"] and h.updates == []


def test_recompose_headcount_checks_recompose_still_against_its_cast(client, episode, monkeypatch):  # noqa: F811
    from hfvg.qc.g2_headcount import record_headcount
    api_main, c = client
    out = episode / "data" / "episodes" / "ep77" / "outputs" / "A01"
    out.mkdir(parents=True)
    (out / "still_v5.jpg").write_bytes(b"jpg")
    plans = {"A01": {"version": 5, "n": 1, "shot_meta": {"count": 2, "characters": ["H", "M"], "duck": False},
                     "still_ready": True, "decision": None, "finished": False}}
    monkeypatch.setattr(api_main, "temporal_client", _Client(_Handle(dict(META), plans)))
    monkeypatch.setattr("hfvg.qc.animal_count.get_animal_counter", lambda: None)
    r = c.get("/api/episodes/ep77/recompose-headcount")
    assert r.status_code == 200 and r.json()["shots"]["A01"]["status"] == "NEEDS_REVIEW", r.text
    record_headcount("ep77", "A01", 5, {"cast": 1, "duck": 0, "other": 0}, by="t",
                     episodes_dir=str(episode / "eps"))
    r = c.get("/api/episodes/ep77/recompose-headcount").json()
    assert r["status"] == "FAIL" and r["failing"] == ["A01"], r  # 1 counted vs the NEW cast of 2
    record_headcount("ep77", "A01", 5, {"cast": 2, "duck": 0, "other": 0}, by="t",
                     episodes_dir=str(episode / "eps"))
    r = c.get("/api/episodes/ep77/recompose-headcount").json()
    assert r["status"] == "PASS" and r["shots"]["A01"]["version"] == 5, r


# ---------- workflow ----------

from tests.test_recompose_and_edit import _to_final_cut, _worker  # noqa: E402
from tests.test_moderation_recovery import Harness, _child_result, _wait, dry_env  # noqa: E402,F401
from tests.test_picture_assembly_round3 import MOVING, ClipFactory, needs_ffmpeg, no_counter  # noqa: E402,F401


@needs_ffmpeg
@pytest.mark.asyncio
@pytest.mark.timeout(400)
async def test_recompose_with_new_cast_runs_child_on_patched_meta(dry_env, tmp_path, no_counter):
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.testing import WorkflowEnvironment
    h = Harness({})
    clips = ClipFactory(tmp_path, [MOVING])
    ref = tmp_path / "rowan.png"
    ref.write_bytes(b"png")
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, clips):
            handle = await _to_final_cut(env, h, tmp_path, "ep95")
            two = lint_ok("Low close-up of two animals", count=2)
            with pytest.raises(WorkflowUpdateFailedError) as refused:
                await handle.execute_update("recompose_shot", args=[
                    "A01", "r", two, [str(ref)], ["rowan"], None, {"water": True}])
            assert "may only change" in str(refused.value.cause)
            out = await handle.execute_update("recompose_shot", args=[
                "A01", "Studio: two in frame", two, [str(ref)], ["rowan"], None, {"count": 2}])
            await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                        timeout=90, what="recompose still parked (child lint passed on the NEW count)")
            assert h.still_calls[-1][2] == two
            plans = await handle.query("recompose_still_plans")
            assert plans["A01"]["version"] == out["version"] and plans["A01"]["shot_meta"]["count"] == 2
            assert plans["A01"]["still_ready"] and not plans["A01"]["finished"]
            await handle.execute_update("recompose_decision", args=["A01", True, "two confirmed"])
            landed = await _child_result(env, "ep95-shot-A01-recompose1", timeout=90)
            assert landed.get("clip_url"), landed
            await _wait(handle, lambda s: not s["recompose"]["pending"], what="closed")
            plans = await handle.query("recompose_still_plans")
            assert plans["A01"]["finished"] and plans["A01"]["decision"] == "approved", plans
            assert plans["A01"]["shot_meta"]["count"] == 2
            await handle.terminate("test done")
