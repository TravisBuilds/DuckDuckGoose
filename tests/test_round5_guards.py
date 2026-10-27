"""Round 5 guards (G-UI-1 / G-AUTH-1): signed session tokens, the app-wide auth guard, start refusal
for an invalid uploaded shots.json, the G_FINAL_CUT approve pre-check, final-cut reject, cap raise,
per-shot G2 approve, episode-id format, the approve lookup query, and one real-Temporal dry run of
the final-gate actions the studio episode page drives."""
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

ADMIN_SECRET = "a" * 32
REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "web" / "tests" / "fixtures" / "e2e"


@pytest.fixture(autouse=True)
def _fresh_api_module():
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


@pytest.fixture
async def api(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db

    db = str(tmp_path / "studio.db")
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "data" / "episodes"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.chdir(tmp_path)
    import api.main as api_main

    api_main.temporal_client = MagicMock()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://test")
    yield api_main, client
    await client.aclose()


async def _login(client) -> str:
    r = await client.post("/api/login", json={"admin_secret": ADMIN_SECRET})
    assert r.status_code == 200, r.text
    return client.cookies["studio_admin_token"]


def _episode_handle(api_main, state: dict):
    """temporal_client whose lookup yields the episode workflow and whose handle returns `state`."""
    handle = MagicMock()
    handle.query = AsyncMock(return_value=state)
    handle.signal = AsyncMock()
    handle.execute_update = AsyncMock(return_value={"success": True})
    queries = []

    def list_workflows(query, *a, **k):
        queries.append(query)

        async def gen():
            yield SimpleNamespace(id="ep42")

        return gen()

    client = MagicMock()
    client.list_workflows = list_workflows
    client.get_workflow_handle = MagicMock(return_value=handle)
    api_main.temporal_client = client
    return handle, queries


# ---------------------------------------------------------------- session tokens (API side)

def test_session_token_vectors_match_the_web_verifier(monkeypatch):
    """Same fixed vectors as web/lib/session.test.mjs: the Edge middleware and the API agree."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    import api.main as api_main

    assert api_main.sign_session_token(4102444800, "A" * 24) == (
        "v1.4102444800.AAAAAAAAAAAAAAAAAAAAAAAA.RTY8v1ZRIjSHwEy-wdfVxWuBx8YWnZm0LUHw_dNwkU8")
    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    assert api_main.sign_session_token(4102444800, "A" * 24) == (
        "v1.4102444800.AAAAAAAAAAAAAAAAAAAAAAAA.QPBAqzoV2MMafWhabFd-fD2qLD_IY5kE-HIh9VkU1cA")


def test_session_signature_check_rejects_forgery_expiry_and_malformed(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    import api.main as api_main

    now = 1_800_000_000
    good = api_main.sign_session_token(now + 60, "N" * 24)
    assert api_main.check_session_signature(good, now=now) is True
    exp, nonce, sig = good.split(".")[1:]
    flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    bad = [
        f"v1.{exp}.{nonce}.{flipped}",                      # tampered signature
        f"v1.{int(exp) + 3600}.{nonce}.{sig}",              # extended expiry, old signature
        api_main.sign_session_token(now + 60, "N" * 24, key=b"k" * 32),  # other key
        "0123456789abcdef" * 4,                              # pre-Round-5 random token format
        f"v1.{exp}.{nonce}.{sig}\n",                         # trailing newline
        f"v1.{exp}.{nonce}",                                 # unsigned
        f"v2.{exp}.{nonce}.{sig}",
        "",
    ]
    for token in bad:
        assert api_main.check_session_signature(token, now=now) is False, token
    assert api_main.check_session_signature(good, now=now + 61) is False  # expired
    monkeypatch.delenv("ADMIN_SECRET")
    assert api_main.check_session_signature(good, now=now) is False  # no key: fail closed


@pytest.mark.asyncio
async def test_api_refuses_forged_expired_and_revoked_sessions(api):
    api_main, client = api
    token = await _login(client)
    assert (await client.get("/api/models")).status_code == 200

    forged = api_main.sign_session_token(int(time.time()) + 600, "F" * 24, key=b"x" * 32)
    unstored = api_main.sign_session_token(int(time.time()) + 600, "U" * 24)  # valid signature, no DB row
    expired = api_main.sign_session_token(int(time.time()) - 1, "E" * 24)
    import aiosqlite
    async with aiosqlite.connect(api_main.DATABASE_PATH) as db:  # row says valid: signature expiry wins
        await db.execute("INSERT INTO sessions (token, created_at, expires_at) VALUES (?, datetime('now'), "
                         "datetime('now', '+1 day'))", (expired,))
        await db.commit()
    for bad in (forged, unstored, expired, "0123456789abcdef" * 4):
        client.cookies.clear()
        client.cookies.set("studio_admin_token", bad)
        assert (await client.get("/api/models")).status_code == 401, bad

    client.cookies.clear()
    client.cookies.set("studio_admin_token", token)
    out = await client.post("/api/logout")
    assert out.status_code == 200
    client.cookies.clear()
    client.cookies.set("studio_admin_token", token)
    assert (await client.get("/api/models")).status_code == 401  # revoked server-side


def test_login_compares_secret_in_constant_time():
    src = (REPO / "api" / "main.py").read_text()
    login = src[src.index('@app.post("/api/login")'):src.index('@app.post("/api/logout")')]
    assert "secrets.compare_digest(" in login and "!= admin_secret" not in login


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_web_middleware_session_verifier_unit_tests():
    """Runs web/lib/session.test.mjs (the Edge middleware's HMAC + expiry verifier) under pytest so
    red-green mutations of web/lib/session.mjs are caught by the Python harness too."""
    proc = subprocess.run(["node", "--test", "lib/session.test.mjs"], cwd=REPO / "web",
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]


def test_web_middleware_uses_the_verifier():
    """web/middleware.ts must verify the token (not just test that a cookie is present)."""
    src = (REPO / "web" / "middleware.ts").read_text()
    assert "verifySessionToken(" in src and "sessionKeyFromEnv(" in src
    assert "result.ok" in src or ".ok)" in src


# ---------------------------------------------------------------- episode id

def test_episode_id_format_is_exact(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    import api.main as api_main
    from fastapi import HTTPException
    from pydantic import ValidationError

    for good in ("ep01", "ep99", "ep00", "ep05full", "ep05b", "ep05full2"):
        result = api_main.validate_episode_id(good)
        assert result == good, f"validate_episode_id rejected valid ID {good!r}"
        api_main.StartEpisodeRequest(episode_id=good)
    for bad in ("ep01\n", "ep\u0661\u0662", "ep001", "ep1", "EP01", "ep01-shot-A01", "../ep01", "",
                "ep05Full", "ep05_full", "ep05-full", "ep05full/..", "ep05.full", "ep05full\n",
                "ep05abcdefghijklm", "ep05\u00e9"):
        try:
            api_main.validate_episode_id(bad)
            refused = False
        except HTTPException as e:
            refused = e.status_code == 400
        assert refused, f"validate_episode_id accepted {bad!r}"
        try:
            api_main.StartEpisodeRequest(episode_id=bad)
            refused = False
        except ValidationError:
            refused = True
        assert refused, f"StartEpisodeRequest accepted {bad!r}"


# ---------------------------------------------------------------- start refusal

@pytest.mark.asyncio
async def test_start_refuses_invalid_uploaded_shots_before_starting(api, tmp_path):
    api_main, client = api
    await _login(client)
    start = AsyncMock(return_value=MagicMock(id="ep42", result_run_id="r1", run_id="r1"))
    api_main.temporal_client = MagicMock(start_workflow=start)
    ep_dir = tmp_path / "data" / "episodes" / "ep42"
    ep_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "BEATMAP.md", ep_dir / "BEATMAP.md")

    shutil.copy(FIXTURES / "bad-shots.json", ep_dir / "shots.json")  # 99 s clip: preflight error
    r = await client.post("/api/episodes", json={"episode_id": "ep42", "dry_run": True})
    assert r.status_code == 400 and "Refusing to start" in r.json()["detail"], r.text
    assert "3 to 15 seconds" in r.json()["detail"]
    (ep_dir / "shots.json").write_text("{not json")
    r = await client.post("/api/episodes", json={"episode_id": "ep42", "dry_run": True})
    assert r.status_code == 400 and "invalid shots.json" in r.json()["detail"], r.text
    (ep_dir / "shots.json").write_text(json.dumps({"shots": [{"id": "S01"}]}))  # schema error
    r = await client.post("/api/episodes", json={"episode_id": "ep42", "dry_run": True})
    assert r.status_code == 400, r.text
    start.assert_not_called()

    shutil.copy(FIXTURES / "shots.json", ep_dir / "shots.json")
    r = await client.post("/api/episodes", json={"episode_id": "ep42", "dry_run": True})
    assert r.status_code == 200, r.text
    start.assert_called_once()


@pytest.mark.asyncio
async def test_preflight_reports_offline_cost_estimate(api, tmp_path):
    api_main, client = api
    await _login(client)
    ep_dir = tmp_path / "data" / "episodes" / "ep42"
    ep_dir.mkdir(parents=True)
    shutil.copy(FIXTURES / "shots.json", ep_dir / "shots.json")
    r = await client.post("/api/episodes/ep42/preflight", json={})
    assert r.status_code == 200, r.text
    assert "cost_estimate" in r.json(), sorted(r.json())
    est = r.json()["cost_estimate"]
    assert est["total_usd_micros"] == sum(
        v["stills_usd_micros"] + (v["clip_usd_micros"] or 0) for v in est["per_shot"].values())
    assert set(est["per_shot"]) == {"S01", "S02"} and est["total_usd_micros"] > 0 and est["unpriced"] == []


# ---------------------------------------------------------------- approve lookup + G_FINAL_CUT

@pytest.mark.asyncio
async def test_approve_lookup_query_is_parenthesised(api):
    api_main, client = api
    await _login(client)
    handle, queries = _episode_handle(api_main, {})
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g2_scene_stills"})
    assert r.status_code == 200, r.text
    assert queries == ['(WorkflowId = "ep42" OR WorkflowId STARTS_WITH "ep42-") AND WorkflowType = "EpisodeWorkflowV2"']


AT_GATE = {"current_gate": "G_FINAL_CUT", "pending_clip_retries": [],
           "assembly": {"blocking": False, "qc": {"verdict": "PASS"}},
           "delivery": {"blocking": False, "qc": {"verdict": "PASS"}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("change, needle", [
    ({"assembly": {"blocking": True, "qc": {"verdict": "FAIL", "reason": "black frames"}}}, "assembly QC FAIL"),
    ({"delivery": {"blocking": True, "qc": {"verdict": "NEEDS_REVIEW", "reason": "loudness"}}}, "delivery QC NEEDS_REVIEW"),
    ({"pending_clip_retries": ["S01"]}, "clip retry pending for: S01"),
    ({"current_gate": "G2_SCENE_STILLS"}, "not at G_FINAL_CUT"),
])
async def test_final_cut_approve_refused_while_blocked(api, change, needle):
    api_main, client = api
    await _login(client)
    handle, _ = _episode_handle(api_main, {**AT_GATE, **change})
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g_final_cut"})
    assert r.status_code == 409, r.text
    assert needle in r.json()["detail"]
    handle.signal.assert_not_called()


@pytest.mark.asyncio
async def test_final_cut_approve_signals_when_clear(api):
    api_main, client = api
    await _login(client)
    handle, _ = _episode_handle(api_main, dict(AT_GATE))
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g_final_cut"})
    assert r.status_code == 200, r.text
    handle.signal.assert_awaited_once_with("approve_g_final_cut")


def test_no_duplicate_final_cut_route(monkeypatch):
    """Approval goes through POST .../approve only (one decision path with the 409 pre-check)."""
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    import api.main as api_main

    paths = {r.path for r in api_main.app.routes}
    assert not any("g_final_cut" in p.lower() for p in paths), sorted(paths)
    assert "/api/episodes/{episode_id}/final-cut/reject" in paths


@pytest.mark.asyncio
async def test_final_cut_reject_route(api):
    api_main, client = api
    await _login(client)
    handle, _ = _episode_handle(api_main, dict(AT_GATE))
    assert (await client.post("/api/episodes/ep42/final-cut/reject", json={"reason": "  "})).status_code == 422
    r = await client.post("/api/episodes/ep42/final-cut/reject", json={"reason": "pacing"})
    assert r.status_code == 200, r.text
    handle.signal.assert_awaited_once_with("reject_g_final_cut", "pacing")
    handle, _ = _episode_handle(api_main, {**AT_GATE, "current_gate": "G2_SCENE_STILLS"})
    assert (await client.post("/api/episodes/ep42/final-cut/reject", json={"reason": "x"})).status_code == 409
    handle.signal.assert_not_called()


@pytest.mark.asyncio
async def test_raise_cap_route_only_upward(api, monkeypatch):
    api_main, client = api
    await _login(client)
    from hfvg.credit_ledger import CreditLedger

    async def status(self, ep):
        return {"cap_credits": 500, "spent_credits": 0, "reserved_credits": 0}

    monkeypatch.setattr(CreditLedger, "get_episode_status", status)
    handle, _ = _episode_handle(api_main, {})
    r = await client.post("/api/episodes/ep42/raise-cap", json={"new_cap": 500})
    assert r.status_code == 409, r.text
    handle.signal.assert_not_called()
    assert (await client.post("/api/episodes/ep42/raise-cap", json={"new_cap": 0})).status_code == 422
    r = await client.post("/api/episodes/ep42/raise-cap", json={"new_cap": 650})
    assert r.status_code == 200 and r.json()["previous_cap"] == 500
    handle.signal.assert_awaited_once_with("raise_credit_cap", 650)


# ---------------------------------------------------------------- real Temporal dry run

async def _poll(client, ep, pred, budget=60.0):
    end = time.time() + budget
    st = None
    while time.time() < end:
        r = await client.get(f"/api/episodes/{ep}")
        if r.status_code == 200:
            st = r.json()
            if pred(st):
                return st
        await asyncio.sleep(0.25)
    raise AssertionError(f"condition not reached; last state {st}")


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_dry_run_final_gate_actions_real_workflow(api, tmp_path):
    """The episode page's actions against the real EpisodeWorkflowV2 + production activities (DRY_RUN):
    per-shot approve / reject at G2, cap raise AT the final gate reaches the ledger, approve refused
    (409) while a clip retry is pending, final-cut reject recorded with the gate held, then approve."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from hfvg.config import config
    from hfvg.credit_ledger import CreditLedger
    from hfvg.temporal_converter import temporal_data_converter
    from hfvg.worker import PRODUCTION_ACTIVITIES, PRODUCTION_WORKFLOWS

    api_main, client = api
    await _login(client)
    ep = "ep42"
    ep_dir = tmp_path / "data" / "episodes" / ep
    ep_dir.mkdir(parents=True)
    for name in ("BEATMAP.md", "shots.json", "refs.json", "prompt_kit.json"):
        shutil.copy(FIXTURES / name, ep_dir / name)

    async with await WorkflowEnvironment.start_local(data_converter=temporal_data_converter) as env:
        async with Worker(env.client, task_queue=config.TASK_QUEUE, workflows=PRODUCTION_WORKFLOWS,
                          activities=PRODUCTION_ACTIVITIES):
            api_main.temporal_client = env.client
            r = await client.post("/api/episodes", json={"episode_id": ep, "dry_run": True})
            assert r.status_code == 200, r.text
            await _poll(client, ep, lambda s: s["current_gate"] == "G1_SHOT_PLAN")
            assert (await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g1_shot_plan"})).status_code == 200
            await _poll(client, ep, lambda s: s["current_gate"] == "G2_SCENE_STILLS")

            shot_states = (await client.get(f"/api/episodes/{ep}/shot-states")).json()["shots"]
            assert set(shot_states) == {"S01", "S02"}
            await _poll(client, ep, lambda s: True)
            r = await client.post(f"/api/episodes/{ep}/shots/S02/reject", data={"reason": "framing"})
            assert r.status_code == 200, r.text
            r = await client.post(f"/api/episodes/{ep}/shots/S01/approve")
            assert r.status_code == 200, r.text
            st = await _poll(client, ep, lambda s: s["shot_decisions"].get("S01") == "approved")
            assert st["shot_decisions"]["S02"] == "rejected"
            r = await client.post(f"/api/episodes/{ep}/shots/S02/approve")  # flip back: undo the reject
            assert r.status_code == 200, r.text
            r = await client.post(f"/api/episodes/{ep}/shots/S02/reject", data={"reason": "framing"})
            assert r.status_code == 200, r.text
            assert (await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g2_scene_stills"})).status_code == 200
            assert (await client.post(f"/api/episodes/{ep}/shots/S01/approve")).status_code == 409  # G2 passed

            st = await _poll(client, ep, lambda s: s["current_gate"] == "G_FINAL_CUT"
                             and (s["delivery"] or {}).get("qc", {}).get("verdict"), budget=120)
            assert st["assembly"]["qc"]["verdict"] == "DRY_RUN" and not st["assembly"]["blocking"]

            cap_before = (await CreditLedger(api_main.DATABASE_PATH).get_episode_status(ep))["cap_credits"]
            r = await client.post(f"/api/episodes/{ep}/raise-cap", json={"new_cap": cap_before + 150})
            assert r.status_code == 200, r.text
            for _ in range(80):
                credits = (await client.get(f"/api/episodes/{ep}/credits")).json()
                if credits["cap_credits"] == cap_before + 150:
                    break
                await asyncio.sleep(0.25)
            assert credits["cap_credits"] == cap_before + 150, credits

            r = await client.post(f"/api/episodes/{ep}/shots/S01/retry-clip", json={"reason": "motion"})
            assert r.status_code == 200, r.text
            r = await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g_final_cut"})
            if r.status_code == 409:
                assert "clip retry pending" in r.json()["detail"]
            await _poll(client, ep, lambda s: not s["pending_clip_retries"] and s["clip_retries"].get("S01") == 1)
            r = await client.post(f"/api/episodes/{ep}/final-cut/reject", json={"reason": "pacing"})
            assert r.status_code == 200, r.text
            st = await _poll(client, ep, lambda s: s["final_cut_rejections"])
            assert st["final_cut_rejections"][-1]["reason"] == "pacing"
            assert "G_FINAL_CUT" not in st["passed_gates"] and not st["approvals"]["g_final_cut"]

            # wait for the same condition the approve guard checks: not due AND not mid-rebuild
            await _poll(client, ep, lambda s: not s["assembly"]["stale"] and not (s["delivery"] or {}).get("due")
                        and (s["delivery"] or {}).get("phase") in (None, "done"))
            r = await client.post(f"/api/episodes/{ep}/approve", json={"gate_id": "g_final_cut"})
            assert r.status_code == 200, r.text
            st = await _poll(client, ep, lambda s: "G_FINAL_CUT" in s["passed_gates"])
            assert st["approvals"]["g_final_cut"] is True
