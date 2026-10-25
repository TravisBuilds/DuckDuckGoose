"""End-only backfill: EpisodeWorkflowV2.retry_end_still + resolve_landed_start_still.

The ep05 A02 r3 run landed its start still, then the _end failed (6 refs), so the shot workflow
completed as failed and the episode parked at G2. retry_end_still must submit ONLY the _end still
(same attempt, lead = the landed start still), never the start, never approve G2, and never resubmit
an _end that was already tried with a lead ref.
"""
import asyncio
import time
from datetime import timedelta

import aiosqlite
import httpx
import pytest
import respx
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

START = "Exactly 0 animals in frame. Real-time, no slow motion. START frame probe."
END = "Exactly 0 animals in frame. Real-time, no slow motion. END frame probe."
NEW_END = "Exactly 0 animals in frame. Real-time, no slow motion. NEW end frame probe."
START_URL = "https://d3u0tzju9qaucj.cloudfront.net/out/a02-r3-start.jpeg"


def _shot():
    return {"shot_id": "A02", "prompt": START, "end_prompt": END, "duration": 3.0, "params": {"end_frame": True},
            "refs": ["refs/duck.png", "refs/family.png", "refs/subject01.png", "refs/pool.png"],
            "shot_meta": {"count": 0, "characters": [], "wet": False}}


def _activities(log, *, fail_start=False, resolve_error=None):
    from tests.test_pr10_216_fixes import _activities as base

    end_fails = {"n": 0}

    async def submit_still(ep, sid, prompt, version, refs, res, q, ar, tier, meta, missing, attempt, lead=None):
        log.append(("submit_still", sid, prompt, attempt, lead, list(refs or [])))
        suffix = f"-r{attempt - 1}" if attempt > 1 else f"-v{version}"
        lead_sfx = "-lead7b9bdd55" if lead else ""
        n = sum(1 for e in log if e[0] == "submit_still" and e[1] == sid)
        return {"job_id": f"still-{ep}-{sid}{suffix}{lead_sfx}-{n}", "line_name": "L", "reserved_amount": 1}

    async def poll(job_id, *a):
        if fail_start and "_end" not in job_id:
            return {"status": "failed", "error": "Generation failed (start)"}
        if "_end" in job_id and end_fails["n"] == 0:
            end_fails["n"] += 1   # the original _end fails like A02_end-r3 (8b427df8...)
            return {"status": "failed", "error": "Generation failed (raw 8b427df8)"}
        log.append(("landed", job_id))
        return {"status": "completed", "output_url": START_URL if "_end" not in job_id else f"fake://{job_id}.jpg",
                "cost": 1}

    async def resolve(ep, sid, attempt, version):
        log.append(("resolve", ep, sid, attempt, version))
        if resolve_error:
            from temporalio.exceptions import ApplicationError
            raise ApplicationError(resolve_error, type="EndStillBackfillRefused", non_retryable=True)
        return {"url": START_URL, "job_id": f"still-{ep}-{sid}-r{attempt - 1}-1", "provider_job_id": "3fe9f971"}

    def mk(name, fn):
        async def w(*a):
            return await fn(*a)
        return activity.defn(name=name)(w)

    keep = [a for a in base(log, _shot(), escalate=True)
            if getattr(a, "__temporal_activity_definition").name not in ("submit_still_job_enforced", "poll_job_status")]
    return keep + [mk("submit_still_job_enforced", submit_still), mk("poll_job_status", poll),
                   mk("resolve_landed_start_still", resolve)]


