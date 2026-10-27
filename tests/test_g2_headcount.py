"""GAP-27 (ep05v2 2026-10-27): V12 v2 had FOUR main-cast animals for a 3-animal shot and reached the G2
package: nothing compared the animals in a still with the declared cast. The G2 headcount check counts
main-cast animals, ducks and every animal in each kept still; a mismatch is FAIL and blocks G2 approval
(409) unless overridden per shot with an audited reason. No counter and no reviewer count -> NEEDS_REVIEW
(never a silent PASS)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hfvg.qc.animal_count import FakeAnimalCounter
from hfvg.qc.g2_headcount import (
    ReviewerHeadcountCounter, check_still_headcount, expected_cast, g2_headcount_report, load_headcounts,
    record_headcount,
)

CAPY3 = {"count": 3, "characters": ["R", "H", "M"], "duck": False}
DUCK_FAMILY = {"count": 3, "characters": ["D", "R", "H", "M"], "duck": True}


def _still(tmp_path, shot="V12", v=2, ep="ep42"):
    d = tmp_path / "episodes" / ep / "outputs" / shot
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"still_v{v}.jpg"
    p.write_bytes(b"\xff\xd8jpeg")
    return p


def test_expected_cast():
    assert expected_cast(CAPY3) == {"cast": 3, "duck": 0, "animal": 3}
    assert expected_cast(DUCK_FAMILY) == {"cast": 3, "duck": 1, "animal": 4}
    assert expected_cast({"count": 0, "duck": False}) == {"cast": 0, "duck": 0, "animal": 0}
    assert expected_cast({}) is None and expected_cast({"count": True}) is None


@pytest.mark.asyncio
async def test_four_cast_animals_for_three_is_fail(tmp_path):
    p = _still(tmp_path)
    res = await check_still_headcount(FakeAnimalCounter({"cast": 4, "duck": 0, "animal": 4}), p, CAPY3)
    assert res["status"] == "FAIL" and "cast: expected 3, counted 4" in res["reason"], res


@pytest.mark.asyncio
async def test_extra_other_animal_fails_on_total(tmp_path):
    p = _still(tmp_path)
    res = await check_still_headcount(FakeAnimalCounter({"cast": 3, "duck": 0, "animal": 4}), p, CAPY3)
    assert res["status"] == "FAIL" and "animal: expected 3, counted 4" in res["reason"]


@pytest.mark.asyncio
async def test_missing_or_extra_duck_fails(tmp_path):
    p = _still(tmp_path)
    no_duck = await check_still_headcount(FakeAnimalCounter({"cast": 3, "duck": 0, "animal": 3}), p, DUCK_FAMILY)
    assert no_duck["status"] == "FAIL" and "duck: expected 1, counted 0" in no_duck["reason"]
    extra = await check_still_headcount(FakeAnimalCounter({"cast": 3, "duck": 1, "animal": 4}), p, CAPY3)
    assert extra["status"] == "FAIL" and "duck: expected 0, counted 1" in extra["reason"]


@pytest.mark.asyncio
async def test_matching_cast_passes(tmp_path):
    p = _still(tmp_path)
    res = await check_still_headcount(FakeAnimalCounter({"cast": 3, "duck": 1, "animal": 4}), p, DUCK_FAMILY)
    assert res["status"] == "PASS", res


@pytest.mark.asyncio
async def test_no_counter_low_confidence_or_no_file_is_needs_review(tmp_path):
    p = _still(tmp_path)
    assert (await check_still_headcount(None, p, CAPY3))["status"] == "NEEDS_REVIEW"
    low = FakeAnimalCounter({"cast": 4, "duck": 0, "animal": 4}, confidence=0.3)
    assert (await check_still_headcount(low, p, CAPY3))["status"] == "NEEDS_REVIEW"
    assert (await check_still_headcount(FakeAnimalCounter({}), tmp_path / "nope.jpg", CAPY3))["status"] == "NEEDS_REVIEW"
    assert (await check_still_headcount(FakeAnimalCounter({}), p, {}))["status"] == "NEEDS_REVIEW"
    empty = ReviewerHeadcountCounter({}, None)
    assert (await check_still_headcount(empty, p, CAPY3))["status"] == "NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_reviewer_headcount_counts_exact_version(tmp_path):
    eps = tmp_path / "eps"
    record_headcount("ep42", "V12", 2, {"cast": 4, "duck": 0}, by="admin", note="4th at left", episodes_dir=eps)
    data = load_headcounts("ep42", eps)
    assert data["V12"]["2"]["animal"] == 4
    c = ReviewerHeadcountCounter(data)
    v2 = _still(tmp_path, v=2)
    v3 = _still(tmp_path, v=3)
    assert (await check_still_headcount(c, v2, CAPY3))["status"] == "FAIL"
    assert (await check_still_headcount(c, v3, CAPY3))["status"] == "NEEDS_REVIEW"  # v3 not counted yet
    with pytest.raises(ValueError):
        record_headcount("ep42", "V12", 2, {"cast": -1, "duck": 0}, by="a", episodes_dir=eps)


def _versions(**cur):
    return {s: {"current_version": v, "selection": None, "versions": []} for s, v in cur.items()}


@pytest.mark.asyncio
async def test_report_uses_kept_version_skips_rejected_and_honours_overrides(tmp_path):
    eps = tmp_path / "eps"
    for v in (2, 3):
        _still(tmp_path, "V12", v)
    _still(tmp_path, "V23", 2)
    record_headcount("ep42", "V12", 2, {"cast": 4, "duck": 0}, by="a", episodes_dir=eps)
    record_headcount("ep42", "V12", 3, {"cast": 3, "duck": 0}, by="a", episodes_dir=eps)
    record_headcount("ep42", "V23", 2, {"cast": 2, "duck": 0}, by="a", episodes_dir=eps)
    plans = {"V12": {"shot_meta": CAPY3}, "V23": {"shot_meta": CAPY3}}
    rep = await g2_headcount_report("ep42", _versions(V12=2, V23=2), plans, {}, data_dir=tmp_path, episodes_dir=eps)
    assert rep["status"] == "FAIL" and rep["blocking"] == ["V12", "V23"], rep
    rep = await g2_headcount_report("ep42", _versions(V12=3, V23=2), plans, {"V23": "rejected"},
                                    data_dir=tmp_path, episodes_dir=eps)
    assert rep["blocking"] == [] and rep["shots"]["V12"]["status"] == "PASS"
    assert rep["shots"]["V23"]["status"] == "SKIPPED"
    rep = await g2_headcount_report("ep42", _versions(V12=2, V23=3), plans, {}, data_dir=tmp_path,
                                    episodes_dir=eps, overrides={"V12": "studio ok with 4"})
    assert rep["blocking"] == [] and rep["shots"]["V12"]["override"] == "studio ok with 4"
    assert rep["shots"]["V23"]["status"] == "NEEDS_REVIEW"  # v3 has no file / no count


# ---------------------------------------------------------------- API

class _Handle:
    def __init__(self, versions, plans, decisions=None):
        self.versions, self.plans, self.decisions = versions, plans, decisions or {}
        self.updates = []

    async def query(self, name, args=None):
        if name == "still_versions":
            return self.versions
        if name == "still_version_plan":
            return self.plans.get(args[0])
        return {"shot_decisions": self.decisions}

    async def execute_update(self, name, *args, **kw):
        self.updates.append(name)
        return {"success": True}


@pytest.fixture
async def api(tmp_path, monkeypatch):
    import httpx
    from tests.test_round5_guards import ADMIN_SECRET, _login
    monkeypatch.setenv("ADMIN_SECRET", ADMIN_SECRET)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    import sys
    sys.modules.pop("api.main", None)
    import api.main as api_main
    from hfvg.studio_db import init_studio_db
    await init_studio_db(str(tmp_path / "studio.db"))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://test")
    await _login(client)
    yield api_main, client
    await client.aclose()


def _wire(api_main, handle):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    def list_workflows(query, *a, **k):
        async def gen():
            yield SimpleNamespace(id="ep42")
        return gen()
    client = MagicMock()
    client.list_workflows = list_workflows
    client.get_workflow_handle = MagicMock(return_value=handle)
    api_main.temporal_client = client


@pytest.mark.asyncio
async def test_api_g2_approve_blocked_by_headcount_fail_until_fixed_or_overridden(api, tmp_path):
    api_main, client = api
    _still(tmp_path, "V12", 2)
    handle = _Handle(_versions(V12=2), {"V12": {"shot_meta": CAPY3}})
    _wire(api_main, handle)
    r = await client.post("/api/episodes/ep42/shots/V12/headcount", json={"version": 2, "cast": 4, "duck": 0})
    assert r.status_code == 200, r.text
    r = await client.post("/api/episodes/ep42/shots/V12/headcount", json={"version": 9, "cast": 3, "duck": 0})
    assert r.status_code == 409  # no such still on disk
    r = await client.get("/api/episodes/ep42/g2-headcount")
    assert r.status_code == 200 and r.json()["blocking"] == ["V12"], r.text
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g2_scene_stills"})
    assert r.status_code == 409 and "headcount FAIL" in r.json()["detail"] and "V12 v2" in r.json()["detail"]
    assert handle.updates == []
    r = await client.post("/api/episodes/ep42/approve",
                          json={"gate_id": "g2_scene_stills", "headcount_overrides": {"V12": " "}})
    assert r.status_code == 422
    r = await client.post("/api/episodes/ep42/approve",
                          json={"gate_id": "g2_scene_stills", "headcount_overrides": {"V12": "Studio accepts 4"}})
    assert r.status_code == 200 and handle.updates == ["approve_g2_scene_stills"], r.text
    with sqlite3.connect(tmp_path / "studio.db") as c:
        actions = [a for (a,) in c.execute("SELECT action FROM audit_log")]
    assert "g2_headcount" in actions and "g2_headcount_override" in actions


@pytest.mark.asyncio
async def test_api_g2_approve_not_blocked_by_needs_review_and_fails_closed_when_unreadable(api, tmp_path):
    api_main, client = api
    _still(tmp_path, "V12", 3)
    handle = _Handle(_versions(V12=3), {"V12": {"shot_meta": CAPY3}})
    _wire(api_main, handle)
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g2_scene_stills"})
    assert r.status_code == 200, r.text  # NEEDS_REVIEW is the human's call at G2, not a block

    class Broken(_Handle):
        async def query(self, name, args=None):
            raise RuntimeError("query failed")
    broken = Broken({}, {})
    _wire(api_main, broken)
    r = await client.post("/api/episodes/ep42/approve", json={"gate_id": "g2_scene_stills"})
    assert r.status_code == 409 and "fails closed" in r.json()["detail"] and broken.updates == []
