"""Built-in default prompt lint profile: a prompt kit without a "lint" section (draft-from-idea,
free-form uploads) or with ``"lint": "default"`` is linted by the default profile, not refused.

The free-form fixture (tests/fixtures/freeform_episode) mirrors a real draft-from-idea upload:
17 free-form shots, count 1 everywhere (crowd shots included), no count or real-time phrasing.
Synthetic text only.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from hfvg import paths
from hfvg.content_policy import HARD_STOP
from hfvg.prompt_lint import (
    PromptLintError,
    apply_continuity_locks,
    clip_lint_violations,
    default_lint_kit,
    enforce_prompt_lint,
    lint_kit_problems,
    lint_report,
    lint_summary,
    lint_violations,
    load_lint_kit,
    resolve_lint_kit,
)
from hfvg.shot_data import parse_shots
from tests.lint_helpers import SYNTHETIC_LINT_KIT, acapture, capture, lint_meta, lint_ok
from tests.test_prompt_lint import _live_db

FIXTURE = Path(__file__).parent / "fixtures" / "freeform_episode"
FREEFORM_KIT = json.loads((FIXTURE / "prompt_kit.json").read_text(encoding="utf-8"))
FREEFORM_PLANS = parse_shots(json.loads((FIXTURE / "shots.json").read_text(encoding="utf-8")))

FREE = "A courier cycles through the market at noon while pigeons scatter."
GRADE_LOCK = "SYNTH GRADE: warm dusk palette, soft film grain."
LOCKS = [{"id": "grade", "when": "always", "kinds": ["still", "clip"], "text": GRADE_LOCK}]


@pytest.fixture
def episodes(monkeypatch):
    """Per-episode kits only: no PROMPT_LINT_KIT_PATH override."""
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    return paths.episodes_dir()


def _write_kit(ep: str, kit) -> Path:
    path = paths.episodes_dir() / ep / "prompt_kit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(kit), encoding="utf-8")
    return path


def _gate(ep: str, plan: dict, kind: str):
    """The live submit gate (lock injection, then lint): the PromptLintError, or None if it passes."""
    raw = (plan.get("clip_prompt") or plan["prompt"]) if kind == "clip" else plan["prompt"]

    def submit_side():
        prompt, _ = apply_continuity_locks(ep, raw, plan["shot_meta"], True, kind)
        enforce_prompt_lint(ep, prompt, plan["shot_meta"], True, kind)

    return capture(PromptLintError, submit_side)


# ---------------------------------------------------------------- the free-form draft episode

def test_freeform_draft_kit_and_shots_pass_the_default_profile_with_zero_hits(episodes):
    _write_kit("ep98x", FREEFORM_KIT)
    assert "lint" not in FREEFORM_KIT and len(FREEFORM_PLANS) == 17
    assert {p["shot_meta"]["count"] for p in FREEFORM_PLANS.values()} == {1}

    summary = lint_summary("ep98x", FREEFORM_PLANS)
    assert summary["kit_error"] is None and summary["profile"] == "default"
    assert summary["shots"] == {} and summary["problem_count"] == 0 and summary["ok"] is True
    assert "no 'lint' section" in summary["notice"] and "default lint profile" in summary["notice"]

    for sid, plan in FREEFORM_PLANS.items():
        for kind in ("still", "clip"):
            err = _gate("ep98x", plan, kind)
            assert err is None, f"{sid} {kind}: {err}"

    # The kit's own text (style, characters, sets) is composed into canary prompts: clean too.
    kit = load_lint_kit("ep98x")
    text = " ".join([FREEFORM_KIT["style"], *(c["description"] for c in FREEFORM_KIT["characters"].values()),
                     *FREEFORM_KIT["sets"].values()])
    meta = lint_meta(characters=sorted(FREEFORM_KIT["characters"]))
    assert lint_violations(text, meta, kit) == [] and clip_lint_violations(text, meta, kit) == []


def test_the_same_shots_fail_every_shot_under_a_series_kit(episodes):
    _write_kit("ep98x", {**FREEFORM_KIT, "lint": SYNTHETIC_LINT_KIT})
    summary = lint_summary("ep98x", FREEFORM_PLANS)
    assert summary["profile"] == "kit" and summary["notice"] is None and summary["ok"] is False
    assert sorted(summary["shots"]) == sorted(FREEFORM_PLANS)
    for problems in summary["shots"].values():
        assert any(p.startswith("still: missing count clause") for p in problems), problems
        assert "still: missing real-time / no-slow-motion clause" in problems
    assert _gate("ep98x", FREEFORM_PLANS["S01"], "still") is not None


@pytest.mark.asyncio
@respx.mock
async def test_live_still_without_a_lint_section_reaches_the_provider(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_db(tmp_path, monkeypatch)
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    _write_kit("ep99", FREEFORM_KIT)
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "req-1"}))
    plan = FREEFORM_PLANS["S01"]
    try:
        await submit_still_job_enforced("ep99", "S01", plan["prompt"], 1, shot_meta=plan["shot_meta"])
    except PromptLintError as e:
        raise AssertionError(f"a free-form still was refused by prompt lint: {e}") from None
    assert sub.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_live_still_with_a_hard_stop_term_is_refused_before_any_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_db(tmp_path, monkeypatch)
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    _write_kit("ep99", FREEFORM_KIT)
    est = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    err = await acapture(PromptLintError, submit_still_job_enforced(
        "ep99", "S01", "A topless figure poses on the convoy truck.", 1, shot_meta=lint_meta()))
    assert err is not None and "hard-stop term: 'topless'" in str(err)
    assert est.call_count == 0 and sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0 and status["spent"] == 0


# ---------------------------------------------------------------- what the default profile checks

def test_default_profile_requires_no_series_clauses():
    kit = default_lint_kit()
    for meta in (lint_meta(), lint_meta(count=0), lint_meta(count=5, exterior=True, water=True, characters=["A"])):
        assert lint_violations(FREE, meta, kit) == [], meta
        assert clip_lint_violations(FREE, meta, kit) == [], meta
    # series style rules stay with the kits that ask for them
    for prompt in ("Big waves roll in under a grey sky.", "Photorealistic wildlife grazes by the road.",
                   "The moose is carrying a crate.", "A lantern glows under the gazebo."):
        assert lint_violations(prompt, lint_meta(), kit) == [], prompt


@pytest.mark.parametrize("term", sorted(HARD_STOP))
def test_default_profile_refuses_every_hard_stop_term(term):
    kit = default_lint_kit()
    prompt = f"A poster in the shop window reads {term.upper()} in red letters."
    expected = f"content-policy hard-stop term: {term!r} (docs/CONTENT-POLICY.md)"
    assert expected in lint_violations(prompt, lint_meta(), kit)
    assert expected in clip_lint_violations(prompt, lint_meta(), kit)


def test_hard_stop_terms_are_read_as_whole_words_like_the_moderation_ladder():
    from hfvg.activities.moderation_recovery import hard_stop_terms

    kit = default_lint_kit()
    for prompt in ("A comet barely visible to the naked-eye observer.", "They speak intimately by the fire.",
                   "A documentary about sexuality education."):
        assert hard_stop_terms(prompt) == [] and lint_violations(prompt, lint_meta(), kit) == [], prompt
    two = lint_violations("A nude statue beside a sensual mural.", lint_meta(), kit)
    assert [p for p in two if "hard-stop" in p] == [
        "content-policy hard-stop term: 'nude' (docs/CONTENT-POLICY.md)",
        "content-policy hard-stop term: 'sensual' (docs/CONTENT-POLICY.md)",
    ]


PRECHECK_CASES = {
    "nsfw": "An nsfw sticker on the laptop lid.",
    "gore": "A gore-streaked battlefield at dusk.",
    "explicitly": "He explicitly refuses the order.",
    "gore-tex": "A hiker zips up a Gore-Tex jacket.",
    "explicit": "An explicit warning label.",
    "clean": "A calm harbour at dawn.",
    "gorgeous": "A gorgeous sunset over the gorge.",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", list(PRECHECK_CASES.values()), ids=list(PRECHECK_CASES))
async def test_default_profile_refuses_exactly_what_the_live_still_precheck_refuses(prompt, monkeypatch):
    from hfvg.activities import qc

    monkeypatch.setattr(qc.config, "DRY_RUN", False)
    precheck = await ActivityEnvironment().run(qc.precheck_still_qc, "ep99", "S01", prompt, {})
    problems = lint_violations(prompt, lint_meta(), default_lint_kit())
    assert bool(problems) == (not precheck["passed"]), (problems, precheck)
    assert len(problems) <= 1, "a word in both lists is reported once"


def test_default_profile_keeps_the_motion_rules():
    kit = default_lint_kit()
    assert "forbidden phrase: 'nothing else moves'" in lint_violations(f"{FREE} Nothing else moves.", lint_meta(), kit)
    clip = clip_lint_violations(f"{FREE} Freeze-frame on the courier.", lint_meta(), kit)
    assert clip and "clip prompt freezes the scene" in clip[0]


def test_default_profile_fails_closed_on_bad_metadata_and_series_flags():
    kit = default_lint_kit()
    assert "shot_meta.count must be a non-negative integer" in lint_violations(FREE, lint_meta(count="1"), kit)
    flagged = lint_violations(FREE, lint_meta(duck=True), kit)
    assert "lint kit has no duck_clause but shot_meta.duck is set" in flagged
    assert capture(PromptLintError, enforce_prompt_lint, "ep-any", FREE, None, True) is not None


def test_default_profile_applies_the_kits_top_level_continuity_locks(episodes):
    _write_kit("ep70", {**FREEFORM_KIT, "continuity_locks": LOCKS})
    kit = load_lint_kit("ep70")
    assert kit.profile == "default" and [dict(lock)["id"] for lock in kit.continuity_locks] == ["grade"]
    meta = lint_meta()
    prompt, added = apply_continuity_locks("ep70", FREE, meta, True, "still")
    assert added == ["grade"] and prompt.endswith(GRADE_LOCK)
    assert capture(PromptLintError, enforce_prompt_lint, "ep70", prompt, meta, True) is None
    skipped = capture(PromptLintError, enforce_prompt_lint, "ep70", FREE, meta, True)
    assert skipped is not None and "missing continuity lock 'grade' (still)" in str(skipped)
    plans = {"S01": {"prompt": FREE, "clip_prompt": None, "shot_meta": meta, "pre_approved_still": None}}
    assert lint_summary("ep70", plans)["shots"] == {}


def test_summary_lints_the_lock_injected_prompts_like_the_submit_gate(episodes):
    _write_kit("ep77", {**FREEFORM_KIT, "lint": SYNTHETIC_LINT_KIT, "continuity_locks": LOCKS})
    clean = {"prompt": lint_ok("A heron wades by the dock"), "clip_prompt": None, "shot_meta": lint_meta(),
             "pre_approved_still": None}
    bad = {**clean, "prompt": "A heron wades by the dock. Nothing else moves."}
    assert _gate("ep77", clean, "still") is None and _gate("ep77", clean, "clip") is None
    assert _gate("ep77", bad, "still") is not None
    summary = lint_summary("ep77", {"A01": clean, "A02": bad})
    assert sorted(summary["shots"]) == ["A02"] and summary["profile"] == "kit"
    assert not any("continuity lock" in p for p in summary["shots"]["A02"])


# ---------------------------------------------------------------- explicit and invalid lint values

def test_explicit_default_profile(episodes):
    _write_kit("ep71", {**FREEFORM_KIT, "lint": "default"})
    kit, notice = resolve_lint_kit("ep71")
    assert kit == default_lint_kit() and notice is None
    assert lint_report("ep71", FREEFORM_PLANS) == {}
    assert lint_kit_problems("ep71") == []


def test_a_lint_object_keeps_the_kits_own_rules(episodes):
    _write_kit("ep72", {**FREEFORM_KIT, "lint": SYNTHETIC_LINT_KIT})
    kit, notice = resolve_lint_kit("ep72")
    assert kit.profile == "kit" and notice is None and kit.count_template == SYNTHETIC_LINT_KIT["count_template"]
    # kits with a lint object never had content-policy term checks; they still do not
    assert lint_violations(lint_ok("A nude statue"), lint_meta(), kit) == []
    assert lint_violations("A nude statue", lint_meta(), default_lint_kit())


def test_override_file_without_lint_rules_is_still_refused(tmp_path, monkeypatch):
    full = tmp_path / "full_kit.json"
    full.write_text(json.dumps(FREEFORM_KIT))
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(full))
    err = capture(PromptLintError, load_lint_kit, "ep-any")
    assert err is not None and "has no 'lint' section" in str(err)
    full.write_text(json.dumps({**FREEFORM_KIT, "lint": "default"}))
    assert load_lint_kit("ep-any") == default_lint_kit()


@pytest.mark.parametrize("value", ["strict", "", "Default", None, 1, True, ["default"]])
def test_any_other_lint_value_fails_closed_before_spend(episodes, value):
    _write_kit("ep73", {**FREEFORM_KIT, "lint": value})
    err = capture(PromptLintError, resolve_lint_kit, "ep73")
    assert err is not None and '"lint" must be an object' in str(err) and json.dumps(value) in str(err)
    assert lint_kit_problems("ep73") == [f"prompt lint kit: {err}"]
    summary = lint_summary("ep73", FREEFORM_PLANS)
    assert summary["ok"] is False and summary["kit_error"] == str(err) and summary["profile"] is None
    assert lint_report("ep73", FREEFORM_PLANS) == {"*": [str(err)]}
    assert _gate("ep73", FREEFORM_PLANS["S01"], "still") is not None


def test_a_lint_object_missing_its_rules_is_still_refused(episodes):
    _write_kit("ep74", {**FREEFORM_KIT, "lint": {}})
    problems = lint_kit_problems("ep74")
    assert problems == ["prompt lint kit: Lint kit is missing required field 'count_template'"]


def test_missing_or_unreadable_kits_are_reported(episodes):
    assert lint_kit_problems("ep-none") and "No prompt lint kit" in lint_kit_problems("ep-none")[0]
    path = _write_kit("ep75", {})
    path.write_text("{not json", encoding="utf-8")
    assert "Cannot read prompt lint kit" in lint_kit_problems("ep75")[0]
    path.write_text("[]", encoding="utf-8")
    assert lint_kit_problems("ep75") == [f"prompt lint kit: Prompt lint kit {path} must be a JSON object"]


def test_lint_report_marks_the_default_profile_once(episodes):
    _write_kit("ep76", FREEFORM_KIT)
    plans = {**FREEFORM_PLANS, "S99": {"prompt": "A sexy pose.", "clip_prompt": None, "shot_meta": lint_meta(),
                                       "pre_approved_still": None}}
    report = lint_report("ep76", plans)
    assert sorted(report) == ["*", "S99"] and "no 'lint' section" in report["*"][0]
    assert report["S99"] == ["still: content-policy hard-stop term: 'sexy' (docs/CONTENT-POLICY.md)",
                             "clip: content-policy hard-stop term: 'sexy' (docs/CONTENT-POLICY.md)"]


# ---------------------------------------------------------------- draft-from-idea kits
DRAFT_ADMIN = "default-lint-draft-admin-0123456789abcdef"
IDEA = "A ringmaster and his stagehand keep a harbor show going through one stormy night"


@pytest.fixture
def draft_client(monkeypatch):
    """The API in dry mode, logged in as admin; each test mocks the story writer."""
    import sys

    from fastapi.testclient import TestClient

    from hfvg.config import config

    monkeypatch.setenv("ADMIN_SECRET", DRAFT_ADMIN)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    monkeypatch.setattr(config, "OPENAI_API_KEY", "test-key-for-mocked-calls")
    sys.modules.pop("api.main", None)
    from api.main import app

    client = TestClient(app)
    login = client.post("/api/login", json={"admin_secret": DRAFT_ADMIN})
    assert login.status_code == 200
    client.cookies.set("studio_admin_token", login.cookies.get("studio_admin_token"))
    return client


def _writer_reply() -> dict:
    """The story writer's files for the free-form fixture; its kit carries a model-invented lint object."""
    from hfvg import draft_repair

    shots = json.loads((FIXTURE / "shots.json").read_text(encoding="utf-8"))["shots"]
    rows = "\n".join(f"| {i} | {s['id']} | {s['duration']} ({s['duration']}) | Harbor | Night | A | beat {i} | Story "
                     "| - | - | K |" for i, s in enumerate(shots, start=1))
    return {"beatmap_md": f"# Episode Beatmap\n\n{draft_repair.TABLE_HEADER}\n{draft_repair.TABLE_SEPARATOR}\n{rows}\n",
            "shots_json": {"shots": shots}, "refs_json": {"per_shot": {s["id"]: [] for s in shots}},
            "prompt_kit_json": {**FREEFORM_KIT, "lint": SYNTHETIC_LINT_KIT}, "ip_steering_note": "",
            "title": "The Show Goes On"}


