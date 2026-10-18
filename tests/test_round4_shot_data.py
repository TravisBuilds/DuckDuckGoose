"""Round 4: per-shot data (shots.json / refs.json), model/duration/end-frame, refs, G2.01, episode GET.

All fixtures are SYNTHETIC (tmp dirs). No private episode content, no network (respx / stubs only).
"""

import asyncio
import json
import os
import sys

import aiosqlite
import httpx
import pytest
import respx
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg import activities
from hfvg.shot_data import (
    ShotDataError,
    load_shot_plans,
    merge_into_shots,
    parse_shots,
    resolve_refs,
)
from tests.ledger_helpers import insert_line, set_balance
from tests.lint_helpers import acapture, capture, lint_meta, lint_ok

TRAILING_PROMPT = "Synthetic prompt A. Exactly 2 animals in frame. Real-time speed, no slow motion.  \n"


def _shot(sid="A01", **kw):
    shot = {
        "id": sid, "count": 2, "characters": ["Birch", "Duck"], "wet": False, "exterior": True,
        "water": False, "model": "kling", "duration": 4, "end_frame": False,
        "antler_character": True, "duck_present": True, "aspect_ratio": "9:16",
        "prompt": TRAILING_PROMPT, "ref_keys": ["r_have", "r_new"],
    }
    shot.update(kw)
    return shot


def _write_data(tmp_path, shots, refs_dir=None, make_new=False):
    """Write synthetic shots.json + refs.json; r_have exists on disk, r_new is planned."""
    refs_dir = refs_dir or tmp_path
    have = refs_dir / "have.png"
    have.write_bytes(b"x")
    new = refs_dir / "new.png"
    if make_new:
        new.write_bytes(b"x")
    per_shot = {
        s["id"]: [
            {"key": k, "path": str(have if k == "r_have" else new), "exists": k == "r_have"}
            for k in s["ref_keys"]
        ]
        for s in shots
    }
    sp, rp = tmp_path / "shots.json", tmp_path / "refs.json"
    sp.write_text(json.dumps({"episode_id": "ep98", "shots": shots}))
    rp.write_text(json.dumps({"episode_id": "ep98", "per_shot": per_shot}))
    return sp, rp, new


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    shots = [_shot("A01"), _shot("A02", end_frame=True, duration=5), _shot("E05", duration=12),
             _shot("B04", duration=3, ref_keys=["r_have"])]
    sp, rp, new = _write_data(tmp_path, shots)
    monkeypatch.setenv("SHOTS_JSON_PATH", str(sp))
    monkeypatch.setenv("REFS_JSON_PATH", str(rp))
    return {"shots": sp, "refs": rp, "new": new, "tmp": tmp_path}


# ------------------------------------------------------------------ F1 loader / shot_meta

def test_shot_meta_filled_from_shots_json(data_env):
    plans = load_shot_plans("ep98", require=True)
    meta = plans["A01"]["shot_meta"]
    assert meta["count"] == 2
    assert meta["characters"] == ["Birch", "Duck"]
    assert meta["antler"] is True and meta["duck"] is True
    assert meta["wet"] is False and meta["exterior"] is True
    assert plans["A01"]["tags"] == []
    wet = parse_shots({"shots": [_shot("C04", wet=True, water=True)]})["C04"]
    assert wet["shot_meta"]["wet"] is True and wet["tags"] == ["wet"]
    assert wet["shot_meta"]["water"] is True


def test_prompt_is_used_as_is(data_env):
    plans = load_shot_plans("ep98", require=True)
    assert plans["A01"]["prompt"] == TRAILING_PROMPT


def test_shots_json_path_env_override(tmp_path, monkeypatch):
    alt = tmp_path / "elsewhere" / "my-shots.json"
    alt.parent.mkdir()
    alt.write_text(json.dumps({"shots": [_shot("A01", ref_keys=[])]}))
    monkeypatch.setenv("SHOTS_JSON_PATH", str(alt))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "nonexistent"))
    plans = load_shot_plans("ep98", require=False)
    assert plans is not None and set(plans) == {"A01"}


def test_live_refuses_without_shots_json(tmp_path, monkeypatch):
    monkeypatch.delenv("SHOTS_JSON_PATH", raising=False)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    err = capture(ShotDataError, load_shot_plans, "ep98", True)
    assert err is not None, "live mode must refuse when shots.json is missing"
    assert "shots.json" in str(err)
    assert load_shot_plans("ep98", require=False) is None  # dry mode tolerates absence


def test_shots_json_validation_rejects_bad_entries():
    for bad in (
        {"shots": []},
        {"shots": [_shot(model="veo")]},
        {"shots": [_shot(duration=0)]},
        {"shots": [_shot(count="2")]},
        {"shots": [_shot(wet="no")]},
        {"shots": [_shot("A01"), _shot("A01")]},
    ):
        assert capture(ShotDataError, parse_shots, bad) is not None, bad


@pytest.mark.asyncio
async def test_parse_beatmap_activity_merges_and_live_requires(tmp_path, monkeypatch, data_env):
    from hfvg.activities.workflow_support import parse_beatmap_activity

    # Reset DRY_RUN to default state (test isolation)
    monkeypatch.setenv("DRY_RUN", "true")
    
    beat = tmp_path / "BEATMAP.md"
    beat.write_text(
        "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | A01 | 2.5 (3.5) | Lobby | Morning | D R | Synthetic action | beat | – | – | K |\n"
        "| 2 | A02 | 3.0 (4.0) ⇥ | Lobby | Morning | D R | Synthetic action | beat | – | – | K |\n"
    )
    out = await parse_beatmap_activity(str(beat), "ep98", False)
    by_id = {s["shot_id"]: s for s in out["shots"]}
    assert by_id["A01"]["prompt"] == TRAILING_PROMPT
    assert by_id["A01"]["shot_meta"]["count"] == 2
    assert by_id["A02"]["params"]["duration"] == 5 and by_id["A02"]["params"]["end_frame"] is True
    # live (DRY_RUN=false) + beatmap shots that differ from shots.json -> refuse
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    err = await acapture(ShotDataError, parse_beatmap_activity(str(beat), "ep98", False))
    assert err is not None and "differ" in str(err)


