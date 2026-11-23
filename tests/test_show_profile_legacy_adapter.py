"""An episode of the first series (prompt_kit.json and shots.json, no show.json) gets exactly what it
got before the show profile existed: lint and playbook messages, shot_meta keys in order, headcount
rosters and legacy reviewer rows, character sheet prompts, judge prompts and cache keys
(tests/legacy_goldens.py recorded them on that code). Synthetic text only: no network, no spend."""

from __future__ import annotations

import pytest

from tests import legacy_goldens


@pytest.mark.parametrize("surface", sorted(legacy_goldens.SURFACES))
def test_first_series_outputs_match_the_goldens(surface, tmp_path, monkeypatch):
    expected = legacy_goldens.load(surface)
    got = legacy_goldens.compute(surface, tmp_path, monkeypatch)
    assert sorted(got) == sorted(expected)
    for part in expected:
        assert got[part] == expected[part], f"{surface}.{part} differs from the recorded first-series output"