def _draft(client, source: str, monkeypatch) -> dict:
    from unittest.mock import patch

    from tests.test_draft_repair import _openai

    body = {"idea": IDEA, "target_duration": 30}
    if source == "fake":  # DRAFT_LLM_FAKE: the canned test draft (hfvg.genre_drafts)
        monkeypatch.setenv("DRAFT_LLM_FAKE", "1")
        res = client.post("/api/episodes/draft-from-idea", json=body)
    else:
        with patch("openai.AsyncOpenAI", return_value=_openai(_writer_reply())):
            res = client.post("/api/episodes/draft-from-idea", json=body)
    assert res.status_code == 200, res.text
    return res.json()


@pytest.mark.parametrize("source", ["writer", "fake"])
async def test_drafts_name_the_default_profile_and_pass_every_gate(draft_client, monkeypatch, source):
    import os

    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    from hfvg.budget import BudgetLedger

    draft = _draft(draft_client, source, monkeypatch)
    assert draft["prompt_kit_json"].get("lint") == "default", draft["prompt_kit_json"]

    # Stored as the create wizard uploads them (web/app/studio/create/page.tsx)
    ep = draft["episode_id"]
    d = paths.episode_dir(ep)
    d.mkdir(parents=True, exist_ok=True)
    (d / "BEATMAP.md").write_text(draft["beatmap"], encoding="utf-8")
    for name in ("shots", "refs", "prompt_kit"):
        (d / f"{name}.json").write_text(json.dumps(draft[f"{name}_json"]), encoding="utf-8")
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH")

    plans = parse_shots(draft["shots_json"])
    assert lint_summary(ep, plans) == {"ok": True, "profile": "default", "notice": None, "kit_error": None,
                                       "shots": {}, "problem_count": 0}
    for sid, plan in plans.items():
        for kind in ("still", "clip"):
            err = _gate(ep, plan, kind)
            assert err is None, f"{sid} {kind}: {err}"

    monkeypatch.setenv("HIGGSFIELD_API_KEY", "synthetic-not-a-key")
    ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await ledger.init_db()
    await ledger.set_manual_balance(50_000_000, set_by="test")
    try:
        await ActivityEnvironment().run(preflight_validate_episode_activity, ep, None, True)
    except PreflightError as e:
        pytest.fail(f"live preflight refused the draft: {e.problems}")
