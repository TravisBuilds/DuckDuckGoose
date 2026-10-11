"""P3: prompt lint gate (refuses before estimate/reserve/provider call). Synthetic fixtures only."""

import logging

import aiosqlite
import httpx
import pytest
import respx

from hfvg.prompt_lint import (
    LintKit,
    PromptLintError,
    enforce_prompt_lint,
    lint_prompt,
    lint_violations,
    load_lint_kit,
)
from tests.ledger_helpers import insert_line, set_balance
from tests.lint_helpers import (
    REALTIME,
    SYNTHETIC_LINT_KIT,
    acapture,
    capture,
    lint_meta,
    lint_ok,
    write_kit,
)

KIT = LintKit.from_dict(SYNTHETIC_LINT_KIT)
LOCKED = SYNTHETIC_LINT_KIT["locked_camera_clause"]


def _lint(prompt, **meta):
    return capture(PromptLintError, lint_prompt, prompt, lint_meta(**meta), KIT)


# ---------------------------------------------------------------- clean + count clause

def test_clean_prompt_passes():
    assert lint_violations(lint_ok("Synthetic scene", count=2), lint_meta(count=2), KIT) == []


def test_missing_count_clause_refused():
    err = _lint(f"Synthetic scene. {REALTIME}")
    assert err is not None, "a prompt without the count clause must be refused"
    assert "missing count clause" in str(err)


def test_count_mismatch_refused():
    err = _lint(lint_ok("Synthetic scene", count=3), count=2)
    assert err is not None, "count clause N != shot_meta.count must be refused"
    assert "mismatch" in str(err)


def test_bad_count_metadata_refused():
    assert _lint(lint_ok("x"), count="two") is not None
    assert _lint(lint_ok("x"), count=True) is not None


# ---------------------------------------------------------------- locked camera, real-time

def test_locked_camera_clause_required_on_exterior_and_water_shots():
    base = lint_ok("Synthetic scene")
    assert _lint(base, exterior=True) is not None
    assert _lint(base, water=True) is not None
    assert "locked-camera" in str(_lint(base, exterior=True))
    assert _lint(f"{base} {LOCKED}.", exterior=True) is None
    # interior shot does not need it
    assert _lint(base) is None


def test_realtime_clause_required():
    err = _lint("Synthetic scene. Exactly 1 animals in frame.")
    assert err is not None and "real-time" in str(err)


# ---------------------------------------------------------------- forbidden phrases

def test_nothing_else_moves_is_forbidden():
    err = _lint(lint_ok("The pond is calm, nothing else moves"))
    assert err is not None, "'nothing else moves' must be refused"
    assert "nothing else moves" in str(err)


def test_realistic_wild_is_forbidden():
    assert _lint(lint_ok("A realistic wild scene")) is not None


def test_bare_waves_forbidden_but_one_small_wave_allowed():
    assert _lint(lint_ok("Waves roll in")) is not None
    assert _lint(lint_ok("Gentle waves on the pond")) is not None
    assert _lint(lint_ok("ONE small wave crosses the pond")) is None


def test_moose_carry_or_hold_verbs_forbidden():
    for sentence in ("The moose carries a bag", "A moose is holding a cup", "The moose held the rope"):
        assert _lint(lint_ok(sentence)) is not None, sentence
    # same verb for another animal, or a moose sentence without such a verb, is fine
    assert _lint(lint_ok("The duck carries a bag")) is None
    assert _lint(lint_ok("The moose stands. The duck holds still")) is None


def test_banned_decor_words_forbidden():
    err = _lint(lint_ok("A gazebo stands by the pond"))
    assert err is not None, "banned decor word must be refused"
    assert "gazebo" in str(err)
    assert _lint(lint_ok("A Lantern glows")) is not None
    assert _lint(lint_ok("The gazeboes")) is None  # whole-word match only


def test_banned_decor_list_is_configurable():
    kit = LintKit.from_dict({**SYNTHETIC_LINT_KIT, "banned_decor": ["teapot"]})
    assert capture(PromptLintError, lint_prompt, lint_ok("A teapot"), lint_meta(), kit) is not None
    assert capture(PromptLintError, lint_prompt, lint_ok("A gazebo"), lint_meta(), kit) is None


# ---------------------------------------------------------------- antler / duck clauses

def test_antler_clause_required_when_antler_character_present():
    base = lint_ok("Birch stands")
    err = _lint(base, characters=["Birch"])
    assert err is not None, "missing antler clause must be refused when Birch is present"
    assert "antler clause" in str(err)
    ok = f"{base}. {SYNTHETIC_LINT_KIT['antler_clause']}."
    assert _lint(ok, characters=["Birch"]) is None
    assert _lint(base, characters=[]) is None


def test_duck_clause_required_when_duck_present():
    base = lint_ok("A duck swims")
    err = _lint(base, characters=["Duck"])
    assert err is not None and "duck clause" in str(err)
    assert _lint(f"{base}. {SYNTHETIC_LINT_KIT['duck_clause']}.", characters=["Duck"]) is None


def test_kit_without_antler_clause_fails_closed_when_antler_character_present():
    kit = LintKit.from_dict({**SYNTHETIC_LINT_KIT, "antler_clause": ""})
    err = capture(PromptLintError, lint_prompt, lint_ok("x"), lint_meta(characters=["Birch"]), kit)
    assert err is not None and "no antler_clause" in str(err)


