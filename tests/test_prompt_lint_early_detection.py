"""A prompt lint kit the paid submits cannot load is caught before any spend, not at the first still:
the live episode preflight refuses it, POST /api/episodes and start-again refuse a live start, and the
G1 plan shows what the lint gate will decide (profile in force, kit error, per-shot problems). A kit
without a "lint" section loads the built-in default profile, so a free-form draft passes all of them.
Synthetic free-form episode (tests/fixtures/freeform_episode), no provider calls."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from temporalio.testing import ActivityEnvironment

from hfvg import paths
from hfvg.activities.g1_plan import compute_g1_plan_activity
from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
from hfvg.budget import BudgetLedger
from hfvg.shot_data import load_shot_plans, merge_into_shots

FIXTURE = Path(__file__).parent / "fixtures" / "freeform_episode"
FREEFORM_KIT = json.loads((FIXTURE / "prompt_kit.json").read_text(encoding="utf-8"))
FREEFORM_SHOTS = json.loads((FIXTURE / "shots.json").read_text(encoding="utf-8"))
SHOT_IDS = [s["id"] for s in FREEFORM_SHOTS["shots"]]
EP = "ep97lint"
ADMIN = "early-lint-admin-secret-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
DEFAULT_NOTICE_TAIL = ("has no 'lint' section: the built-in default lint profile applies "
                       "(content-policy terms and motion rules, no series clauses)")

# name -> (per-episode prompt_kit.json, PROMPT_LINT_KIT_PATH file or None, what the refusal says)
BROKEN = {
    "unknown-lint-value": ({**FREEFORM_KIT, "lint": "strict"}, None, '"lint" must be an object'),
    "lint-object-without-rules": ({**FREEFORM_KIT, "lint": {"realtime_pattern": "real-time"}}, None,
                                  "missing required field 'count_template'"),
    "invalid-regex": ({**FREEFORM_KIT, "lint": {"count_template": "{n} people", "realtime_pattern": "(",
                                                "locked_camera_clause": "locked camera"}}, None,
                      "realtime_pattern is not a valid regex"),
    "not-an-object": ([FREEFORM_KIT], None, "must be a JSON object"),
    "override-without-lint-rules": (FREEFORM_KIT, {"style": "an override with no lint rules"},
                                    "has no 'lint' section"),
}


def _write_kit(ep: str, kit) -> None:
    (paths.episode_dir(ep) / "prompt_kit.json").write_text(json.dumps(kit), encoding="utf-8")


def _episode(ep: str, kit=FREEFORM_KIT) -> Path:
    """The free-form episode's files as the create wizard uploads them."""
    d = paths.episode_dir(ep)
    d.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(f"| {i} | {sid} | 8.0 (8.0) | Harbor | Day | A | beat {i} | Story | - | - | K |"
                     for i, sid in enumerate(SHOT_IDS, start=1))
    (d / "BEATMAP.md").write_text(
        f"# Episode {ep} Beatmap\n\n| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out "
        "| Model |\n|---|---|---|---|---|---|---|---|---|---|---|\n" + rows + "\n", encoding="utf-8")
    (d / "shots.json").write_text(json.dumps(FREEFORM_SHOTS), encoding="utf-8")
    (d / "refs.json").write_text(json.dumps({"per_shot": {sid: [] for sid in SHOT_IDS}}), encoding="utf-8")
    _write_kit(ep, kit)
    return d


def _break(case: str, ep: str, tmp_path, monkeypatch) -> str:
    """Give the episode the broken lint config `case`; returns what the refusal says."""
    kit, override, expected = BROKEN[case]
    _write_kit(ep, kit)
    if override is None:
        monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    else:
        path = tmp_path / "override_kit.json"
        path.write_text(json.dumps(override), encoding="utf-8")
        monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(path))
    return expected


async def _preflight(ep: str, live: bool) -> list[str]:
    """The episode preflight's problems ([] when it passes)."""
    try:
        await ActivityEnvironment().run(preflight_validate_episode_activity, ep, None, live)
    except PreflightError as e:
        return e.problems
    return []


