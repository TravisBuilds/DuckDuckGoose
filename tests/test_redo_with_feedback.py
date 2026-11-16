"""A G2 redo ("Make this picture again") goes with the owner's note and shows its price first.

POST .../shots/{sid}/retry/estimate prices the redo like its submits are priced (the dry-run figure,
or the provider's own estimate, fail closed) for the stills the workflow says the redo makes
(retry_prompt_plan "stills"). POST .../retry with approved_usd_micros prices it again and refuses a
higher price, or a live redo the balance gate would refuse, before anything is launched; the live
GAP-65 gates are checked before a redo is counted. The workflow reports each turned-down picture's
reason, which GET /api/episodes/{id} shows to who may see the cast. No network, no spend.
"""
import asyncio
import re
import sqlite3

import httpx
import pytest
import respx

from tests.ledger_helpers import set_balance

SECRET = "test-secret-redo-feedback-0123456789abcdefgh"
EP = "ep71"
BASE = "A red ball comes to rest beside a blue cup on a wooden table, soft daylight"
NOTE = "Change requested: the framing is too loose."
ESTIMATE = re.compile(r"https://api\.higgsfield\.ai/estimate/.+")


class _Handle:
    """The episode run as the redo routes see it: retry_prompt_plan and the retry_shot update."""

    def __init__(self, stills=("start",), found=True, base=BASE):
        self.stills, self.found, self.base = stills, found, base
        self.plans, self.updates = [], []

    async def query(self, name, *args, **kw):
        assert name == "retry_prompt_plan", name
        shot_id, append = kw["args"]
        self.plans.append((shot_id, append))
        if not self.found:
            return {"found": False}
        from hfvg.workflows.episode_v2 import amend_prompt
        prompt = amend_prompt(self.base, append)[0] if append else self.base
        plan = {"found": True, "still_prompt": prompt, "clip_prompt": prompt, "clip_model": "kling",
                "shot_meta": None}
        if self.stills is not None:
            plan["stills"] = [{"kind": kind, "prompt": prompt, "resolution": "1k", "quality": "medium",
                               "aspect_ratio": "9:16"} for kind in self.stills]
        return plan

    async def execute_update(self, name, *a, **kw):
        self.updates.append((name, list(kw["args"]) if "args" in kw else list(a)))
        return {"status": "queued", "workflow_id": f"{EP}-shot-S02-retry1"}


class _Client:
    def __init__(self, handle):
        self.handle = handle

    def get_workflow_handle(self, workflow_id):
        return self.handle


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    import api.main as api_main
    from hfvg.studio_db import init_studio_db
    db = str(tmp_path / "studio.db")
    asyncio.run(init_studio_db(db))
    monkeypatch.setattr(api_main, "DATABASE_PATH", db)
    monkeypatch.setenv("ADMIN_SECRET", SECRET)
    monkeypatch.setenv("DRY_RUN", "true")
    handle = _Handle()
    monkeypatch.setattr(api_main, "temporal_client", _Client(handle))
    client = TestClient(api_main.app, headers={"Authorization": f"Bearer {SECRET}"})
    return client, handle, db


def _live(monkeypatch, db, balance_usd="100.00"):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    if balance_usd is not None:
        asyncio.run(set_balance(db, usd=balance_usd, episode_id=EP))


def _audit(db, action):
    with sqlite3.connect(db) as conn:
        return [r[0] for r in conn.execute(
            "SELECT details FROM audit_log WHERE episode_id = ? AND action = ? ORDER BY id", (EP, action))]


def _estimate(client, body=None):
    return client.post(f"/api/episodes/{EP}/shots/S02/retry/estimate", json=body)


def _redo(client, body):
    return client.post(f"/api/episodes/{EP}/shots/S02/retry", json=body)


