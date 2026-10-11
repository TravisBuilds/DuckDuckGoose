"""Shared fixtures: a synthetic prompt-lint kit is always configured (kit presence only; each
test still has to supply a lint-clean prompt and shot_meta for live submits)."""

import pytest

from tests.lint_helpers import write_kit


@pytest.fixture(autouse=True)
def _synthetic_lint_kit(tmp_path, monkeypatch):
    kit_path = write_kit(tmp_path / "synthetic_lint_kit.json")
    monkeypatch.setenv("PROMPT_LINT_KIT_PATH", str(kit_path))
