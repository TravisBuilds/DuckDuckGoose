"""Round 7 B5: the playbook moderation-risk lint + routing check runs before every paid still /
clip submit, its decision is recorded, and it is advisory unless a rule is configured hard."""

import json

import httpx
import pytest
import respx
import temporalio.activity

from hfvg import moderation_lint as ml
from tests.lint_helpers import acapture, lint_meta, lint_ok
from tests.test_prompt_lint import _live_db

GUEST_ONLY = lint_meta(characters=["G"], duck=False, duck_declared=True)


@pytest.fixture(autouse=True)
def _no_rule_env(monkeypatch):
    monkeypatch.delenv(ml.HARD_RULES_ENV, raising=False)
    monkeypatch.delenv(ml.WORLD_REFS_ENV, raising=False)


def test_missing_no_duck_phrase_is_advisory_by_default_even_live():
    try:
        rec = ml.evaluate("A01", "Three guests by the pond", GUEST_ONLY, kind="still", live=True)
    except ml.ModerationLintError as e:  # advisory rules must never refuse, even live
        raise AssertionError(f"advisory duck_role refused a live submit: {e}") from None
    assert rec["decision"] == "advisory"
    assert rec["rules"]["duck_role"]["violations"] and rec["rules"]["duck_role"]["hard"] is False
    assert rec["hard_violations"] == [] and rec["advisory_violations"]


def test_undeclared_duck_present_makes_no_guest_only_claim():
    meta = lint_meta(characters=["G"], duck=False)  # shot_data default, not declared
    assert ml.evaluate("A01", "Three guests", meta, kind="still", live=True)["decision"] == "pass"
    ok = ml.evaluate("A01", "Three guests. No duck in frame.", GUEST_ONLY, kind="still", live=True)
    assert ok["decision"] == "pass", ok


def test_hard_rule_refuses_live_and_records_would_refuse_in_dry(monkeypatch):
    monkeypatch.setenv(ml.HARD_RULES_ENV, "duck_role")
    dry = ml.evaluate("A01", "Three guests", GUEST_ONLY, kind="still", live=False)
    assert dry["decision"] == "would_refuse" and dry["hard_rules"] == ["duck_role"]
    with pytest.raises(ml.ModerationLintError, match="no duck") as e:
        ml.evaluate("A01", "Three guests", GUEST_ONLY, kind="still", live=True)
    assert e.value.record["decision"] == "refuse"


def test_unknown_hard_rule_fails_loudly(monkeypatch):
    monkeypatch.setenv(ml.HARD_RULES_ENV, "duck_role, not_a_rule")
    with pytest.raises(ml.ModerationLintError, match="not_a_rule"):
        ml.hard_rules()


def test_routing_flags_moderation_risk_clip_routed_away_from_recommendation():
    meta = lint_meta()
    rec = ml.evaluate("A02", "Mud bath", meta, kind="clip", model="seedance", tags=["mud"], live=True)
    assert rec["routing"]["recommended"] == "kling" and rec["routing"]["actual"] == "seedance"
    assert rec["rules"]["moderation_routing"]["violations"], rec
    assert rec["decision"] == "advisory"
    kling = ml.evaluate("A02", "Mud bath", meta, kind="clip", model="kling", tags=["mud"], live=True)
    assert kling["rules"]["moderation_routing"]["violations"] == []
    still = ml.evaluate("A02", "Mud bath", meta, kind="still", tags=["mud"], live=True)
    assert still["rules"]["moderation_routing"]["violations"] == [], "routing applies to clips only"


def test_upright_pose_and_world_refs(monkeypatch):
    rec = ml.evaluate("A03", "A guest standing upright carrying a tray", lint_meta(), kind="still", live=True)
    assert rec["rules"]["animal_poses"]["violations"], rec
    assert rec["rules"]["same_resort"]["violations"] == [], "no world refs configured: not evaluated"


