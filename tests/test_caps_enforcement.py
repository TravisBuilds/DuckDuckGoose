"""Model-caps enforcement at the spend points, G1, the API and the retry paths (T15-T19, T22)."""
import asyncio
import itertools
import json
import sys

import httpx
import pytest
import respx
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.lint_helpers import acapture, lint_meta, lint_ok
from tests.test_round4_shot_data import _live_env

GROK = "xai/grok-imagine-image-2.0"
KLING = "kling-video/v3.0/pro/image-to-video"
API = "https://api.higgsfield.ai"


async def _no_spend(db_path):
    """No budget reserve and no credit-ledger rows for the episode."""
    import aiosqlite
    from hfvg.budget import BudgetLedger

    for line in ("L1_refs", "L2_drafts", "L4_video"):
        try:
            status = await BudgetLedger(db_path).get_line_status("ep99", line)
        except Exception:
            continue
        assert status["reserved"] == 0 and status.get("spent", 0) == 0, (line, status)
    async with aiosqlite.connect(db_path) as db:
        tables = [r[0] async for r in await db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in ("credit_reservations", "credit_jobs", "jobs", "job_ledger"):
            if table in tables:
                rows = [r async for r in await db.execute(f"SELECT * FROM {table}")]
                assert not rows, f"{table} has rows after a preflight refusal: {rows}"


def _mock_all():
    """Every Higgsfield route the spend path could touch; all must stay at zero calls."""
    return [
        respx.post(f"{API}/files/generate-upload-url").mock(return_value=httpx.Response(200, json={})),
        respx.post(url__regex=rf"{API}/estimate/.*").mock(
            return_value=httpx.Response(200, json={"credits": "1", "usd": "0.06"})),
        respx.post(f"{API}/{GROK}").mock(return_value=httpx.Response(200, json={"request_id": "never"})),
        respx.post(f"{API}/{KLING}").mock(return_value=httpx.Response(200, json={"request_id": "never"})),
        respx.get(url__regex=r"https://.*").mock(return_value=httpx.Response(200, content=b"x")),
    ]


# ------------------------------------------------------------------ T15 / T16: spend points

@pytest.mark.asyncio
@respx.mock
async def test_still_preflight_runs_before_estimate_and_reserve(tmp_path, monkeypatch):
    """T15: a still that breaks a model limit (aspect, and refs) is refused by preflight in
    submit_still_job_enforced: PreflightError (non-retryable), zero provider calls, no ledger rows."""
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.preflight import PreflightError

    db_path = await _live_env(tmp_path, monkeypatch)
    monkeypatch.delenv("MODEL_PATH_STILL", raising=False)
    routes = _mock_all()
    refs = []   # no refs: without preflight this still would go straight to estimate + submit
    # aspect 21:9 is not a Grok aspect: only preflight knows that (the legacy ref check cannot)
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, refs, "1k", "medium", "21:9", None, lint_meta(), []))
    assert isinstance(err, PreflightError), f"expected a preflight refusal, got {err!r}"
    assert err.non_retryable and err.type == "PreflightError"
    assert "aspect ratio '21:9' is not supported" in str(err) and "Limit source:" in str(err), err
    # 4k is not a Grok resolution
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, refs, "4k", "medium", "9:16", None, lint_meta(), []))
    assert isinstance(err, PreflightError) and "resolution '4k'" in str(err), repr(err)
    assert [r.call_count for r in routes] == [0] * len(routes), "a refused still reached the provider"
    await _no_spend(db_path)


@pytest.mark.asyncio
@respx.mock
async def test_clip_preflight_runs_before_estimate_and_reserve(tmp_path, monkeypatch):
    """T16: a Kling clip whose prompt is over 2500 chars (Higgsfield would silently cut it) or whose
    duration is out of range is refused in submit_clip_job_enforced before any estimate/reserve."""
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.preflight import PreflightError

    db_path = await _live_env(tmp_path, monkeypatch)
    routes = _mock_all()
    long_prompt = lint_ok("S" + " wide shot" * 260)
    assert len(long_prompt) > 2500
    err = await acapture(ValueError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", long_prompt, 5, 1, "final", lint_meta(), "kling", [], None))
    assert isinstance(err, PreflightError), f"expected a preflight refusal, got {err!r}"
    assert err.non_retryable and f"prompt is {len(long_prompt)} characters" in str(err)
    assert "accepts 2500 (Higgsfield silently cuts the rest)" in str(err), err
    err = await acapture(ValueError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("S"), 16, 1, "final", lint_meta(), "kling", [], None))
    assert isinstance(err, PreflightError) and "from 3 to 15 seconds" in str(err), repr(err)
    assert [r.call_count for r in routes] == [0] * len(routes), "a refused clip reached the provider"
    await _no_spend(db_path)


