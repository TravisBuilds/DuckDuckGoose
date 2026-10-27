"""Budget lines made by an earlier start keep stale caps (INSERT OR IGNORE); sync_episode_lines
re-applies the policy/env caps (never below what a line holds, never over the episode stop)."""

import json
from pathlib import Path

import aiosqlite
import pytest

LINES = {"L1_refs": 0.25, "L2_drafts": 2.5, "L3_final_stills": 0.25, "L4_video": 32.5}


def _policy(tmp_path, lines=None, episode="ep05full"):
    example = sorted((Path(__file__).resolve().parents[1] / "examples").glob("*/HARNESS-GATES.json"))[0]
    policy = json.loads(example.read_text())
    if lines is not None:
        b = policy.setdefault("budgets", {})
        b[episode] = {"higgsfield": {"lines": {k: 1 for k in lines}}}
        b.setdefault("usd", {})[f"{episode}_lines_app_usd"] = lines
    path = tmp_path / f"policy-{len(list(tmp_path.glob('policy-*')))}.json"
    path.write_text(json.dumps(policy))
    return str(path)


async def _lines(db_path, ep="ep05full"):
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT line_name, cap_usd_micros, stop_usd_micros, spent_usd_micros FROM budget_lines "
            "WHERE episode_id=?", (ep,))).fetchall()
    return {r[0]: r[1:] for r in rows}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODE_CAP_USD", "40")
    monkeypatch.setenv("EPISODE_STOP_PERCENT", "95")
    monkeypatch.setenv("REVISION_RESERVE_USD", "1")
    return str(tmp_path / "s.db")


async def _failed_start_then_spend(tmp_path, monkeypatch, db):
    from hfvg.budget import BudgetLedger
    monkeypatch.setenv("HFVG_GATE_POLICY", _policy(tmp_path))  # no ep05full lines -> defaults
    ledger = BudgetLedger(db)
    await ledger.init_episode_budget("ep05full")
    assert (await _lines(db))["L4_video"][:2] == (16_000_000, 15_200_000)
    assert await ledger.reserve("ep05full", "L2_drafts", 1_204_000, job_id="j1")
    monkeypatch.setenv("HFVG_GATE_POLICY", _policy(tmp_path, LINES))
    return BudgetLedger(db)


@pytest.mark.asyncio
async def test_sync_applies_policy_caps_to_lines_from_an_earlier_start(tmp_path, monkeypatch, env):
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    result = await ledger.sync_episode_lines("ep05full", reason="Operator 04:36 CET")
    lines = await _lines(env)
    assert lines["L4_video"][:2] == (32_500_000, 30_875_000)  # >= $10.528 first pass + $20 retries
    assert lines["L2_drafts"][:2] == (2_500_000, 2_375_000)
    assert lines["L7_revision"][:2] == (1_000_000, 1_000_000)
    assert {c["line"] for c in result["changed"]} == {"L1_refs", "L2_drafts", "L3_final_stills", "L4_video"}
    async with aiosqlite.connect(env) as db:
        n = (await (await db.execute(
            "SELECT COUNT(*) FROM budget_transactions WHERE txn_type='cap_sync' AND reason LIKE '%Operator%'")).fetchone())[0]
    assert n == len(result["changed"])
    # idempotent
    again = await ledger.sync_episode_lines("ep05full", reason="again")
    assert again["changed"] == []


@pytest.mark.asyncio
async def test_sync_never_goes_below_what_a_line_holds(tmp_path, monkeypatch, env):
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    monkeypatch.setenv("HFVG_GATE_POLICY", _policy(tmp_path, dict(LINES, L2_drafts=1.0)))  # stop 0.95 < 1.204 held
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(env)  # the policy is read when a ledger is made (API: per request)
    before = await _lines(env)
    with pytest.raises(ValueError, match="already holds"):
        await ledger.sync_episode_lines("ep05full", reason="x")
    assert await _lines(env) == before  # strict: nothing changed
    res = await ledger.sync_episode_lines("ep05full", reason="x", strict=False)
    assert [s["line"] for s in res["skipped"]] == ["L2_drafts"]
    assert (await _lines(env))["L4_video"][0] == 32_500_000


@pytest.mark.asyncio
async def test_sync_refuses_caps_over_the_episode_stop(tmp_path, monkeypatch, env):
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    monkeypatch.setenv("HFVG_GATE_POLICY", _policy(tmp_path, dict(LINES, L4_video=40)))
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(env)
    with pytest.raises(ValueError, match="episode stop"):
        await ledger.sync_episode_lines("ep05full", reason="x")


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(tmp_path, monkeypatch, env):
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    before = await _lines(env)
    res = await ledger.sync_episode_lines("ep05full", reason="x", dry_run=True)
    assert res["changed"] and await _lines(env) == before


@pytest.mark.asyncio
async def test_restart_init_applies_the_policy(tmp_path, monkeypatch, env):
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    await ledger.init_episode_budget("ep05full")  # second start of the workflow
    assert (await _lines(env))["L4_video"][:2] == (32_500_000, 30_875_000)


@pytest.mark.asyncio
async def test_api_route(tmp_path, monkeypatch, env):
    from fastapi.testclient import TestClient
    import api.main as api_main

    from hfvg.studio_db import init_studio_db
    await init_studio_db(env)
    ledger = await _failed_start_then_spend(tmp_path, monkeypatch, env)
    monkeypatch.setattr(api_main, "DATABASE_PATH", env)
    monkeypatch.setenv("ADMIN_SECRET", "test-secret-sync-0123456789abcdefghijklmn")
    client = TestClient(api_main.app, headers={"Authorization": "Bearer test-secret-sync-0123456789abcdefghijklmn"})
    r = client.post("/api/budget/ep05full/lines/sync", json={"reason": "dry", "dry_run": True})
    assert r.status_code == 200 and r.json()["changed"]
    assert (await _lines(env))["L4_video"][0] == 16_000_000
    r = client.post("/api/budget/ep05full/lines/sync", json={"reason": "Operator 04:36 CET"})
    assert r.status_code == 200
    assert (await _lines(env))["L4_video"][0] == 32_500_000
    async with aiosqlite.connect(env) as db:
        audit = (await (await db.execute(
            "SELECT details FROM audit_log WHERE action='budget_lines_sync'")).fetchall())
    assert len(audit) == 1 and "Operator 04:36 CET" in audit[0][0]
    assert client.post("/api/budget/../lines/sync", json={"reason": "abc"}).status_code in (400, 404)
    monkeypatch.setenv("HFVG_GATE_POLICY", _policy(tmp_path, dict(LINES, L2_drafts=1.0)))
    assert client.post("/api/budget/ep05full/lines/sync", json={"reason": "abc"}).status_code == 409
