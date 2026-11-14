"""Cast prompt rules (hfvg/cast_prompt.py): every still prompt carries the locked description of each
character in frame and the default no-text constraint, the still submit injects them (dry and live,
before the lint, estimate and spend), the lint requires them, and legacy shots are unchanged."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
import respx

from hfvg import cast_prompt
from hfvg.cast_prompt import (
    CAST_LEAD_IN,
    NO_TEXT_CLAUSE,
    cast_lint_violations,
    character_clause,
    draft_cast,
    inject_cast_prompt,
    text_requests,
    with_cast_prompts,
)
from hfvg.prompt_lint import PromptLintError, enforce_prompt_lint, lint_summary, lint_violations, load_lint_kit
from tests.ledger_helpers import set_balance
from tests.lint_helpers import acapture, lint_meta, lint_ok

PIP = "Pip: a small round robot with one green eye; outfit: a yellow raincoat."
MOSS = "Moss: a tall heron made of folded paper."


def _meta(*clauses: str, text_policy: str | None = "no_text", **kw) -> dict:
    meta = lint_meta(cast=[{"character": c.split(":")[0].lower(), "version": 1, "clause": c} for c in clauses], **kw)
    if text_policy:
        meta["text_policy"] = text_policy
    return meta


# ------------------------------------------------------------------------------------- clauses, injection


def test_the_character_clause():
    assert character_clause("Pip", "a small round robot with one green eye.", "a yellow raincoat") == PIP
    assert character_clause(" Moss ", "  a tall heron\nmade of folded paper ") == MOSS
    assert character_clause("Moss", "") == "Moss."


def test_injection_adds_each_description_and_the_no_text_constraint_once():
    prompt, added = inject_cast_prompt("Two friends share an umbrella on the pier.", _meta(PIP, MOSS))
    assert prompt == (f"Two friends share an umbrella on the pier. {CAST_LEAD_IN} {PIP} {MOSS} {NO_TEXT_CLAUSE}")
    assert added == ["cast:pip", "cast:moss", "no_text"]
    again, added_again = inject_cast_prompt(prompt, _meta(PIP, MOSS))
    assert (again, added_again) == (prompt, [])
    # already there in other case/spacing: not added again
    upper = f"Pier scene. {PIP.upper()}  {NO_TEXT_CLAUSE.lower()}"
    out, added = inject_cast_prompt(upper, _meta(PIP))
    assert added == [] and out == upper


def test_only_missing_clauses_are_added():
    prompt, added = inject_cast_prompt(f"Pier scene with {PIP}", _meta(PIP, MOSS))
    assert added == ["cast:moss", "no_text"] and prompt.count("Pip:") == 1 and MOSS in prompt


def test_the_no_text_constraint_alone_and_allowed_text():
    out, added = inject_cast_prompt("A title card over the sea.", {"text_policy": "no_text"})
    assert out.endswith(NO_TEXT_CLAUSE) and added == ["no_text"]
    out, added = inject_cast_prompt("A title card that reads 'Harbor Tales'.", _meta(PIP, text_policy=None))
    assert NO_TEXT_CLAUSE not in out and PIP in out


def test_legacy_shot_meta_changes_nothing():
    for meta in (None, {}, lint_meta(), {"cast": []}, {"cast": [{"character": "pip", "clause": "  "}]},
                 {"text_policy": "allow_text"}):
        prompt = "  A quiet harbor at dawn.  "
        assert inject_cast_prompt(prompt, meta) == (prompt, [])
        assert cast_lint_violations("speech bubble that says 'hi'", meta) == []


# ------------------------------------------------------------------------------------- lint


def test_the_lint_requires_the_descriptions_and_the_no_text_constraint():
    problems = cast_lint_violations("Pier scene.", _meta(PIP, MOSS))
    assert len(problems) == 3
    assert "'pip'" in problems[0] and "'moss'" in problems[1] and NO_TEXT_CLAUSE in problems[2]
    full, _ = inject_cast_prompt("Pier scene.", _meta(PIP, MOSS))
    assert cast_lint_violations(full, _meta(PIP, MOSS)) == []


@pytest.mark.parametrize("prompt", [
    "Pip holds a sign that says 'hello'",
    "a speech bubble above Moss",
    "comic panel with word balloons",
    "a caption at the bottom",
    "Bold typography across the sky",
    "the banner reads: 'Welcome'",
    "with subtitles",
    "a note written on the wall",
    "hand lettering on the boat",
])
def test_the_lint_names_requests_for_on_image_text(prompt):
    full, _ = inject_cast_prompt(prompt, _meta(PIP))
    problems = cast_lint_violations(full, _meta(PIP))
    assert problems and all("asks for on-image text" in p for p in problems)
    assert cast_lint_violations(full, _meta(PIP, text_policy="allow_text")) == []


@pytest.mark.parametrize("prompt", [
    "no speech bubbles, no captions",
    "without lettering or subtitles",
    "never any typography",
    "a clean frame free of captions",
    "Pip reads a book quietly",
])
def test_negated_mentions_are_fine(prompt):
    assert text_requests(prompt) == []


def test_rule_13_is_part_of_the_still_lint_only():
    kit = load_lint_kit("ep99")
    prompt = lint_ok("Pier scene")
    meta = _meta(PIP)
    still = lint_violations(prompt, meta, kit, kind="still")
    assert any("locked description" in p for p in still) and any("no-text" in p for p in still)
    assert not any("locked description" in p or "no-text" in p for p in lint_violations(prompt, meta, kit, kind="clip"))
    assert lint_violations(inject_cast_prompt(prompt, meta)[0], meta, kit) == []


def test_live_lint_refuses_a_prompt_without_the_cast_clauses():
    with pytest.raises(PromptLintError, match="locked description of cast character 'pip'"):
        enforce_prompt_lint("ep99", lint_ok("Pier scene"), _meta(PIP), live=True)
    assert enforce_prompt_lint("ep99", inject_cast_prompt(lint_ok("Pier scene"), _meta(PIP))[0], _meta(PIP),
                               live=True) == []


def test_the_preflight_lint_injects_like_the_submit_and_names_text_requests():
    plans = {
        "A01": {"prompt": lint_ok("Pier scene"), "shot_meta": _meta(PIP)},
        "A02": {"prompt": lint_ok("Pip stands under a speech bubble"), "shot_meta": _meta(PIP)},
        "A03": {"prompt": lint_ok("Harbor"), "shot_meta": lint_meta()},
    }
    summary = lint_summary("ep99", plans)
    assert set(summary["shots"]) == {"A02"}
    assert len(summary["shots"]["A02"]) == 1
    assert summary["shots"]["A02"][0].startswith("still: the prompt asks for on-image text ('speech bubble')")


def test_the_g1_preview_shows_the_prompts_as_submitted():
    shots = [{"shot_id": "A01", "prompt": "Pier scene.", "end_prompt": "Pier at dusk.", "shot_meta": _meta(PIP)},
             {"shot_id": "A02", "prompt": "Harbor.", "shot_meta": lint_meta()}]
    out = with_cast_prompts(shots)
    assert out[0]["prompt"].endswith(f"{PIP} {NO_TEXT_CLAUSE}") and PIP in out[0]["end_prompt"]
    assert out[1] is shots[1] and shots[0]["prompt"] == "Pier scene."


# ------------------------------------------------------------------------------------- the still submit


async def test_the_dry_still_submit_injects_before_the_lint(monkeypatch):
    from hfvg.activities import studio_generation as sg

    seen = {}

    def capture_lint(episode_id, prompt, shot_meta, live, kind="still"):
        seen["prompt"], seen["live"] = prompt, live
        return []
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(sg, "enforce_prompt_lint", capture_lint)
    result = await sg.submit_still_job_enforced("ep99", "A01", "Pier scene.", 1, shot_meta=_meta(PIP, MOSS))
    assert result["job_id"].startswith("still-dry-")
    assert seen == {"prompt": f"Pier scene. {CAST_LEAD_IN} {PIP} {MOSS} {NO_TEXT_CLAUSE}", "live": False}
    await sg.submit_still_job_enforced("ep99", "A01", "Pier scene.", 1, shot_meta=lint_meta())
    assert seen["prompt"] == "Pier scene."  # no cast, no text policy: exactly as before


async def test_the_dry_still_submit_still_never_refuses(monkeypatch, caplog):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    monkeypatch.setenv("DRY_RUN", "true")
    with caplog.at_level(logging.WARNING, logger="hfvg.prompt_lint"):
        result = await submit_still_job_enforced("ep99", "A01", "Pip under a speech bubble", 1,
                                                 shot_meta=_meta(PIP))
    assert result["job_id"].startswith("still-dry-")
    assert any("asks for on-image text" in r.message for r in caplog.records)


async def _live_episode(tmp_path, monkeypatch) -> str:
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

    db_path = str(tmp_path / "cast-live.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.set_line("ep99", "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)
    credits = CreditLedger(db_path)
    await credits.init_db()
    await credits.create_episode_line("ep99", cap_credits=500)
    for key, value in {"DRY_RUN": "false", "SMOKE_TEST_OVERRIDE": "true", "DATABASE_PATH": db_path,
                       "HIGGSFIELD_API_KEY": "test_id:test_secret", "HIGGSFIELD_BASE_URL": "https://api.higgsfield.ai",
                       "MODEL_PATH_GPT_IMAGE_2": "xai/grok-imagine-image-2.0"}.items():
        monkeypatch.setenv(key, value)
    return db_path


@respx.mock
async def test_the_live_still_submit_sends_the_descriptions_and_the_no_text_constraint(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_episode(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "provider-cast-1"}))
    result = await submit_still_job_enforced("ep99", "A01", lint_ok("Two friends on the pier"), 1,
                                             shot_meta=_meta(PIP, MOSS))
    assert result["job_id"] == "still-ep99-A01-v1" and sub.call_count == 1
    sent = json.loads(sub.calls.last.request.content)
    text = json.dumps(sent)
    assert PIP in text and MOSS in text and NO_TEXT_CLAUSE in text and CAST_LEAD_IN in text


@respx.mock
async def test_a_live_still_asking_for_text_is_refused_before_any_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_episode(tmp_path, monkeypatch)
    est = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"}))
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"}))
    err = await acapture(PromptLintError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("Pip holds a sign that says 'hello'"), 1, shot_meta=_meta(PIP)))
    assert err is not None and "asks for on-image text" in str(err)
    assert est.call_count == 0 and sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0 and status["spent"] == 0


# ------------------------------------------------------------------------------------- draft from idea


def test_draft_cast_turns_kit_characters_into_cast_entries():
    kit = {"characters": {"A": {"name": "Pip", "role": "lead", "description": "a small round robot"},
                          "B": {"name": "Pip", "description": "a second robot"},
                          "C": "a paper heron"},
           "lint": "default"}
    shots = [{"id": "A01", "characters": ["A", "C"]}, {"id": "A02", "characters": ["B"]}, {"id": "A03"}]
    new_kit, cast = draft_cast(kit, shots)
    assert [(c["code"], c["slug"], c["name"], c["role"], c["shots"]) for c in cast] == [
        ("A", "pip", "Pip", "lead", ["A01"]),
        ("B", "pip-2", "Pip", "", ["A02"]),
        ("C", "character-c", "Character C", "", ["A01"]),
    ]
    assert cast[2]["description"] == "a paper heron"
    assert new_kit["text_policy"] == "no_text" and new_kit["characters"]["C"]["name"] == "Character C"
    assert shots[0] == {"id": "A01", "characters": ["A", "C"]}  # shots are not changed
    kept, _ = draft_cast({**kit, "text_policy": "allow_text"}, shots)
    assert kept["text_policy"] == "allow_text"
    empty_kit, empty = draft_cast({"lint": "default"}, shots)
    assert empty == [] and empty_kit["characters"] == {}


def test_slugify():
    assert cast_prompt.slugify("Moss Fern!") == "moss-fern"
    assert cast_prompt.slugify("???") == "character"
    assert len(cast_prompt.slugify("x" * 80)) == 32