def test_the_estimate_prices_each_picture_the_redo_makes_and_spends_nothing(api):
    client, handle, _ = api
    res = _estimate(client, {"prompt_append": NOTE})
    assert res.status_code == 200, res.text
    assert res.json() == {"shot_id": "S02", "stills": 1, "kinds": ["start"], "per_still_usd_micros": [60_000],
                          "total_usd_micros": 60_000, "dry_run": True,
                          "summary": "1 placeholder picture, free in dry-run mode"}
    assert handle.plans == [("S02", NOTE)]

    handle.stills = ("start", "end")
    both = _estimate(client, {"prompt_append": NOTE}).json()
    assert (both["kinds"], both["per_still_usd_micros"], both["total_usd_micros"]) == (
        ["start", "end"], [60_000, 60_000], 120_000)
    assert both["summary"] == "2 placeholder pictures, free in dry-run mode"

    handle.stills = ()
    reused = _estimate(client).json()
    assert (reused["stills"], reused["total_usd_micros"]) == (0, 0)
    assert reused["summary"] == "no new picture is paid for: the start picture you uploaded is used again"
    assert handle.plans[-1] == ("S02", None)
    assert handle.updates == []


def test_a_redo_that_cannot_be_priced_is_not_made(api):
    client, handle, _ = api
    handle.stills = None  # a worker that does not say which pictures a redo makes
    for res in (_estimate(client, {"prompt_append": NOTE}),
                _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 1_000_000})):
        assert res.status_code == 503 and "cannot be priced; nothing was spent" in res.json()["detail"], res.text
    assert handle.updates == []
    handle.found = False
    res = _estimate(client)
    assert res.status_code == 404 and res.json()["detail"] == "Shot S02 is not in this episode's shot plan"
    assert handle.updates == []


def test_the_estimate_refuses_a_note_the_redo_would_refuse(api):
    client, handle, _ = api
    handle.base = "x" * 2400
    res = _estimate(client, {"prompt_append": "y " * 100})
    assert res.status_code == 409 and "Preflight refused the amended re-roll before any spend" in res.json()["detail"]
    assert handle.updates == []


def test_a_redo_is_made_for_the_price_the_owner_approved_and_no_more(api):
    client, handle, db = api
    res = _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 50_000})
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == ("The redo now costs $0.06 (you approved $0.05); "
                                    "review the new price and approve again")
    assert handle.updates == [] and _audit(db, "retry_shot") == []

    res = _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 60_000})
    assert res.status_code == 200, res.text
    assert handle.updates == [("retry_shot", ["S02", NOTE])]
    assert handle.plans == [("S02", NOTE), ("S02", NOTE)]  # priced with the note it sends
    [audited] = _audit(db, "retry_shot")
    assert f"prompt_append={NOTE!r}" in audited and "approved_usd_micros=60000" in audited

    handle.stills = ()
    assert _redo(client, {"approved_usd_micros": 0}).status_code == 200
    assert handle.updates[-1] == ("retry_shot", ["S02"])


def test_a_redo_without_an_approved_price_is_sent_as_before(api):
    client, handle, _ = api
    handle.stills = None
    assert _redo(client, {"prompt_append": NOTE}).status_code == 200
    assert _redo(client, None).status_code == 200
    assert handle.updates == [("retry_shot", ["S02", NOTE]), ("retry_shot", ["S02"])]


@respx.mock
def test_live_redo_prices_come_from_the_provider_and_fail_closed(api, monkeypatch):
    client, handle, db = api
    _live(monkeypatch, db)
    route = respx.post(ESTIMATE).mock(return_value=httpx.Response(500, json={"error": "down"}))
    for res in (_estimate(client, {"prompt_append": NOTE}),
                _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 1_000_000})):
        assert res.status_code == 503 and "did not give a price" in res.json()["detail"], res.text
    assert route.call_count >= 2 and handle.updates == []

    route.mock(return_value=httpx.Response(200, json={"credits": "1.5", "usd": "0.07"}))
    estimate = _estimate(client, {"prompt_append": NOTE}).json()
    assert estimate == {"shot_id": "S02", "stills": 1, "kinds": ["start"], "per_still_usd_micros": [70_000],
                        "total_usd_micros": 70_000, "dry_run": False,
                        "summary": "1 new picture, about $0.07, charged to this video's budget"}
    assert route.calls.last.request.read().decode().count(NOTE) == 1
    res = _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 70_000})
    assert res.status_code == 200, res.text
    assert handle.updates == [("retry_shot", ["S02", NOTE])]