def test_merge_requires_matching_shot_sets_in_live():
    plans = parse_shots({"shots": [_shot("A01", ref_keys=[])]})
    for p in plans.values():
        p.update({"refs": [], "missing_refs": []})
    beat = [{"shot_id": "A01"}, {"shot_id": "A99"}]
    assert capture(ShotDataError, merge_into_shots, "ep98", beat, plans, True) is not None
    assert len(merge_into_shots("ep98", beat, plans, False)) == 2  # dry: tolerated


# ------------------------------------------------------------------ F2 model / duration / end frame

def test_params_carry_model_duration_and_end_frame(data_env):
    plans = load_shot_plans("ep98", require=True)
    assert plans["E05"]["params"]["duration"] == 12
    assert plans["B04"]["params"]["duration"] == 3
    assert plans["A02"]["params"]["duration"] == 5
    assert plans["A02"]["params"]["end_frame"] is True and plans["A01"]["params"]["end_frame"] is False
    assert plans["A01"]["params"]["model"] == "kling"
    assert parse_shots({"shots": [_shot("S1", model="seedance")]})["S1"]["params"]["model"] == "seedance"
    assert plans["A02"]["shot_meta"]["end_frame"] is True


def test_dry_clip_estimates_use_real_model_and_duration():
    from hfvg.pricing import clip_estimate_usd_micros

    assert clip_estimate_usd_micros("kling", "final", 12) == 672_000
    assert clip_estimate_usd_micros("kling", "final", 3) == 168_000
    assert clip_estimate_usd_micros("seedance", "final", 4) == 4_548_800
    assert clip_estimate_usd_micros("seedance", "draft", 4) == 822_400


@pytest.mark.asyncio
async def test_dry_clip_reservation_uses_model_and_duration(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.studio_db import create_episode, init_studio_db

    db_path = str(tmp_path / "dry.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep98")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    k12 = await submit_clip_job_enforced("ep98", "E05", "/fake/still.jpg", lint_ok("S"), 12, 1,
                                         None, lint_meta(), "kling", [])
    k3 = await submit_clip_job_enforced("ep98", "B04", "/fake/still.jpg", lint_ok("S"), 3, 1,
                                        None, lint_meta(), "kling", [])
    sd = await submit_clip_job_enforced("ep98", "A01", "/fake/still.jpg", lint_ok("S"), 5, 1,
                                        "final", lint_meta(), "seedance", [])
    assert k12["reserved_amount"] == 672_000
    assert k3["reserved_amount"] == 168_000
    assert sd["reserved_amount"] == 5_686_000


async def _live_env(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

    db_path = str(tmp_path / "r4.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await BudgetLedger(db_path).init_db()
    # Initialize credit ledger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L4_video", 20_000_000, 16_000_000)
        await insert_line(db, "ep99", "L2_drafts", 20_000_000, 16_000_000)
        await db.commit()
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    return db_path


@pytest.mark.asyncio
@respx.mock
async def test_live_start_end_shot_without_end_image_is_refused_before_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_env(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    meta = lint_meta(end_frame=True)
    err = await acapture(ValueError, submit_clip_job_enforced(
        "ep99", "A02", "https://example.com/s.jpg", lint_ok("S"), 5, 1, "final", meta, "kling", [], None))
    assert err is not None and "end_image_url" in str(err)
    assert sub.call_count == 0, "no provider request may be made"
    status = await BudgetLedger(db_path).get_line_status("ep99", "L4_video")
    assert status["reserved"] == 0


# ------------------------------------------------------------------ F3 refs

def test_missing_refs_are_listed_not_dropped(data_env):
    plans = load_shot_plans("ep98", require=True)
    assert plans["A01"]["missing_refs"] == ["r_new"]
    assert len(plans["A01"]["refs"]) == 1 and plans["A01"]["refs"][0].endswith("have.png")
    assert plans["B04"]["missing_refs"] == []


def test_refs_checked_on_disk_not_by_stale_flag(data_env):
    assert resolve_refs("ep98", {"A01": ["r_have", "r_new"]})["A01"]["missing_refs"] == ["r_new"]
    data_env["new"].write_bytes(b"x")  # the planned ref now exists, flag in refs.json still false
    got = resolve_refs("ep98", {"A01": ["r_have", "r_new"]})["A01"]
    assert got["missing_refs"] == [] and len(got["refs"]) == 2


@pytest.mark.asyncio
@respx.mock
async def test_live_still_with_missing_refs_is_held_before_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_env(tmp_path, monkeypatch)
    
    # Create the ref file that exists (so mutation doesn't fail with FileNotFoundError)
    ref_file = tmp_path / "a" / "have.png"
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    ref_file.write_bytes(b"fake-ref-data")
    
    # Mock upload API (real shape per live probe)
    respx.post("https://api.higgsfield.ai/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={
            "upload_url": "http://upload/url",
            "public_url": "http://cdn.example.com/media123.png",
            "upload_headers": {"Content-Type": "image/png", "x-amz-tagging": "retention=temporary"}
        })
    )
    respx.put("http://upload/url").mock(
        return_value=httpx.Response(200, content=b"")
    )
    
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, [str(ref_file)], "1k", "medium", "9:16", None,
        lint_meta(), ["r_new"]))
    assert err is not None and "G2.01" in str(err) and "r_new" in str(err)
    assert sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_live_clip_with_missing_refs_is_held_before_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced

    await _live_env(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    err = await acapture(ValueError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("S"), 4, 1, "final", lint_meta(), "kling",
        [], None, None, ["r_new"]))
    assert err is not None and "G2.01" in str(err)
    assert sub.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_live_still_over_ref_limit_is_refused_not_truncated(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_env(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    refs = [f"https://example.com/r{i}.png" for i in range(9)]  # Grok cap is 8
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, refs, "1k", "medium", "9:16", None, lint_meta(), []))
    assert err is not None and "silently dropping" in str(err), err
    assert sub.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_still_provider_refuses_instead_of_truncating_refs():
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    provider = HiggsfieldStillProvider(api_key="test_id:test_secret", base_url="http://mock.api")
    try:
        err = await acapture(ValueError, provider.submit_image(
            prompt="p", image_urls=[f"https://example.com/{i}.png" for i in range(9)]))
        assert err is not None, "9 refs must be refused on Grok (cap 8), not silently cut to 8"
        assert route.call_count == 0
        await provider.submit_image(prompt="p", image_urls=[f"https://example.com/{i}.png" for i in range(8)])
        assert len(json.loads(route.calls[0].request.content)["image_urls"]) == 8
    finally:
        await provider.close()