async def _parked(tmp_path, monkeypatch, log, body, **kw):
    """Run ep05 (A02, attempt override 4 like r3) to G2 with A02 failed, then run body(h, state)."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow

    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="endbf", workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(log, **kw)):
            h = await env.client.start_workflow(
                EpisodeWorkflowV2.run, args=["ep05", None, True, ["A02"], 1000, {"A02": 4}],
                id="ep05", task_queue="endbf")

            async def state():
                return await asyncio.wait_for(h.query(EpisodeWorkflowV2.get_state, rpc_timeout=timedelta(seconds=2)), 3)

            async def wait(pred, budget=15):
                end, st = time.time() + budget, None
                while time.time() < end:
                    try:
                        st = await state()
                        if pred(st):
                            return st
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                raise AssertionError(f"condition not reached: {st}")
            try:
                await wait(lambda s: s.get("current_gate") == "G1_SHOT_PLAN")
                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                await wait(lambda s: s.get("current_gate") == "G2_SCENE_STILLS" and s.get("failed_shots"))
                await body(h, wait)
            finally:
                for wid in ("ep05", "ep05-shot-A02", "ep05-shot-A02-end1", "ep05-shot-A02-end2"):
                    try:
                        await env.client.get_workflow_handle(wid).terminate("test done")
                    except Exception:
                        pass


async def _refused(h, *args):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    try:
        res = await h.execute_update(EpisodeWorkflowV2.retry_end_still, args=list(args))
    except WorkflowUpdateFailedError as e:
        return str(e.cause) if e.cause else str(e)
    raise AssertionError(f"retry_end_still{args} was accepted: {res}")


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_end_still_submits_only_the_end_still_with_the_landed_start_as_lead(tmp_path, monkeypatch):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    log = []

    async def body(h, wait):
        st = await wait(lambda s: True)
        assert st["failed_shots"][0]["reason"].startswith("End still failed"), st
        n_before = len(log)
        res = await h.execute_update(EpisodeWorkflowV2.retry_end_still, args=["A02", NEW_END])
        assert res["workflow_id"] == "ep05-shot-A02-end1" and res["attempt"] == 4, res
        assert res["start_still_url"] == START_URL, res
        st = await wait(lambda s: not s.get("pending_retries") and any(e[0] == "landed" and "_end" in e[1] for e in log))
        new = log[n_before:]
        subs = [e for e in new if e[0] == "submit_still"]
        assert [e[1] for e in subs] == ["A02_end"], f"backfill must submit ONLY the _end still: {subs}"
        _, _, prompt, attempt, lead, refs = subs[0]
        assert prompt == NEW_END, f"_end must use the supplied end_prompt: {prompt!r}"
        assert attempt == 4, f"same attempt as the parked run (r3 ids): {attempt}"
        assert lead == START_URL, f"_end lead ref must be the landed start still: {lead}"
        assert refs == _shot()["refs"], refs
        assert ("resolve", "ep05", "A02", 4, 4) in new, new
        assert not st["failed_shots"], st
        assert st["approvals"]["g2_scene_stills"] is False and st["current_gate"] == "G2_SCENE_STILLS", st
        assert not any(e[0] == "submit_clip" for e in log), "clip started without G2"
        # a second backfill for the same shot is refused (shot is no longer failed)
        assert "not in failed_shots" in await _refused(h, "A02")
        assert sum(1 for e in log if e[0] == "submit_still" and e[1] == "A02") == 1, "start still resubmitted"

    await _parked(tmp_path, monkeypatch, log, body)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_end_still_without_end_prompt_uses_plan_end_prompt(tmp_path, monkeypatch):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    log = []

    async def body(h, wait):
        n_before = len(log)
        await h.execute_update(EpisodeWorkflowV2.retry_end_still, args=["A02"])
        await wait(lambda s: not s.get("pending_retries") and any(e[0] == "landed" and "_end" in e[1] for e in log))
        subs = [e for e in log[n_before:] if e[0] == "submit_still"]
        assert [(e[1], e[2]) for e in subs] == [("A02_end", END)], subs

    await _parked(tmp_path, monkeypatch, log, body)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_end_still_refused_when_start_failed(tmp_path, monkeypatch):
    log = []

    async def body(h, wait):
        st = await wait(lambda s: True)
        assert not st["failed_shots"][0]["reason"].startswith("End still failed"), st
        msg = await _refused(h, "A02")
        assert "another reason" in msg, msg
        assert not any(e[0] == "resolve" for e in log), "resolve ran for a refused backfill"
        assert sum(1 for e in log if e[0] == "submit_still") == 1, log

    await _parked(tmp_path, monkeypatch, log, body, fail_start=True)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_retry_end_still_resolve_refusal_spends_nothing_and_keeps_shot_failed(tmp_path, monkeypatch):
    log = []

    async def body(h, wait):
        n_before = len(log)
        msg = await _refused(h, "A02")
        assert "already submitted with a lead ref" in msg, msg
        st = await wait(lambda s: True)
        assert not st["pending_retries"], f"pending retry left behind: {st}"
        assert st["failed_shots"] and st["failed_shots"][0]["shot_id"] == "A02", st
        assert not [e for e in log[n_before:] if e[0] == "submit_still"], "submitted despite resolve refusal"
        assert "unknown shot" not in msg
        assert "not in failed_shots" in await _refused(h, "A09")

    await _parked(tmp_path, monkeypatch, log, body,
                  resolve_error="A02_end was already submitted with a lead ref for this attempt")


# ------------------------------------------------------------------ resolve_landed_start_still (activity)
async def _ledger(tmp_path, monkeypatch, rows, provider_rows):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    db = str(tmp_path / "s.db")
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    async with aiosqlite.connect(db) as c:
        for job_id, shot, status in rows:
            await c.execute("INSERT INTO job_ledger (job_id, episode_id, shot_id, model, tier, line_name, "
                            "usd_micros, status, verdict, used, created_at, updated_at) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (job_id, "ep05", shot, "xai/grok-imagine-image-2.0", "draft", "L2_drafts", 60000, status,
                             None, None, "t", "t"))
        for job_id, pid in provider_rows:
            await c.execute("INSERT INTO provider_jobs VALUES (?,?,?,?)", (job_id, pid, "higgsfield", "t"))
        await c.commit()
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")


R3 = [("still-ep05-A02-r2-anchor336ba082", "A02", "committed"),      # older attempt (r2): ignored
      ("still-ep05-A02-r3-anchor336ba082", "A02", "committed"),      # the landed r3 start
      ("still-ep05-A02_end-r3-anchor336ba082", "A02_end", "released")]
R3P = [("still-ep05-A02-r2-anchor336ba082", "626c6461"), ("still-ep05-A02-r3-anchor336ba082", "3fe9f971"),
       ("still-ep05-A02_end-r3-anchor336ba082", "8b427df8")]


async def _resolve(*a):
    from temporalio.exceptions import ApplicationError
    from hfvg.activities.studio_generation import resolve_landed_start_still
    try:
        return await resolve_landed_start_still(*a), None
    except ApplicationError as e:
        return None, str(e)


@pytest.mark.asyncio
@respx.mock
async def test_resolve_landed_start_still_finds_exactly_this_attempts_start(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, R3, R3P)
    st = respx.get("https://api.higgsfield.ai/requests/3fe9f971/status").mock(
        return_value=httpx.Response(200, json={"status": "completed", "images": [{"url": START_URL}]}))
    respx.get("https://api.higgsfield.ai/requests/626c6461/status").mock(   # rejected r2 start
        return_value=httpx.Response(200, json={"status": "completed", "images": [{"url": "https://cdn/r2.jpeg"}]}))
    out, err = await _resolve("ep05", "A02", 4, 4)
    assert err is None, err
    assert out == {"url": START_URL, "job_id": "still-ep05-A02-r3-anchor336ba082", "provider_job_id": "3fe9f971"}, out
    assert st.call_count == 1
    posts = [c for c in respx.calls if c.request.method != "GET"]
    assert not posts, f"resolve must be read-only (GET status only): {posts}"


@pytest.mark.asyncio
@respx.mock
async def test_resolve_refuses_when_an_end_with_lead_was_already_submitted(tmp_path, monkeypatch):
    rows = R3 + [("still-ep05-A02_end-r3-lead7b9bdd55", "A02_end", "released")]
    await _ledger(tmp_path, monkeypatch, rows, R3P + [("still-ep05-A02_end-r3-lead7b9bdd55", "9d0e")])
    st = respx.get(url__regex=r".*/requests/.*/status").mock(return_value=httpx.Response(200, json={"status": "completed"}))
    out, err = await _resolve("ep05", "A02", 4, 4)
    assert out is None and err and "already submitted with a lead ref" in err, (out, err)
    assert st.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_resolve_refuses_when_start_not_committed_or_not_completed(tmp_path, monkeypatch):
    await _ledger(tmp_path, monkeypatch, [("still-ep05-A02-r3-anchor336ba082", "A02", "released")], R3P)
    ok = respx.get("https://api.higgsfield.ai/requests/3fe9f971/status").mock(
        return_value=httpx.Response(200, json={"status": "completed", "images": [{"url": START_URL}]}))
    out, err = await _resolve("ep05", "A02", 4, 4)
    assert out is None and err and "exactly one committed start still" in err, (out, err)

    second = tmp_path / "completed-check"
    second.mkdir()
    await _ledger(second, monkeypatch, R3, R3P)
    ok.mock(return_value=httpx.Response(200, json={"status": "failed", "error": "Generation failed"}))
    out, err = await _resolve("ep05", "A02", 4, 4)
    assert out is None and err and "not completed" in err, (out, err)


def test_worker_registers_resolve_landed_start_still():
    from hfvg.worker import PRODUCTION_ACTIVITIES
    names = {getattr(a, "__temporal_activity_definition").name for a in PRODUCTION_ACTIVITIES}
    assert "resolve_landed_start_still" in names, "retry_end_still would hang: activity not registered on the worker"