@pytest.mark.asyncio
@respx.mock
async def test_clip_preflight_allows_a_valid_kling_clip(tmp_path, monkeypatch):
    """Control for T16: a valid clip passes preflight and reaches estimate + submit."""
    from hfvg.activities.studio_generation import submit_clip_job_enforced

    await _live_env(tmp_path, monkeypatch)
    est = respx.post(f"{API}/estimate/{KLING}").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    sub = respx.post(f"{API}/{KLING}").mock(return_value=httpx.Response(200, json={"request_id": "ok"}))
    out = await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("S"), 5, 1, "final", lint_meta(), "kling", [], None)
    assert est.call_count >= 1 and sub.call_count == 1 and out["job_id"], out


# ------------------------------------------------------------------ T17: retry paths

@pytest.mark.asyncio
@respx.mock
async def test_end_still_over_cap_with_lead_refused_before_fetching_the_lead(tmp_path, monkeypatch):
    """T17 (activity): the backfill _end (lead + anchor + 5 planned -> 6 after the anchor drop) is
    refused by preflight before the start still is even downloaded, with no spend."""
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.preflight import PreflightError
    from tests.test_ref_cap_lead_anchor import START_URL, _setup

    db_path, refs, _, puts, est, sub = await _setup(tmp_path, monkeypatch)
    planned = [refs[k] for k in ("duck", "family", "mochi", "pool", "extra")]
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A02_end", lint_ok("S"), 1, planned, "1k", "medium", "9:16", None, lint_meta(), [], 4, START_URL))
    assert isinstance(err, PreflightError), repr(err)
    assert "A02_end (end frame) uses 6 reference images (including the start still" in str(err), err
    assert "Remove 1" in str(err)
    assert not [c for c in respx.calls if str(c.request.url) == START_URL], "lead fetched before preflight"
    assert sub.call_count == 0 and est.call_count == 0 and not puts
    await _no_spend(db_path)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_end_still_over_cap_parks_shot_with_the_creator_message(tmp_path, monkeypatch):
    """T17 (workflow): retry_end_still for an _end that preflight refuses -> one submit attempt (the
    PreflightError is non-retryable), the shot stays in failed_shots with the creator message, no clip."""
    from hfvg import preflight
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    import tests.test_end_still_backfill as bf

    five = ["refs/duck.png", "refs/family.png", "refs/mochi.png", "refs/pool.png", "refs/extra.png"]

    orig_shot = bf._shot

    def shot():
        s = orig_shot()
        s["refs"] = list(five)
        return s

    original = bf._activities

    def activities(log, **kw):
        from tests.test_pr10_216_fixes import _activities as base
        acts = {a.__temporal_activity_definition.name: a for a in original(log, **kw)}
        acts.update({a.__temporal_activity_definition.name: a for a in base(log, shot(), escalate=True)
                     if a.__temporal_activity_definition.name not in
                     ("submit_still_job_enforced", "poll_job_status", "resolve_landed_start_still")})
        fake_submit = acts["submit_still_job_enforced"]

        @activity.defn(name="submit_still_job_enforced")
        async def submit(ep, sid, prompt, version, refs, res, q, ar, tier, meta, missing, attempt, lead=None):
            if lead:   # the real preflight, with the actual lead (as submit_still_job_enforced runs it)
                log.append(("preflight", sid, attempt, len(refs or [])))
                found = preflight.preflight_shot(
                    {"id": sid, "start_prompt": prompt, "end_prompt": prompt, "ref_keys": list(refs or [])},
                    {"still_model": GROK, "anchor_ref": None, "lead_ref": lead},
                    __import__("hfvg.model_caps", fromlist=["x"]).get_registry(), phase="end")
                preflight.raise_on_errors(found, f"{ep}/{sid}")
            return await fake_submit(ep, sid, prompt, version, refs, res, q, ar, tier, meta, missing, attempt, lead)

        acts["submit_still_job_enforced"] = submit
        return list(acts.values())

    monkeypatch.setattr(bf, "_activities", activities)
    monkeypatch.setattr(bf, "_shot", shot)
    log = []

    async def body(h, wait):
        st = await wait(lambda s: True)
        failed = st["failed_shots"]
        assert failed and failed[0]["shot_id"] == "A02", st
        reason = failed[0]["reason"]
        assert "6 reference images" in reason and "at most 5" in reason, reason
        tries = [e for e in log if e[0] == "preflight"]
        assert len(tries) == 1, f"a non-retryable preflight refusal was retried: {tries}"
        assert not any(e[0] == "submit_still" and e[1] == "A02_end" for e in log), "over-cap _end submitted"
        n_before = len(log)
        try:
            await h.execute_update(EpisodeWorkflowV2.retry_end_still, args=["A02"])
        except WorkflowUpdateFailedError:
            pass   # a refusal is fine too; what matters is no submit and the creator message
        st = await wait(lambda s: not s.get("pending_retries") and s.get("failed_shots"))
        new = log[n_before:]
        assert len([e for e in new if e[0] == "preflight"]) <= 1, new
        assert not any(e[0] == "submit_still" and e[1] == "A02_end" for e in new), new
        assert "6 reference images" in st["failed_shots"][0]["reason"], st
        assert not any(e[0] == "submit_clip" for e in log), "clip started for a refused shot"

    await bf._parked(tmp_path, monkeypatch, log, body)