@respx.mock
def test_a_live_redo_waits_for_the_balance_gate(api, monkeypatch):
    client, handle, db = api
    _live(monkeypatch, db, balance_usd="0.05")
    respx.post(ESTIMATE).mock(return_value=httpx.Response(200, json={"credits": "1.5", "usd": "0.07"}))
    res = _redo(client, {"prompt_append": NOTE, "approved_usd_micros": 70_000})
    assert res.status_code == 409, res.text
    assert res.json()["detail"].startswith("Balance check failed: higgsfield balance insufficient")
    assert handle.updates == []


def test_a_live_redo_is_refused_before_it_is_counted_without_a_recorded_balance(api, monkeypatch):
    client, handle, db = api
    _live(monkeypatch, db, balance_usd=None)
    res = _redo(client, {"prompt_append": NOTE})
    assert res.status_code == 409, res.text
    assert res.json()["detail"].startswith(
        "Picture redo refused before it was sent (GAP-65: nothing counted, no spend): "
        "GC.01: no manual Higgsfield balance recorded")
    assert handle.updates == [] and handle.plans == []


# ---------------------------------------------------------------- the workflow side


class _Log:
    def info(self, *a, **k):
        pass

    warning = error = info


def _workflow(monkeypatch):
    import hfvg.workflows.episode_v2 as mod
    monkeypatch.setattr(mod.workflow, "logger", _Log())
    wf = mod.EpisodeWorkflowV2()
    wf.state.current_gate = "G2_SCENE_STILLS"
    wf.parsed_beatmap = {"shots": [
        {"shot_id": "S01", "prompt": "A red ball rolls across a wooden table", "params": {"model": "kling"}},
        {"shot_id": "S02", "prompt": "A blue cup on a wooden table", "end_prompt": "The cup, tipped over",
         "params": {"end_frame": True, "resolution": "2k", "quality": "high", "aspect_ratio": "16:9"}},
        {"shot_id": "S03", "prompt": "A paper boat on a puddle", "params": {}},
    ]}
    wf.pre_approved_stills = {"S03": {"source": "upload", "path": "/tmp/s03.png"}}
    return wf


def test_the_plan_of_a_redo_lists_the_paid_stills_its_child_would_submit(monkeypatch):
    wf = _workflow(monkeypatch)
    still = {"resolution": "1k", "quality": "medium", "aspect_ratio": "9:16"}
    plan = wf.retry_prompt_plan("S01", NOTE)
    assert plan["stills"] == [{"kind": "start", "prompt": f"A red ball rolls across a wooden table {NOTE}", **still}]
    big = {"resolution": "2k", "quality": "high", "aspect_ratio": "16:9"}
    assert wf.retry_prompt_plan("S02")["stills"] == [
        {"kind": "start", "prompt": "A blue cup on a wooden table", **big},
        {"kind": "end", "prompt": "The cup, tipped over", **big}]
    # retry_shot hands the child the pre-approved start still again: nothing paid for
    assert wf.retry_prompt_plan("S03")["stills"] == []
    # unless the owner turned that still down at G2: then the redo makes (and pays for) a new one
    wf.state.shot_decisions["S03"] = "rejected"
    assert wf.retry_prompt_plan("S03")["stills"] == [{"kind": "start", "prompt": "A paper boat on a puddle", **still}]
    assert wf.retry_prompt_plan("S09") == {"found": False}


async def test_the_state_reports_why_a_picture_was_turned_down_until_it_is_redone(monkeypatch):
    from unittest.mock import MagicMock
    wf = _workflow(monkeypatch)
    wf.shot_results = {sid: MagicMock(id=f"{EP}-shot-{sid}") for sid in ("S01", "S02")}
    assert wf.get_state()["rejection_reasons"] == {}
    await wf.reject_shot("S01", "the framing is too loose")
    await wf.reject_shot("S02", "the cup should be red")
    assert wf.get_state()["rejection_reasons"] == {"S01": "the framing is too loose", "S02": "the cup should be red"}
    await wf.approve_shot("S02")
    assert wf.get_state()["rejection_reasons"] == {"S01": "the framing is too loose"}