@pytest.mark.asyncio
async def test_dry_submits_record_the_decision(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced, submit_still_job_enforced
    from hfvg.studio_db import init_studio_db
    monkeypatch.setenv("DRY_RUN", "true")
    await init_studio_db(str(tmp_path / "test.db"))
    still = await submit_still_job_enforced("ep99", "A01", "Three guests", 1, shot_meta=GUEST_ONLY)
    assert still["moderation_lint"]["kind"] == "still"
    assert still["moderation_lint"]["decision"] == "advisory"
    clip = await submit_clip_job_enforced("ep99", "A01", "/tmp/s.jpg", "Three guests", 5.0, 1,
                                          shot_meta=GUEST_ONLY)
    assert clip["moderation_lint"]["kind"] == "clip" and clip["moderation_lint"]["routing"]["actual"] == "kling"


@pytest.mark.asyncio
async def test_dry_hard_violation_does_not_refuse(monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv(ml.HARD_RULES_ENV, "duck_role")
    out = await submit_still_job_enforced("ep99", "A01", "Three guests", 1, shot_meta=GUEST_ONLY)
    assert out["moderation_lint"]["decision"] == "would_refuse"


@pytest.mark.asyncio
@respx.mock
async def test_live_hard_violation_refuses_before_estimate_reserve_and_submit(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced, submit_still_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_db(tmp_path, monkeypatch)
    monkeypatch.setenv(ml.HARD_RULES_ENV, "duck_role")
    est = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    meta = lint_meta(count=3, characters=["G"], duck=False, duck_declared=True)
    err = await acapture(ml.ModerationLintError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("Three guests by the pond", 3), 1, shot_meta=meta))
    assert err is not None, "a hard playbook violation must refuse a live still"
    assert est.call_count == 0 and sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0 and status["spent"] == 0

    clip_est = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    # Anything else on the provider host (the clip submit itself) is answered, so a lint that
    # does not run surfaces as a submit that happened, not as an unmocked-request error.
    clip_sub = respx.route(host="api.higgsfield.ai").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    err = await acapture(ml.ModerationLintError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("Three guests by the pond", 3), 5.0, 1,
        shot_meta=meta))
    assert err is not None, "a hard playbook violation must refuse a live clip"
    assert clip_est.call_count == 0 and clip_sub.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_live_advisory_violation_does_not_block(tmp_path, monkeypatch):
    """validate_duck_role on a missing literal phrase stays advisory: the live still is submitted."""
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_db(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "req-1"}))
    meta = lint_meta(count=3, characters=["G"], duck=False, duck_declared=True)
    out = await submit_still_job_enforced("ep99", "A01", lint_ok("Three guests by the pond", 3), 1, shot_meta=meta)
    assert sub.call_count == 1
    assert out["moderation_lint"]["decision"] == "advisory"


@pytest.mark.asyncio
async def test_episode_preflight_rejects_unknown_hard_rule(tmp_path, monkeypatch):
    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db

    ep = tmp_path / "data" / "episodes" / "ep99"
    ep.mkdir(parents=True)
    (ep / "BEATMAP.md").write_text("A01 (2.5s)")
    (ep / "shots.json").write_text(json.dumps({"A01": {"prompt": "x", "params": {"model": "kling", "duration": 3}}}))
    (ep / "refs.json").write_text(json.dumps({"available": [], "to_generate": []}))
    (ep / "prompt_kit.json").write_text("{}")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    await init_studio_db(str(tmp_path / "t.db"))
    monkeypatch.setenv(ml.HARD_RULES_ENV, "bogus_rule")
    monkeypatch.setattr(temporalio.activity, "info", lambda: type("o", (), {"workflow_id": "wf"})())
    with pytest.raises(PreflightError) as e:
        await preflight_validate_episode_activity("ep99", None, False)
    assert any("bogus_rule" in p for p in e.value.problems), e.value.problems


def test_shot_data_records_whether_duck_present_was_declared():
    from hfvg.shot_data import parse_shots
    base = {"count": 1, "characters": ["G"], "wet": False, "model": "kling", "duration": 5,
            "end_frame": False, "prompt": "p", "ref_keys": []}
    plans = parse_shots({"shots": [{**base, "id": "A01"}, {**base, "id": "A02", "duck_present": False}]})
    assert plans["A01"]["shot_meta"]["duck_declared"] is False
    assert plans["A02"]["shot_meta"]["duck_declared"] is True