# ------------------------------------------------------------------ T18: API

def _client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient
    import api.main as api_main
    return TestClient(api_main.app)


AUTH = {"Authorization": "Bearer " + "a" * 32}


def test_models_endpoint_returns_creator_view(monkeypatch):
    """T18: GET /api/models lists offered models with creator-facing limits (unknown -> 'not published',
    sources with check dates), hides MCP-only models, needs auth."""
    client = _client(monkeypatch)
    assert client.get("/api/models").status_code == 401
    r = client.get("/api/models", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    by_id = {m["model_id"]: m for m in body["models"]}
    assert GROK in by_id and KLING in by_id and "gpt_image_2" not in by_id, sorted(by_id)
    grok = by_id[GROK]["limits"]
    assert grok["max_refs"]["value"] == 5 and grok["prompt_max_chars"]["value"] == "not published"
    assert grok["max_refs"]["sources"] and any(s["url"].startswith("https://docs.x.ai") for s in grok["max_refs"]["sources"])
    assert all(s["checked"] for s in grok["max_refs"]["sources"])
    assert by_id[KLING]["limits"]["prompt_max_chars"]["value"] == 2500
    assert body["checked"] and body["stale"] in (True, False)


def test_preflight_endpoint_reports_per_shot_pass_fail(monkeypatch, tmp_path):
    """POST /api/episodes/{id}/preflight reads the uploaded shots.json and returns per-shot pass/fail."""
    client = _client(monkeypatch)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    ep = tmp_path / "ep77"
    ep.mkdir()
    base = {"count": 0, "characters": [], "wet": False, "model": "kling", "duration": 5, "end_frame": False}
    shots = {"shots": [
        dict(base, id="A01", prompt="ok shot", ref_keys=["a", "b"]),
        dict(base, id="A02", prompt="over cap", ref_keys=["a", "b", "c", "d", "e", "f"]),
        dict(base, id="A03", prompt="end frame", end_frame=True, ref_keys=["a", "b", "c", "d", "e"]),
    ]}
    (ep / "shots.json").write_text(json.dumps(shots))
    assert client.post("/api/episodes/ep77/preflight").status_code == 401
    r = client.post("/api/episodes/ep77/preflight", headers=AUTH)
    assert r.status_code == 200, r.text
    res = r.json()["shots"]
    assert sorted(res) == ["A01", "A02", "A03"], res
    assert res["A01"]["ok"] is True and res["A02"]["ok"] is False and res["A03"]["ok"] is False, res
    assert "6 reference images" in res["A02"]["errors"][0]
    assert any("end frame" in e and "start still" in e for e in res["A03"]["errors"]), res["A03"]
    assert r.json()["has_errors"] is True
    assert client.post("/api/episodes/ep78/preflight", headers=AUTH).status_code == 404


# ------------------------------------------------------------------ T19: G1 validator

def _g1_activities(shots):
    from hfvg.activities.g1_plan import compute_g1_plan_activity

    @activity.defn(name="parse_beatmap_activity")
    async def parse(*a):
        return {"shots": shots}

    @activity.defn(name="init_episode_budget_lines_activity")
    async def budget_init(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}

    def mk(name, value):
        async def f(*a):
            return value
        return activity.defn(name=name)(f)

    ok = {"ok": True, "valid": True, "errors": [], "warnings": []}
    stubs = [mk("acquire_run_lock", True), mk("release_run_lock", True), mk("load_gate_policy_activity", {}),
             mk("load_refs_to_generate_activity", []), mk("generate_run_report", {}), mk("approve_g2_shots", 0),
             mk("list_anchor_stills", [])]
    stubs += [mk(n, ok) for n in ("init_episode_credit_line_activity", "preflight_validate_episode_activity",
                                  "upsert_episode_activity", "verify_episode_files_activity", "approve_g108_activity",
                                  "recheck_refs_activity", "update_credit_cap_activity")]
    return [parse, budget_init, compute_g1_plan_activity, *stubs]


async def _g1_state(handle):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    for _ in range(100):
        try:
            st = await handle.query(EpisodeWorkflowV2.get_state)
            if st.get("current_gate") == "G1_SHOT_PLAN" and st.get("g1_plan"):
                return st
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise AssertionError("G1 never reached")


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_g1_refused_while_preflight_errors(tmp_path, monkeypatch):
    """T19: the real compute_g1_plan_activity preflights every shot; the G1 validator refuses approval
    (no override) while any error exists, with the creator message; a clean plan is approved."""
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from tests.test_harness_pr10_mutations import FailingShotWorkflowForRetryTests

    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "none.db"))   # no DB: no anchors, nothing created
    bad = [{"shot_id": "A01", "prompt": "Test", "refs": [f"r{i}" for i in range(6)],
            "params": {"duration": 5, "model": "kling", "end_frame": False}}]
    good = [{"shot_id": "A01", "prompt": "Test", "refs": [f"r{i}" for i in range(5)],
             "params": {"duration": 5, "model": "kling", "end_frame": False}}]
    async with await WorkflowEnvironment.start_local() as env:
        for name, shots in (("bad", bad), ("good", good)):
            async with Worker(env.client, task_queue=f"g1pf-{name}",
                              workflows=[EpisodeWorkflowV2, FailingShotWorkflowForRetryTests],
                              activities=_g1_activities(shots)):
                h = await env.client.start_workflow(EpisodeWorkflowV2.run, args=["ep_test", None, True, ["A01"], 40],
                                                    id=f"g1pf-{name}", task_queue=f"g1pf-{name}")
                try:
                    st = await _g1_state(h)
                    plan = st["g1_plan"]
                    if name == "bad":
                        assert plan["preflight_errors"] and plan["preflight"]["A01"]["ok"] is False, plan
                        for override in (False, True):   # the estimates override must not bypass preflight
                            try:
                                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan, args=[override])
                            except WorkflowUpdateFailedError as e:
                                msg = str(e.cause)
                                assert "Cannot approve G1" in msg and "6 reference images" in msg, msg
                            else:
                                raise AssertionError(f"G1 approved with preflight errors (override={override})")
                        st = await h.query(EpisodeWorkflowV2.get_state)
                        assert st["approvals"].get("g1_shot_plan") in (False, None), st["approvals"]
                        assert st["current_gate"] == "G1_SHOT_PLAN", st["current_gate"]
                    else:
                        assert plan["preflight_errors"] == [] and plan["preflight"]["A01"]["ok"] is True, plan
                        res = await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                        assert res == {"status": "approved"}, res
                finally:
                    try:
                        await h.terminate("test done")
                    except Exception:
                        pass


