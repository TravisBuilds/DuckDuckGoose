"""Bath rule in the prompt lint: animals soak shoulder-deep with a visible waterline (Studio 2026-10-27)."""

import pytest

from hfvg.prompt_lint import LintKit, PromptLintError, is_bath_shot, lint_violations, clip_lint_violations

CLAUSE = "submerged up to the shoulders, visible waterline, body partly underwater, ripples"
BASE = "Exactly 3 animals in frame, soaking. No camera drift. Real-time speed, no slow motion."
BATH = {"count": 3, "wet": True, "water": True, "exterior": True, "characters": ["R", "H", "M"]}


def kit(**extra):
    d = {"count_template": "Exactly {n} animals in frame", "realtime_pattern": "real-time speed.{0,40}no slow motion",
         "locked_camera_clause": "no camera drift"}
    d.update(extra)
    return LintKit.from_dict(d)


def test_bath_shot_detection():
    assert is_bath_shot(BATH)
    assert not is_bath_shot({**BATH, "wet": False})          # dry deck by the pool
    assert not is_bath_shot({**BATH, "count": 0})            # empty pool / duck-only
    assert not is_bath_shot({**BATH, "water": False})
    assert is_bath_shot({"count": 0, "bath": True})          # explicit flag wins
    assert not is_bath_shot({**BATH, "bath": False})
    assert not is_bath_shot(None)


def test_bath_clause_required_on_bath_shot_only():
    k = kit(bath_clause=CLAUSE)
    v = lint_violations(BASE, BATH, k)
    assert any("missing bath clause" in x for x in v)
    assert lint_violations(BASE + " " + CLAUSE.capitalize() + ".", BATH, k) == []
    dry = {**BATH, "wet": False}
    assert lint_violations(BASE, dry, k) == []
    # clip prompts get the same rule
    assert any("missing bath clause" in x for x in clip_lint_violations(BASE, BATH, k))


def test_bath_forbidden_phrases():
    k = kit(bath_clause=CLAUSE, bath_forbidden=["on top of the water", "floating on the surface"])
    p = BASE + " " + CLAUSE + ". char_m floats ON TOP OF THE WATER."
    v = lint_violations(p, BATH, k)
    assert v == ["bath shot prompt puts an animal on the water: 'on top of the water'"]
    assert lint_violations(p, {**BATH, "wet": False}, k) == []


def test_kit_without_bath_rule_is_unchanged():
    assert lint_violations(BASE, BATH, kit()) == []


def test_bath_forbidden_must_be_list():
    with pytest.raises(PromptLintError, match="bath_forbidden"):
        kit(bath_forbidden="on top of the water")