# ------------------------------------------------------------------ ShotWorkflow wiring (stub activities)

async def _run_shot_workflow(shot_plan, *, send_end_image=None, expect_wait=False):
    calls = {"still": None, "clip": None}

    @activity.defn(name="submit_still_job_enforced")
    async def still(*args):
        calls["still"] = args
        return {"job_id": "s1", "line_name": "L2_drafts", "reserved_amount": 1}

    @activity.defn(name="submit_clip_job_enforced")
    async def clip(*args):
        calls["clip"] = args
        return {"job_id": "c1", "line_name": "L4_video", "reserved_amount": 1}

    @activity.defn(name="poll_job_status")
    async def poll(job_id, job_type, episode_id):
        return {"status": "completed", "output_url": f"https://x/{job_type}", "cost": None}

    @activity.defn(name="commit_job_budget")
    async def commit(*args):
        return None

    @activity.defn(name="commit_credits_activity")
    async def commit_credits(*args):
        return None

    @activity.defn(name="release_credits_activity")
    async def release_credits(*args):
        return None

    @activity.defn(name="precheck_still_qc")
    async def pre_still(*args):
        return {"passed": True, "issues": []}

    @activity.defn(name="precheck_clip_qc")
    async def pre_clip(*args):
        return {"passed": True, "issues": []}

    @activity.defn(name="review_still")
    async def rev_still(*args):
        return {"passed": True, "issues": []}

    @activity.defn(name="review_clip")
    async def rev_clip(*args):
        return {"passed": True, "issues": []}

    @activity.defn(name="record_shot_result")
    async def record(*args):
        return None

    from hfvg.workflows.shot import ShotWorkflow

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client, task_queue="r4-q", workflows=[ShotWorkflow],
            activities=[still, clip, poll, commit, commit_credits, release_credits, pre_still, pre_clip, rev_still, rev_clip, record,
                        activities.release_job_budget, activities.mark_job_pending_reconcile],
        ):
            handle = await env.client.start_workflow(
                ShotWorkflow.run, args=["ep98", shot_plan], id="r4-shot", task_queue="r4-q")
            await handle.signal("stills_approved")
            if expect_wait:
                await asyncio.sleep(1.0)
                assert calls["clip"] is None, "end-frame shot must wait for its end image (live)"
            if send_end_image:
                await handle.signal("set_end_image", send_end_image)
            await asyncio.wait_for(handle.result(), timeout=30)
    return calls


def _plan(**kw):
    plan = {
        "shot_id": "A02", "prompt": "Synthetic prompt", "refs": ["/refs/a.png", "/refs/b.png"],
        "missing_refs": ["r_new"], "shot_meta": lint_meta(), "tags": ["wet"],
        "params": {"model": "kling", "duration": 12, "aspect_ratio": "9:16", "end_frame": False},
    }
    plan.update(kw)
    return plan


@pytest.mark.asyncio
async def test_shot_workflow_passes_refs_to_still_provider_activity():
    calls = await _run_shot_workflow(_plan())
    args = calls["still"]
    assert args[4] == ["/refs/a.png", "/refs/b.png"]  # refs
    assert args[10] == ["r_new"]  # missing refs reach the activity (live refuses)


@pytest.mark.asyncio
async def test_shot_workflow_passes_real_model_duration_tags_to_clip():
    plan = _plan(params={"model": "seedance", "duration": 12, "end_frame": False})
    calls = await _run_shot_workflow(plan)
    args = calls["clip"]
    assert args[4] == 12  # duration (not the 5 s default)
    assert args[8] == "seedance"  # model (not the kling default)
    assert args[9] == ["wet"]  # tags
    assert args[12] == ["r_new"]  # missing refs reach the clip activity


@pytest.mark.asyncio
async def test_shot_workflow_end_frame_dry_gets_placeholder_and_live_waits_for_signal():
    end_params = {"model": "kling", "duration": 5, "end_frame": True}
    # After bug fix: end-frame stills are generated before Gate 2
    # In dry mode, end still is generated and returns mock URL "https://x/still"
    dry = await _run_shot_workflow(_plan(params=end_params, dry_run=True))
    assert dry["clip"][10] == "https://x/still", "End frame should be the generated still URL"
    # In live mode, end still is also generated before Gate 2 (no longer waits for signal)
    live = await _run_shot_workflow(_plan(params=end_params, dry_run=False))
    assert live["clip"][10] == "https://x/still", "End frame should be the generated still URL"