# ------------------------------------------------------------------ T22: the shared assembly rule

def _original_rule(lead, anchor, planned, cap):
    """The lead/anchor rule exactly as submit_still_job_enforced had it at 2a5a109 (inline)."""
    refs = ([lead] if lead else []) + ([anchor] if anchor else []) + list(planned)
    anchor_used = bool(anchor)
    if lead and anchor and len(refs) > cap:
        del refs[1]
        anchor_used = False
    return refs, anchor_used


def test_assemble_still_refs_matches_the_original_rule():
    """T22: assemble_still_refs == the 2a5a109 inline rule for every lead/anchor/planned/cap combination,
    and submit/preflight both use this one function."""
    import inspect
    from hfvg import preflight
    from hfvg.activities import studio_generation
    from hfvg.still_refs import assemble_still_refs

    for lead, anchor, n, cap in itertools.product((None, "L"), (None, "A"), range(8), range(8)):
        planned = [f"p{i}" for i in range(n)]
        assert assemble_still_refs(lead, anchor, planned, cap) == _original_rule(lead, anchor, planned, cap), \
            (lead, anchor, n, cap)
    assert studio_generation.assemble_still_refs is assemble_still_refs
    assert preflight.assemble_still_refs is assemble_still_refs
    src = inspect.getsource(studio_generation.submit_still_job_enforced)
    assert "assemble_still_refs(" in src and "refs_with_anchor, anchor_used = assemble_still_refs(" in src