@pytest.fixture
async def live_ready(monkeypatch):
    """What a live preflight needs besides the files: an API key and a recent manual balance."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "synthetic-not-a-key")
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    await ledger.set_manual_balance(50_000_000, set_by="test")


# ------------------------------------------------------------------ episode preflight
async def test_free_form_draft_passes_the_live_preflight(live_ready, monkeypatch):
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH")
    _episode(EP)
    assert await _preflight(EP, live=True) == []


@pytest.mark.parametrize("case", sorted(BROKEN))
async def test_live_preflight_refuses_a_lint_kit_that_cannot_load(live_ready, tmp_path, monkeypatch, case):
    _episode(EP)
    expected = _break(case, EP, tmp_path, monkeypatch)
    problems = await _preflight(EP, live=True)
    assert len(problems) == 1, problems
    assert problems[0].startswith("prompt lint kit: ") and expected in problems[0], problems


@pytest.mark.parametrize("case", sorted(BROKEN))
async def test_dry_preflight_leaves_the_lint_kit_to_the_dry_submits(tmp_path, monkeypatch, case):
    """A dry submit only warns about lint (enforce_prompt_lint), so a dry run is not refused for it."""
    _episode(EP)
    _break(case, EP, tmp_path, monkeypatch)
    assert await _preflight(EP, live=False) == []


# ------------------------------------------------------------------ G1 plan
def _merged_shots(ep: str) -> list[dict]:
    """The shots the workflow hands to G1: shots.json merged onto the beatmap (parse_beatmap_activity)."""
    beat = [{"shot_id": sid, "prompt": f"beat {sid}", "params": {}} for sid in SHOT_IDS]
    return merge_into_shots(ep, beat, load_shot_plans(ep, require=False), True)


async def test_g1_plan_shows_the_default_profile_and_what_it_will_refuse(monkeypatch):
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH")
    _episode(EP)
    shots = _merged_shots(EP)
    lint = (await compute_g1_plan_activity(EP, shots, True)).get("prompt_lint") or {}
    assert lint.get("notice", "").endswith(DEFAULT_NOTICE_TAIL), lint
    assert {k: v for k, v in lint.items() if k != "notice"} == {
        "ok": True, "profile": "default", "kit_error": None, "shots": {}, "problem_count": 0}

    term = "content-policy hard-stop term: 'provocative' (docs/CONTENT-POLICY.md)"
    shots[1]["prompt"] += ", a provocative pose"
    shots[3]["prompt"] += ", a provocative pose"
    shots[3]["pre_approved_still"] = {"path": "/approved/S04.png"}  # no still submit: only its clip is linted
    lint = (await compute_g1_plan_activity(EP, shots, True)).get("prompt_lint") or {}
    assert lint.get("shots") == {"S02": [f"still: {term}", f"clip: {term}"], "S04": [f"clip: {term}"]}, lint
    assert (lint["ok"], lint["problem_count"]) == (False, 3)


async def test_g1_plan_keeps_a_kits_own_lint_rules():
    """The conftest kit is a series lint section (PROMPT_LINT_KIT_PATH): its clauses, not the defaults."""
    _episode(EP)
    lint = (await compute_g1_plan_activity(EP, _merged_shots(EP), True)).get("prompt_lint") or {}
    assert (lint.get("profile"), lint.get("notice"), lint.get("kit_error")) == ("kit", None, None), lint
    assert sorted(lint.get("shots") or {}) == SHOT_IDS


@pytest.mark.parametrize("case", sorted(BROKEN))
async def test_g1_plan_reports_a_lint_kit_that_cannot_load(tmp_path, monkeypatch, case):
    _episode(EP)
    expected = _break(case, EP, tmp_path, monkeypatch)
    plan = await compute_g1_plan_activity(EP, _merged_shots(EP), True)
    lint = plan.get("prompt_lint") or {}
    assert (lint.get("ok"), lint.get("profile"), lint.get("shots")) == (False, None, {}), lint
    assert expected in (lint.get("kit_error") or ""), lint
    assert plan["preflight_errors"] == [], "informational at G1: the live preflight is what refuses"


# ------------------------------------------------------------------ API starts
@pytest.fixture
async def api(monkeypatch):
    """The API with a studio DB and a Temporal client whose starts succeed."""
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db

    monkeypatch.setenv("ADMIN_SECRET", ADMIN)
    monkeypatch.delenv("ELEVENLABS_DEFAULT_VOICE_ID", raising=False)
    db = os.environ["DATABASE_PATH"]
    await init_studio_db(db)
    await BudgetLedger(db).init_db()
    await CreditLedger(db).init_db()
    sys.modules.pop("api.main", None)
    import api.main as api_main

    api_main.temporal_client = MagicMock()
    api_main.temporal_client.start_workflow = AsyncMock(return_value=MagicMock(id=EP, result_run_id="run-1"))
    return api_main


def _start(api_main, dry_run: bool):
    from fastapi.testclient import TestClient
    return TestClient(api_main.app).post("/api/episodes", json={"episode_id": EP, "dry_run": dry_run},
                                         headers=AUTH)


async def test_live_start_of_a_free_form_draft_goes_through(api, monkeypatch):
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH")
    _episode(EP)
    res = _start(api, dry_run=False)
    assert res.status_code == 200, res.text
    assert api.temporal_client.start_workflow.call_args.kwargs["args"][2] is False


@pytest.mark.parametrize("case", sorted(BROKEN))
async def test_live_start_is_refused_while_the_lint_kit_cannot_load(api, tmp_path, monkeypatch, case):
    _episode(EP)
    expected = _break(case, EP, tmp_path, monkeypatch)
    live = _start(api, dry_run=False)
    assert live.status_code == 400, live.text
    assert live.json()["detail"].startswith(f"Refusing to start {EP}: prompt lint kit: "), live.text
    assert expected in live.json()["detail"]
    api.temporal_client.start_workflow.assert_not_called()
    dry = _start(api, dry_run=True)
    assert dry.status_code == 200, dry.text


@pytest.mark.parametrize("dry_run", [False, True])
async def test_start_again_rechecks_the_lint_kit_of_a_live_run(api, tmp_path, monkeypatch, dry_run):
    from fastapi.testclient import TestClient

    from hfvg.studio_db import create_episode
    from tests.test_episode_ownership import _ended_run
    from tests.test_run_uploads_voice_recheck import _temporal

    await create_episode(os.environ["DATABASE_PATH"], EP)
    api.temporal_client = _temporal("FAILED")
    _episode(EP)
    inputs = _ended_run(api, EP, dry_run=dry_run)
    expected = _break("unknown-lint-value", EP, tmp_path, monkeypatch)
    again = TestClient(api.app)
    res = again.post(f"/api/episodes/{EP}/start-again", headers=AUTH)
    if not dry_run:
        assert res.status_code == 400, res.text
        assert res.json()["detail"].startswith(f"Refusing to start {EP} again: prompt lint kit: "), res.text
        assert expected in res.json()["detail"]
        api.temporal_client.start_workflow.assert_not_called()
        _write_kit(EP, FREEFORM_KIT)  # the kit uploaded again without the bad "lint" value
        res = again.post(f"/api/episodes/{EP}/start-again", headers=AUTH)
    assert res.status_code == 200, res.text
    assert api.temporal_client.start_workflow.call_args.kwargs["args"] == inputs