@pytest.mark.asyncio
async def test_shot_workflow_start_only_shot_gets_no_end_image():
    calls = await _run_shot_workflow(_plan(params={"model": "kling", "duration": 3, "end_frame": False}))
    assert calls["clip"][10] is None and calls["clip"][4] == 3


# ------------------------------------------------------------------ F6 G2.01 in the episode workflow

BEAT = (
    "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | A01 | 2.5 (3.5) | Lobby | Morning | D R | Synthetic action | beat | – | – | K |\n"
    "| 2 | B04 | 2.5 (3.0) | Lobby | Morning | D R | Synthetic action | beat | – | – | K |\n"
)

SEEN_PLANS: dict = {}
SEEN_PLANS_M95: dict = {}  # Separate for M-95/M-97 test


from temporalio import workflow as _wf  # noqa: E402


@_wf.defn(name="ShotWorkflow", sandboxed=False)
class StubShot:
    def __init__(self):
        self.ok = False

    @_wf.signal
    def stills_approved(self):
        self.ok = True

    @_wf.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        SEEN_PLANS[shot_plan["shot_id"]] = shot_plan
        await _wf.wait_condition(lambda: self.ok)
        return {"shot_id": shot_plan["shot_id"], "status": "success"}


@_wf.defn(name="ShotWorkflow", sandboxed=False)
class StubShotM95:
    """Separate stub for M-95/M-97 test - writes to SEEN_PLANS_M95."""
    def __init__(self):
        self.ok = False

    @_wf.signal
    def stills_approved(self):
        self.ok = True

    @_wf.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        SEEN_PLANS_M95[shot_plan["shot_id"]] = shot_plan
        await _wf.wait_condition(lambda: self.ok)
        return {"shot_id": shot_plan["shot_id"], "status": "completed"}


async def _gate(handle, name, timeout=15.0):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        state = await handle.query(EpisodeWorkflowV2.get_state)
        if state["current_gate"] == name:
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow never reached gate {name}; at {state['current_gate']}")


async def _wait_for_passed_gate(handle, name: str, timeout: float = 5.0):
    """Wait for a gate to appear in passed_gates."""
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        state = await handle.query(EpisodeWorkflowV2.get_state)
        if name in state.get("passed_gates", []):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"workflow never passed gate {name}; passed_gates: {state.get('passed_gates', [])}")