# ---------------------------------------------------------------- kit loading (never hardcoded)

def test_incomplete_kit_refused():
    for missing in ("count_template", "realtime_pattern", "locked_camera_clause"):
        data = {k: v for k, v in SYNTHETIC_LINT_KIT.items() if k != missing}
        assert capture(PromptLintError, LintKit.from_dict, data) is not None, missing
    bad = {**SYNTHETIC_LINT_KIT, "count_template": "no placeholder"}
    assert capture(PromptLintError, LintKit.from_dict, bad) is not None
    bad = {**SYNTHETIC_LINT_KIT, "realtime_pattern": "(unclosed"}
    assert capture(PromptLintError, LintKit.from_dict, bad) is not None


def test_clause_texts_come_from_the_kit(tmp_path, monkeypatch):
    path = write_kit(tmp_path / "k.json", {"count_template": "Total {n} creatures visible"})
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(path))
    kit = load_lint_kit("ep-any")
    assert capture(PromptLintError, lint_prompt, lint_ok("x"), lint_meta(), kit) is not None
    good = f"x. Total 1 creatures visible. {REALTIME}"
    assert capture(PromptLintError, lint_prompt, good, lint_meta(), kit) is None


def test_per_episode_prompt_kit_lint_section(tmp_path, monkeypatch):
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    (tmp_path / "ep77").mkdir()
    (tmp_path / "ep77" / "prompt_kit.json").write_text(
        '{"style": "s", "lint": ' + __import__("json").dumps(SYNTHETIC_LINT_KIT) + "}"
    )
    assert load_lint_kit("ep77").count_template == "Exactly {n} animals in frame"
    assert capture(PromptLintError, load_lint_kit, "ep-missing") is not None


def test_enforce_live_refuses_dry_warns(caplog):
    bad = "Synthetic scene with no clauses"
    live = capture(PromptLintError, enforce_prompt_lint, "ep99", bad, lint_meta(), True)
    assert live is not None, "live mode must refuse"
    assert capture(PromptLintError, enforce_prompt_lint, "ep99", bad, None, True) is not None
    with caplog.at_level(logging.WARNING, logger="hfvg.prompt_lint"):
        problems = enforce_prompt_lint("ep99", bad, lint_meta(), live=False)
    assert problems, "dry mode reports the problems"
    assert any("prompt lint warning" in r.message for r in caplog.records)


def test_enforce_live_fails_closed_without_kit(monkeypatch, tmp_path):
    monkeypatch.delenv("PROMPT_LINT_KIT_PATH", raising=False)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    err = capture(PromptLintError, enforce_prompt_lint, "ep99", lint_ok("x"), lint_meta(), True)
    assert err is not None and "No prompt lint kit" in str(err)


# ---------------------------------------------------------------- activity wiring

async def _live_db(tmp_path, monkeypatch, line="L2_drafts"):
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

    db_path = str(tmp_path / "lint.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await BudgetLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", line, 10_000_000, 8_000_000)
        await db.commit()
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    return db_path


@pytest.mark.asyncio
@respx.mock
async def test_live_still_lint_failure_refuses_before_estimate_reserve_and_submit(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_db(tmp_path, monkeypatch)
    est = respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"})
    )
    sub = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "never"})
    )
    err = await acapture(PromptLintError, submit_still_job_enforced(
        "ep99", "A01", "Synthetic scene with no clauses", 1, shot_meta=lint_meta(),
    ))
    assert err is not None, "live still with a lint-failing prompt must be refused"
    assert est.call_count == 0 and sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0 and status["spent"] == 0
    assert await BudgetLedger(db_path).get_jobs("ep99") == []


@pytest.mark.asyncio
@respx.mock
async def test_live_still_without_shot_meta_refused(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_db(tmp_path, monkeypatch)
    err = await acapture(PromptLintError, submit_still_job_enforced("ep99", "A01", lint_ok("x"), 1))
    assert err is not None and "shot_meta is required" in str(err)


@pytest.mark.asyncio
@respx.mock
async def test_live_clip_lint_failure_refuses_before_estimate_reserve_and_submit(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.budget import BudgetLedger

    db_path = await _live_db(tmp_path, monkeypatch, line="L4_video")
    est = respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"})
    )
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "never"})
    )
    err = await acapture(PromptLintError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", "Synthetic clip, nothing else moves", 5.0, 1,
        shot_meta=lint_meta(),
    ))
    assert err is not None, "live clip with a lint-failing prompt must be refused"
    assert est.call_count == 0 and sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L4_video")
    assert status["reserved"] == 0


@pytest.mark.asyncio
async def test_dry_still_with_bad_prompt_warns_and_does_not_refuse(tmp_path, monkeypatch, caplog):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    monkeypatch.setenv("DRY_RUN", "true")
    with caplog.at_level(logging.WARNING, logger="hfvg.prompt_lint"):
        result = await submit_still_job_enforced("ep99", "A01", "no clauses at all", 1, shot_meta=lint_meta())
    assert result["job_id"].startswith("still-dry-")
    assert any("prompt lint warning" in r.message for r in caplog.records)
