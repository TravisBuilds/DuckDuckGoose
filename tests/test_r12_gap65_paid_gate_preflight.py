"""GAP-65 (ep05v2 r12, 2026-10-28 16:20 CET): the submit-time live gates (smoke test < 24 h, GC.01 manual
balance < 24 h) were only checked inside the child's paid submit. The workflow had already counted the
attempt when the update was accepted, so two refusals with no spend used up V04's recompose grant 2 and
clip retry 1 and V18's recompose 1. The retry-clip / recompose / recompose-approve routes now check the
same gates first and answer 409 with nothing sent to the workflow. No network, no spend."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from tests.lint_helpers import lint_ok
from tests.test_retry_prompt_preflight import client  # noqa: F401  (fixture)


class _Handle:
    def __init__(self):
        self.updates = []

    async def query(self, name, *args, **kw):
        # a lint-clean clip prompt, so GAP-66's clip preflight (same route) passes when the gates do
        return {"found": True, "still_prompt": "x", "clip_prompt": lint_ok("Scene", 0), "clip_model": "kling",
                "shot_meta": {"count": 0, "exterior": False, "water": False, "characters": []}}

    async def execute_update(self, name, *a, **kw):
        self.updates.append(name)
        return {"status": "queued", "workflow_id": "ep77-shot-A01-x", "retry": 1, "job_id": "j"}


class _Client:
    def __init__(self, h):
        self.h = h

    def get_workflow_handle(self, wid):
        return self.h


def _smoke(db, hours_ago, passed=True):
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")

    async def go():
        async with aiosqlite.connect(db) as c:
            await c.execute("INSERT OR IGNORE INTO episodes (episode_id) VALUES ('ep77')")
            await c.execute("UPDATE episodes SET smoke_test_result=? WHERE episode_id='ep77'",
                            (json.dumps({"passed": passed, "timestamp": ts}),))
            await c.commit()
    asyncio.run(go())


def _balance(db):
    from hfvg.budget import BudgetLedger
    asyncio.run(BudgetLedger(db).set_manual_balance(100_000_000, set_by="test", note="t"))


@pytest.fixture
def live(client, monkeypatch):  # noqa: F811
    api_main, c = client
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.delenv("SMOKE_TEST_OVERRIDE", raising=False)
    monkeypatch.delenv("HFVG_BALANCE_MAX_AGE_HOURS", raising=False)
    h = _Handle()
    monkeypatch.setattr(api_main, "temporal_client", _Client(h))
    return api_main, c, h, api_main.DATABASE_PATH


def test_gate_problem_reports_stale_smoke_then_stale_balance(live, monkeypatch):
    api_main, _, _, db = live
    _smoke(db, 27.6)
    _balance(db)
    p = asyncio.run(api_main._paid_gate_problem("ep77"))
    assert p and "Smoke test too old" in p
    _smoke(db, 0.1)
    assert asyncio.run(api_main._paid_gate_problem("ep77")) is None
    monkeypatch.setenv("HFVG_BALANCE_MAX_AGE_HOURS", "0.0000001")  # every recorded balance is now stale
    p = asyncio.run(api_main._paid_gate_problem("ep77"))
    assert p and p.startswith("GC.01") and "record it again" in p


def test_no_balance_recorded_is_refused(live):
    api_main, _, _, db = live
    _smoke(db, 0.1)
    p = asyncio.run(api_main._paid_gate_problem("ep77"))
    assert p and "no manual Higgsfield balance recorded" in p


def test_dry_run_has_no_gates(live, monkeypatch):
    api_main, _, _, db = live
    monkeypatch.setenv("DRY_RUN", "true")
    assert asyncio.run(api_main._paid_gate_problem("ep77")) is None


def test_smoke_override_skips_only_the_smoke_gate(live, monkeypatch):
    api_main, _, _, db = live
    _smoke(db, 30)
    _balance(db)
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    assert asyncio.run(api_main._paid_gate_problem("ep77")) is None
    monkeypatch.setenv("HFVG_BALANCE_MAX_AGE_HOURS", "0.0000001")
    assert asyncio.run(api_main._paid_gate_problem("ep77")).startswith("GC.01")


def test_retry_clip_refused_before_the_update_when_smoke_is_stale(live):
    api_main, c, h, db = live
    _smoke(db, 27.6)
    _balance(db)
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t", "duration_s": 5})
    assert r.status_code == 409 and "GAP-65" in r.json()["detail"] and "Smoke test too old" in r.json()["detail"]
    assert h.updates == []  # nothing counted in the workflow
    _smoke(db, 0.1)
    r = c.post("/api/episodes/ep77/shots/A01/retry-clip", json={"reason": "t", "duration_s": 5})
    assert r.status_code == 200, r.text
    assert h.updates == ["retry_clip"]


def test_recompose_refused_before_the_update_when_balance_is_stale(live, monkeypatch):
    api_main, c, h, db = live
    _smoke(db, 0.1)
    _balance(db)
    monkeypatch.setenv("HFVG_BALANCE_MAX_AGE_HOURS", "0.0000001")
    r = c.post("/api/episodes/ep77/shots/A01/recompose", json={"reason": "t", "prompt": "p", "ref_keys": ["rowan"]})
    assert r.status_code == 409 and "GC.01" in r.json()["detail"] and h.updates == []


def test_recompose_approval_gated_but_reject_is_not(live, monkeypatch):
    api_main, c, h, db = live
    _smoke(db, 27.6)
    _balance(db)
    url = "/api/episodes/ep77/shots/A01/recompose/decision"
    r = c.post(url, json={"approve": True, "reason": "ok"})
    assert r.status_code == 409 and "Recompose approval" in r.json()["detail"] and h.updates == []
    r = c.post(url, json={"approve": False, "reason": "no"})  # a reject spends nothing: never gated
    assert r.status_code == 200 and h.updates == ["recompose_decision"], r.text