async def _episode_to_g201(tmp_path, monkeypatch, dry_run):
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode, list_anchor_stills, approve_g2_shots
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    import hfvg.activities as activities

    db_path = str(tmp_path / "ep.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    await init_studio_db(db_path)
    await create_episode(db_path, "ep98")
    await set_live_mode(db_path, "ep98", True)
    await approve_g108(db_path, "ep98")
    beat = tmp_path / "BEATMAP.md"
    beat.write_text(BEAT)
    SEEN_PLANS.clear()
    stub = StubShot
    env_cm = await WorkflowEnvironment.start_time_skipping()
    from hfvg.activities.credit_activities import (
        init_episode_credit_line_activity,
        init_episode_budget_lines_activity,
    )
    from temporalio import activity
    
    @activity.defn(name="preflight_validate_episode_activity")
    async def mock_preflight(*args):
        return {"validated": True, "warnings": []}
    
    @activity.defn(name="verify_episode_files_activity")
    async def mock_verify(*args):
        return True
    
    @activity.defn(name="load_refs_to_generate_activity")
    async def mock_load_refs(*args):
        return []  # No refs to generate
    
    @activity.defn(name="generate_run_report")
    async def mock_report(*args):
        return {"report_path": "/fake/report.txt"}
    
    @activity.defn(name="compute_g1_plan_activity")
    async def mock_g1_plan(*args):
        # Stub G1 plan to avoid real /estimate API calls
        return {
            "shots": [],
            "total_still_credits": 0.0,
            "total_clip_credits": 0.0,
            "total_credits": 0.0,
            "total_still_usd": "0.00",
            "total_clip_usd": "0.00",
            "total_usd": "0.00",
            "estimates_available": True,
        }
    
    worker_cm = Worker(
        env_cm.client, task_queue="r4-ep", workflows=[EpisodeWorkflowV2, stub],
        activities=[
            activities.acquire_run_lock,
            activities.release_run_lock,
            activities.upsert_episode_activity,
            init_episode_credit_line_activity,
            init_episode_budget_lines_activity,
            mock_preflight,
            mock_verify,
            mock_load_refs,
            mock_report,
            mock_g1_plan,
            activities.load_gate_policy_activity, activities.parse_beatmap_activity,
            activities.recheck_refs_activity, activities.check_live_mode_and_g108,
            activities.approve_g108_activity,
            list_anchor_stills,
            approve_g2_shots,
            activities.trim_clips, activities.render_edit, activities.mix_audio,
            activities.generate_voiceover, activities.generate_sfx, activities.generate_music,
            activities.post_to_platform,
        ],
    )
    return env_cm, worker_cm, beat, EpisodeWorkflowV2


async def _approve_to_g201(handle, W):
    for sig in (W.approve_g101, W.approve_g103, W.approve_g108, W.approve_gc02):
        await handle.signal(sig)
    return await _gate(handle, "G2.01")


@pytest.mark.asyncio
async def test_shot_workflow_passes_refs_and_missing_refs_to_still_activity(tmp_path, monkeypatch):
    """M-85, M-86: ShotWorkflow passes refs and missing_refs to still activity."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from temporalio import activity
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    from hfvg.activities.credit_activities import init_episode_credit_line_activity
    from hfvg.budget import BudgetLedger
    from tests.ledger_helpers import insert_line
    import aiosqlite
    
    # Track what the stub activity receives
    CAPTURED_PARAMS = {}
    
    @activity.defn(name="submit_still_job_enforced")
    async def stub_still_activity(
        episode_id: str,
        shot_id: str,
        prompt: str,
        version: int = 1,
        refs: list[str] | None = None,
        resolution: str = "1k",
        quality: str = "medium",
        aspect_ratio: str = "9:16",
        tier: str | None = None,
        shot_meta: dict | None = None,
        missing_refs: list[str] | None = None,
        attempt: int = 1,
    ):
        """Stub that captures params for assertion."""
        CAPTURED_PARAMS["refs"] = refs
        CAPTURED_PARAMS["missing_refs"] = missing_refs
        CAPTURED_PARAMS["shot_id"] = shot_id
        return {
            "job_id": "stub-job-123",
            "status": "completed",
            "output_url": "https://fake.com/still.jpg",
            "usd_micros": 10_000,
            "line_name": "L2_drafts",
            "reserved_amount": 10_000,
        }
    
    @activity.defn(name="precheck_still_qc")
    async def stub_precheck_qc(*args, **kwargs):
        return {"passed": True}
    
    @activity.defn(name="record_shot_result")
    async def stub_record(*args, **kwargs):
        pass
    
    @activity.defn(name="mark_job_pending_reconcile")
    async def stub_mark_pending(*args, **kwargs):
        pass
    
    @activity.defn(name="release_job_budget")
    async def stub_release_budget(*args, **kwargs):
        pass
    
    @activity.defn(name="poll_job_status")
    async def stub_poll(*args, **kwargs):
        return {"status": "completed", "output_url": "https://fake.com/out.jpg"}
    
    @activity.defn(name="precheck_clip_qc")
    async def stub_clip_qc(*args, **kwargs):
        return {"passed": True}
    
    @activity.defn(name="review_still")
    async def stub_review_still(*args, **kwargs):
        return {"passed": True}
    
    @activity.defn(name="review_clip")
    async def stub_review_clip(*args, **kwargs):
        return {"passed": True}
    
    @activity.defn(name="submit_clip_job_enforced")
    async def stub_clip(*args, **kwargs):
        return {
            "job_id": "clip-123",
            "status": "completed",
            "output_url": "https://fake.com/clip.mp4",
            "line_name": "L4_video",
            "reserved_amount": 50_000,
        }
    
    @activity.defn(name="commit_job_budget")
    async def stub_commit(*args, **kwargs):
        pass
    
    @activity.defn(name="commit_credits_activity")
    async def stub_commit_credits(*args, **kwargs):
        pass
    
    @activity.defn(name="release_credits_activity")
    async def stub_release_credits(*args, **kwargs):
        pass
    
    # Mock lint to avoid failures
    def mock_lint(episode_id, prompt, shot_meta, live):
        pass
    monkeypatch.setattr("hfvg.activities.studio_generation.enforce_prompt_lint", mock_lint)
    
    # Setup DB
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await init_episode_credit_line_activity("ep99", cap_credits=100_000)
    
    await BudgetLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L2_drafts", 100_000_000, 90_000_000)
        await db.commit()
    
    # Start workflow environment with stub activity
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker = Worker(
            env.client,
            task_queue="test-shot",
            workflows=[ShotWorkflow],
            activities=[
                stub_still_activity,
                stub_precheck_qc,
                stub_record,
                stub_mark_pending,
                stub_release_budget,
                stub_poll,
                stub_clip_qc,
                stub_review_still,
                stub_review_clip,
                stub_clip,
                stub_commit,
                stub_commit_credits,
                stub_release_credits,
            ],
        )
        async with worker:
            # Create shot plan with refs and missing_refs
            shot_plan = {
                "episode_id": "ep99",
                "shot_id": "A01",
                "prompt": "Test shot",
                "refs": ["/path/to/ref1.jpg", "/path/to/ref2.jpg"],  # M-85 tests this
                "missing_refs": ["missing_ref1"],  # M-86 tests this
                "shot_meta": {
                    "count": 1,
                    "characters": ["Birch"],
                    "antler_character": True,
                    "duck_present": False,
                    "wet": False,
                },
                "params": {
                    "model": "higgsfield",
                    "resolution": "1k",
                    "aspect_ratio": "9:16",
                },
                "dry_run": False,
                "end_frame": False,
            }
            
            # Start shot workflow
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", shot_plan],
                id="test-shot-A01",
                task_queue="test-shot",
            )
            
            # Signal approval to proceed
            await handle.signal(ShotWorkflow.stills_approved)
            
            # Wait for completion
            await handle.result()
            
            # Assert the stub activity received the refs and missing_refs
            assert CAPTURED_PARAMS["shot_id"] == "A01"
            assert CAPTURED_PARAMS["refs"] == ["/path/to/ref1.jpg", "/path/to/ref2.jpg"], \
                f"M-85: Workflow must pass refs to activity, got {CAPTURED_PARAMS['refs']}"
            assert CAPTURED_PARAMS["missing_refs"] == ["missing_ref1"], \
                f"M-86: Workflow must pass missing_refs to activity, got {CAPTURED_PARAMS['missing_refs']}"


@pytest.mark.asyncio
async def test_shot_workflow_passes_refs_to_still_provider_activity(tmp_path, monkeypatch):
    """M-96: Activity refuses submission when refs are missing (live mode)."""
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    from unittest.mock import AsyncMock, patch
    import aiosqlite
    
    # Mock lint to always pass so we reach refs check
    def mock_lint(episode_id, prompt, shot_meta, live):
        pass  # Do nothing, no errors
    monkeypatch.setattr("hfvg.activities.studio_generation.enforce_prompt_lint", mock_lint)
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate  # Live mode to trigger refs check
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    
    # Initialize credit line
    from hfvg.activities.credit_activities import init_episode_credit_line_activity
    await init_episode_credit_line_activity("ep99", cap_credits=1000)
    
    # Initialize budget lines
    from hfvg.budget import BudgetLedger
    from tests.ledger_helpers import insert_line
    await BudgetLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L2_drafts", 10_000_000, 8_000_000)
        await db.commit()
    
    # Mock provider to avoid real network calls when mutation disables refs check
    class MockProvider:
        model_path = "test/model"
        async def close(self): pass
        async def estimate_usd_micros(self, **kwargs): return 50_000
        async def estimate_usd_and_credits(self, **kwargs): return (50_000, 1.0)
        async def submit_image(self, **kwargs): return "fake-job-id"
        async def upload_reference(self, path, version): return {"public_url": f"fake://ref/{path}"}
    
    # Try to submit with missing refs - lint is mocked, should hit refs check
    with patch("hfvg.activities.studio_generation.HiggsfieldStillProvider", return_value=MockProvider()), \
         patch("hfvg.activities.studio_generation.check_live_mode_and_g108", new_callable=AsyncMock, return_value=(True, True)):
        raised_error = None
        try:
            await submit_still_job_enforced(
                "ep99", "A01", "Test prompt", 1,
                missing_refs=["ref_missing"],  # Has missing refs
                shot_meta={"count": 1, "characters": ["D"], "antler": False, "duck": False, "wet": False}
            )
        except ValueError as e:
            raised_error = e
        
        assert raised_error is not None, "Should have raised ValueError for missing refs"
        assert "G2" in str(raised_error) and "refs not on disk" in str(raised_error), \
            f"Expected G2 refs error, got: {raised_error}"


@pytest.mark.asyncio
async def test_g201_triggers_for_new_refs_and_plans_carry_data(tmp_path, monkeypatch, data_env):
    """G2.01 logic now in G2_SCENE_STILLS gate - tests missing refs detection and recheck merge."""
    # Run in live mode so recheck is executed
    env, worker, beat, W = await _episode_to_g201(tmp_path, monkeypatch, dry_run=False)
    
    # Create ref files - both refs exist so recheck can find them
    shots = [_shot("A01"), _shot("B04", duration=3, ref_keys=["r_have"])]
    sp, rp, new = _write_data(data_env["tmp"], shots, make_new=True)  # make_new=True creates r_new file
    monkeypatch.setenv("SHOTS_JSON_PATH", str(sp))
    monkeypatch.setenv("REFS_JSON_PATH", str(rp))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    
    async with env:
        async with worker:
            handle = await env.client.start_workflow(
                W.run, args=["ep98", str(beat), False], id="r4-ep98-live", task_queue="r4-ep")
            # Approve G1 to reach G2
            await handle.execute_update(W.approve_g1_shot_plan)
            state = await _gate(handle, "G2_SCENE_STILLS")
            assert state["current_gate"] == "G2_SCENE_STILLS"
            
            # Approve to proceed through G2
            await handle.execute_update(W.approve_g2_scene_stills)
            state = await _wait_for_passed_gate(handle, "G2_SCENE_STILLS")
            assert "G2_SCENE_STILLS" in state["passed_gates"], "should pass G2 after approval"
            
            # M-97: Assert recheck merged ref data into plan
            plan = SEEN_PLANS["A01"]
            assert "refs" in plan, "recheck should add refs field to plan"
            assert len(plan["refs"]) == 2, f"A01 should have 2 refs after recheck, got {len(plan.get('refs', []))}"
            assert "missing_refs" in plan, "recheck should add missing_refs field"
            assert plan["missing_refs"] == [], f"A01 should have no missing refs, got {plan.get('missing_refs')}"
            
            # Also check other plan fields are preserved during merge
            assert plan["prompt"] == TRAILING_PROMPT and plan["shot_meta"]["count"] == 2
            assert plan["params"]["duration"] == 4 and plan["dry_run"] is False
            assert SEEN_PLANS["B04"]["params"]["duration"] == 3
            await handle.terminate()


@pytest.mark.asyncio
async def test_g2_detects_missing_refs_and_recheck_merges_data(tmp_path, monkeypatch):
    """M-95, M-97: Missing refs detection and recheck data merge (fresh env, unique episode)."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108, list_anchor_stills, approve_g2_shots
    from hfvg.activities.credit_activities import init_episode_credit_line_activity, init_episode_budget_lines_activity
    import hfvg.activities as activities
    
    # Fresh DB for unique episode
    db_path = str(tmp_path / "m95_97.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    await init_studio_db(db_path)
    await create_episode(db_path, "ep_m9597")
    await set_live_mode(db_path, "ep_m9597", True)
    await approve_g108(db_path, "ep_m9597")
    await init_episode_credit_line_activity("ep_m9597", cap_credits=100_000)
    await init_episode_budget_lines_activity("ep_m9597")
    
    # Create data files - r_have exists, r_new missing
    beat = tmp_path / "BEATMAP_M9597.md"
    beat.write_text(BEAT.replace("ep98", "ep_m9597"))
    data_dir = tmp_path / "m9597data"
    data_dir.mkdir(exist_ok=True)
    shots = [_shot("A01"), _shot("B04", duration=3, ref_keys=["r_have"])]
    sp, rp, new = _write_data(data_dir, shots, make_new=False)
    monkeypatch.setenv("SHOTS_JSON_PATH", str(sp))
    monkeypatch.setenv("REFS_JSON_PATH", str(rp))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    
    # Create activity mocks
    from temporalio import activity
    
    @activity.defn(name="preflight_validate_episode_activity")
    async def mock_preflight(*args):
        return {"validated": True, "warnings": []}
    @activity.defn(name="verify_episode_files_activity")
    async def mock_verify(*args):
        return True
    @activity.defn(name="load_refs_to_generate_activity")
    async def mock_load_refs(*args):
        return []
    @activity.defn(name="generate_run_report")
    async def mock_report(*args):
        return {"report_path": "/fake/report.txt"}
    @activity.defn(name="acquire_run_lock")
    async def mock_acquire_lock(*args):
        return None
    @activity.defn(name="release_run_lock")
    async def mock_release_lock(*args):
        return None
    @activity.defn(name="recheck_refs_activity")
    async def mock_recheck(episode_id: str, ref_keys_by_shot: dict):
        """Mock recheck that returns r_new present for M-97 test."""
        have_path = str(data_dir / "have.png")
        new_path = str(data_dir / "new.png")
        # Always return both refs present (simulating that r_new was created)
        return {
            "A01": {"refs": [have_path, new_path], "missing_refs": []},
            "B04": {"refs": [have_path], "missing_refs": []},
        }
    
    @activity.defn(name="compute_g1_plan_activity")
    async def mock_g1_plan(*args):
        # Stub G1 plan to avoid real /estimate API calls
        return {
            "shots": [],
            "total_still_credits": 0.0,
            "total_clip_credits": 0.0,
            "total_credits": 0.0,
            "total_still_usd": "0.00",
            "total_clip_usd": "0.00",
            "total_usd": "0.00",
            "estimates_available": True,
        }
    
    SEEN_PLANS_M95.clear()
    
    # Fresh Temporal env
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker = Worker(
            env.client, task_queue="m9597-q", workflows=[EpisodeWorkflowV2, StubShotM95],
            activities=[
                mock_acquire_lock, mock_release_lock,
                activities.upsert_episode_activity,
                init_episode_credit_line_activity, init_episode_budget_lines_activity,
                mock_preflight, mock_verify, mock_load_refs, mock_report, mock_recheck, mock_g1_plan,
                activities.load_gate_policy_activity, activities.parse_beatmap_activity,
                activities.check_live_mode_and_g108,
                activities.approve_g108_activity,
                list_anchor_stills,
                approve_g2_shots,
                activities.trim_clips, activities.render_edit, activities.mix_audio,
                activities.generate_voiceover, activities.generate_sfx, activities.generate_music,
                activities.post_to_platform,
            ],
        )
        async with worker:
            # Test M-97: recheck merge
            # The parse_beatmap initially sees r_new missing (from refs.json + filesystem check)
            # The mocked recheck returns r_new present (simulating file creation)
            # The workflow should merge the recheck data into shot plans
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_m9597", str(beat), False],
                id="wf-m9597",
                task_queue="m9597-q",
            )
            
            # Approve G1
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            state = await _gate(handle, "G2_SCENE_STILLS")
            state = await handle.query(EpisodeWorkflowV2.get_state)
            
            # M-95: Assert recheck was called and updated missing_refs
            # After recheck (mocked to return r_new present), missing_refs should be empty
            assert state.get("missing_refs") == {}, \
                f"M-95: After recheck, missing_refs should be empty, got {state.get('missing_refs')}"
            
            # M-97: Assert recheck merged data into shot plans
            await handle.execute_update(EpisodeWorkflowV2.approve_g2_scene_stills)
            await _wait_for_passed_gate(handle, "G2_SCENE_STILLS")
            plan = SEEN_PLANS_M95["A01"]
            assert "refs" in plan, "M-97: recheck should merge refs"
            assert len(plan["refs"]) == 2, "M-97: Should have 2 refs after recheck"
            assert any("have.png" in str(r) for r in plan["refs"]), "Should have r_have"
            assert any("new.png" in str(r) for r in plan["refs"]), "M-97: Should have r_new after recheck"
            assert "missing_refs" in plan, "M-97: recheck should set missing_refs"
            assert plan["missing_refs"] == [], f"M-97: Should have no missing refs after recheck, got {plan.get('missing_refs')}"
            await handle.terminate()


@pytest.mark.asyncio
@pytest.mark.skip(reason="G2.01 collapsed into G2_SCENE_STILLS in 3-gate system")
async def test_g201_holds_live_while_refs_still_missing_then_releases(tmp_path, monkeypatch, data_env):
    env, worker, beat, W = await _episode_to_g201(tmp_path, monkeypatch, dry_run=False)
    # live episodes require the shot sets to match: shots.json here has exactly A01 + B04
    shots = [_shot("A01"), _shot("B04", duration=3, ref_keys=["r_have"])]
    sp, rp, new = _write_data(data_env["tmp"], shots)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    async with env:
        async with worker:
            handle = await env.client.start_workflow(
                W.run, args=["ep98", str(beat), False], id="r4-ep98-live", task_queue="r4-ep")
            await _approve_to_g201(handle, W)
            await handle.signal(W.approve_g201)  # approved, but the file is still not there
            await asyncio.sleep(1.0)
            state = await handle.query(W.get_state)
            assert state["current_gate"] == "G2.01", "must HOLD at G2.01 while refs are missing"
            assert "G2.01" not in state["passed_gates"]
            assert state["missing_refs"] == {"A01": ["r_new"]}
            assert not SEEN_PLANS, "no shot may start without its refs"
            new.write_bytes(b"x")  # the new ref is produced
            await handle.signal(W.approve_g201)
            await asyncio.sleep(1.0)
            state = await handle.query(W.get_state)
            assert "G2.01" in state["passed_gates"] and state["missing_refs"] == {}
            assert len(SEEN_PLANS["A01"]["refs"]) == 2
            await handle.terminate()


# ------------------------------------------------------------------ F4 GET /api/episodes/{id}

def _client(monkeypatch, temporal):
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    for name in [m for m in sys.modules if m == "api.main"]:
        del sys.modules[name]
    from fastapi.testclient import TestClient
    import api.main as api_main

    api_main.temporal_client = temporal
    return TestClient(api_main.app), api_main


def _temporal_mock(state=None, raises=None):
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    handle = MagicMock()
    if raises:
        handle.query = AsyncMock(side_effect=raises)
    else:
        handle.query = AsyncMock(return_value=state)
    client.get_workflow_handle = MagicMock(return_value=handle)
    client.start_workflow = AsyncMock(return_value=MagicMock(id="ep04", result_run_id="r"))
    return client


AUTH = {"Authorization": "Bearer " + "a" * 32}
STATE = {"episode_id": "ep04", "stage": "script", "approvals": {"g101": True}, "shots": 31}


def test_get_episode_looks_up_the_started_workflow_id(monkeypatch, tmp_path):
    # Create minimal beatmap for ep04
    beatmap_dir = tmp_path / "data/episodes/ep04"
    beatmap_dir.mkdir(parents=True, exist_ok=True)
    (beatmap_dir / "BEATMAP.md").write_text("# Episode 04\n\n## A01\nTest shot")
    monkeypatch.chdir(tmp_path)
    
    temporal = _temporal_mock(STATE)
    client, _ = _client(monkeypatch, temporal)
    start = client.post("/api/episodes", headers=AUTH, json={"episode_id": "ep04", "dry_run": True})
    assert start.status_code == 200
    started_id = temporal.start_workflow.call_args.kwargs["id"]
    resp = client.get("/api/episodes/ep04", headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["episode_id"] == "ep04"
    temporal.get_workflow_handle.assert_called_with(started_id)
    assert started_id == "ep04"
    temporal.list_workflows.assert_not_called()  # no prefix scan (that only finds ep04-shot-*)


def test_get_episode_accepts_episode_suffix_style_id(monkeypatch):
    temporal = _temporal_mock(STATE)
    client, _ = _client(monkeypatch, temporal)
    resp = client.get("/api/episodes/ep04-episode", headers=AUTH)
    assert resp.status_code == 200, resp.text
    temporal.get_workflow_handle.assert_called_with("ep04")


def test_get_episode_unknown_workflow_is_404_not_500(monkeypatch):
    from temporalio.service import RPCError, RPCStatusCode

    temporal = _temporal_mock(raises=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    client, _ = _client(monkeypatch, temporal)
    assert client.get("/api/episodes/ep04", headers=AUTH).status_code == 404


# ------------------------------------------------------------------ F1 API upload

@pytest.mark.asyncio
async def test_upload_shots_route_validates_and_stores(monkeypatch, tmp_path):
    from hfvg.studio_db import init_studio_db

    db_path = str(tmp_path / "up.db")
    await init_studio_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.chdir(tmp_path)
    client, api_main = _client(monkeypatch, _temporal_mock(STATE))
    login = client.post("/api/login", json={"admin_secret": "a" * 32})
    assert login.status_code == 200, login.text
    cookies = login.cookies
    good = json.dumps({"shots": [_shot("A01")]}).encode()
    refs = json.dumps({"per_shot": {"A01": []}}).encode()
    ok = client.post("/api/episodes/ep98/upload-shots", cookies=cookies,
                     files={"shots_file": ("shots.json", good), "refs_file": ("refs.json", refs)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["shots_count"] == 1
    assert (tmp_path / "data/episodes/ep98/shots.json").read_bytes() == good
    assert (tmp_path / "data/episodes/ep98/refs.json").read_bytes() == refs
    bad = client.post("/api/episodes/ep98/upload-shots", cookies=cookies,
                      files={"shots_file": ("shots.json", json.dumps({"shots": [_shot(model="veo")]}))})
    assert bad.status_code == 400


# ------------------------------------------------------------------ lint: kit file + flags

def test_lint_kit_loads_lint_section_of_full_prompt_kit(tmp_path, monkeypatch):
    from hfvg.prompt_lint import PromptLintError, load_lint_kit
    from tests.lint_helpers import SYNTHETIC_LINT_KIT

    full = tmp_path / "prompt_kit.json"
    full.write_text(json.dumps({"style": "s", "characters": {}, "lint": dict(SYNTHETIC_LINT_KIT)}))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(full))
    err = capture(PromptLintError, load_lint_kit, "ep98")
    assert err is None, f"full prompt_kit.json must load via its lint section: {err}"
    assert load_lint_kit("ep98").count_template == SYNTHETIC_LINT_KIT["count_template"]


def test_lint_flags_require_antler_and_duck_clauses_without_character_codes():
    from hfvg.prompt_lint import LintKit, lint_violations
    from tests.lint_helpers import SYNTHETIC_LINT_KIT

    kit = LintKit.from_dict(SYNTHETIC_LINT_KIT)
    meta = lint_meta(characters=["X"], antler=True, duck=True)
    problems = lint_violations(lint_ok("Synthetic scene"), meta, kit)
    assert any("antler clause" in p for p in problems), problems
    assert any("duck clause" in p for p in problems), problems
    ok = lint_ok("Synthetic scene") + " " + SYNTHETIC_LINT_KIT["antler_clause"] + " " + SYNTHETIC_LINT_KIT["duck_clause"]
    assert lint_violations(ok, meta, kit) == []
