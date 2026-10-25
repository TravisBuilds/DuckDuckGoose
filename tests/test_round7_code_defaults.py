"""Round 7 B6: every request default in code is allowed by model_caps for the model it goes to.

1. hfvg.defaults declares each default with its model; validate_code_defaults checks them all
   against the model caps registry (resolutions, quality, aspect ratios, duration range/integer).
2. A source scan makes the declaration complete: every literal default for resolution / quality /
   aspect_ratio / duration in hfvg/activities, hfvg/workflows and hfvg/cli.py (``.get(field, X)``,
   parameter defaults, click ``default=``) must be one of the declared (validated) values.
"""

import ast
from pathlib import Path

import pytest

from hfvg import defaults

REPO = Path(__file__).resolve().parent.parent
SCAN = [*sorted((REPO / "hfvg" / "activities").glob("*.py")), *sorted((REPO / "hfvg" / "workflows").glob("*.py")),
        REPO / "hfvg" / "cli.py"]
FIELDS = tuple(defaults.FIELD_LIMITS)


def _literal_defaults():
    found = []
    for path in SCAN:
        tree = ast.parse(path.read_text(), str(path))
        for node in ast.walk(tree):
            # mapping.get("field", literal)
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and len(node.args) == 2 and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in FIELDS and isinstance(node.args[1], ast.Constant)):
                found.append((path, node.lineno, node.args[0].value, node.args[1].value))
            # def f(resolution="1k", *, duration=5.0)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = node.args
                pos = a.posonlyargs + a.args
                pairs = list(zip(pos[len(pos) - len(a.defaults):], a.defaults)) + list(zip(a.kwonlyargs, a.kw_defaults))
                for arg, d in pairs:
                    if arg.arg in FIELDS and isinstance(d, ast.Constant):
                        found.append((path, node.lineno, arg.arg, d.value))
            # click.option("--resolution", default="1k")
            if (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str) and node.args[0].value.lstrip("-") in FIELDS):
                for kw in node.keywords:
                    if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                        found.append((path, node.lineno, node.args[0].value.lstrip("-"), kw.value.value))
    return [f for f in found if f[3] is not None]


def test_declared_defaults_are_allowed_by_model_caps():
    assert defaults.validate_code_defaults() == []


def test_declared_defaults_cover_every_model_the_code_defaults_to():
    from hfvg.config import still_model_path
    from hfvg.preflight import DEFAULT_CLIP_PATHS, clip_model_path
    models = {r["model"] for r in defaults.code_defaults()}
    assert still_model_path() in models and defaults.REF_MODEL_PATH in models
    assert {clip_model_path(a) for a in DEFAULT_CLIP_PATHS} <= models


def test_every_literal_default_in_code_is_a_declared_value():
    found = _literal_defaults()
    assert len(found) > 10, "the scan must actually find the defaults"
    bad = [f"{p.relative_to(REPO)}:{line} {field}={value!r}"
           for p, line, field, value in found if value not in defaults.declared_values(field)]
    assert bad == [], "undeclared / unvalidated defaults (declare them in hfvg/defaults.py):\n" + "\n".join(bad)


def test_ref_generation_default_is_the_declared_caps_valid_one():
    import inspect
    from hfvg.activities.ref_generation import generate_ref_activity
    sig = inspect.signature(generate_ref_activity)
    assert sig.parameters["resolution"].default == defaults.REF_RESOLUTION
    from hfvg import model_caps
    allowed = model_caps.get_registry().resolve(defaults.REF_MODEL_PATH).limit("resolutions").value
    assert defaults.REF_RESOLUTION in allowed


def test_validation_bites_on_a_bad_default(monkeypatch):
    monkeypatch.setattr(defaults, "REF_RESOLUTION", "2k")
    problems = defaults.validate_code_defaults()
    assert any("REF_RESOLUTION" in p and "'2k'" in p for p in problems), problems
    monkeypatch.setattr(defaults, "REF_RESOLUTION", "1080p")
    monkeypatch.setattr(defaults, "CLIP_DURATION_S", 2)
    problems = defaults.validate_code_defaults()
    assert any("CLIP_DURATION_S(kling)" in p for p in problems), problems


def test_check_value_duration_rules():
    from hfvg import model_caps
    caps = model_caps.get_registry().resolve("kling-video/v3.0/pro/image-to-video")
    assert defaults.check_value(caps, "duration", 5) is None
    assert "outside" in defaults.check_value(caps, "duration", 16)
    assert "whole number" in defaults.check_value(caps, "duration", 5.5)


@pytest.mark.asyncio
async def test_generate_ref_refuses_a_resolution_the_ref_model_does_not_accept(tmp_path, monkeypatch):
    """Validated before any reserve / provider call (the old default '2k' is not a SOUL v2 value)."""
    from hfvg.activities.ref_generation import generate_ref_activity
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    with pytest.raises(ValueError, match="resolution '2k' refused"):
        await generate_ref_activity("ep99", "family", "a family portrait", str(tmp_path / "f.png"), 1,
                                    resolution="2k")
