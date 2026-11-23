"""An episode of the first series (prompt_kit.json and shots.json, no show.json) gets exactly what it
got before the show profile existed: lint and playbook messages, shot_meta keys in order, headcount
rosters and legacy reviewer rows, character sheet prompts, judge prompts and cache keys
(tests/legacy_goldens.py recorded them on that code). The legacy adapter that reads such an episode
is the profile written out in examples/first-series/show.json. Synthetic text only: no network, no
spend."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hfvg import show_profile
from hfvg.compat import legacy_show
from tests import legacy_goldens
from tests.lint_helpers import SYNTHETIC_LINT_KIT

FIRST_SERIES = Path(__file__).resolve().parent.parent / "examples" / "first-series" / "show.json"
HOST_SHOTS = {"shots": [
    {"id": "A01", "prompt": "Exactly 1 animals in frame. Real-time speed, no slow motion.", "count": 1,
     "characters": ["Duck"], "duck_present": True, "wet": False, "model": "kling", "duration": 5,
     "end_frame": False, "ref_keys": []},
    {"id": "A02", "prompt": "Exactly 2 animals in frame. Real-time speed, no slow motion.", "count": 2,
     "characters": ["Stag03"], "duck_present": False, "antler_character": True, "wet": False, "model": "kling",
     "duration": 5, "end_frame": False, "ref_keys": []},
]}


@pytest.mark.parametrize("surface", sorted(legacy_goldens.SURFACES))
def test_first_series_outputs_match_the_goldens(surface, tmp_path, monkeypatch):
    expected = legacy_goldens.load(surface)
    got = legacy_goldens.compute(surface, tmp_path, monkeypatch)
    assert sorted(got) == sorted(expected)
    for part in expected:
        assert got[part] == expected[part], f"{surface}.{part} differs from the recorded first-series output"


@pytest.fixture
def episodes(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    for name in legacy_goldens.CLEARED_ENV:
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "eps"


def _series_episode(episodes: Path, episode: str = "ep97", kit: dict | None = None) -> Path:
    folder = episodes / episode
    folder.mkdir(parents=True)
    (folder / "prompt_kit.json").write_text(json.dumps(
        {"style": "synthetic style", "characters": [], "lint": SYNTHETIC_LINT_KIT} if kit is None else kit))
    (folder / "shots.json").write_text(json.dumps(HOST_SHOTS))
    return folder


def test_the_adapter_profile_of_the_series_fixture_kit_is_the_first_series_file(episodes):
    _series_episode(episodes)
    adapted = show_profile.resolve_show_profile("ep97")
    written = show_profile.load_show_profile(FIRST_SERIES)
    assert adapted.source == "legacy-adapter" and written.source == "episode"
    assert adapted == written
    assert adapted.digest == written.digest
    # the file is the adapter's profile written out in full, key for key
    assert json.loads(FIRST_SERIES.read_text(encoding="utf-8")) == show_profile.profile_dict(adapted)


def test_the_first_series_file_holds_what_the_code_knew_about_the_series():
    profile = show_profile.load_show_profile(FIRST_SERIES)
    host = profile.identity_profiles["duck_host"]
    assert set(host.signs) == {"bill", "head", "neck_ring", "body", "too_small"}
    assert set(host.apparel) == {"tuque", "vest"} and host.state_features["B"][0] == "tuft"
    assert host.min_height_fraction == 0.12
    assert [(f.flag, f.figure_id, f.name) for f in profile.flag_figures] == [("duck", "_duck_host", "duck")]
    assert [(f.key, f.meta, f.declared_meta) for f in profile.shot_flags] == [
        ("antler_character", "antler", ""), ("duck_present", "duck", "duck_declared")]
    assert profile.lint.forbidden_phrases == ("nothing else moves", "realistic wild")
    assert profile.lint.forbid_bare_waves is True
    realism = next(r for r in profile.lint.forbid_near if r.id == "realism_split")
    assert "photoreal" in realism.words and realism.subjects == ("duck",)
    assert profile.playbook.rule_ids() == ("duck_role", "animal_poses", "same_resort")
    assert profile.scale_reference == "duck"
    assert profile.sheet_lead == "Character design reference of an original character."
    assert profile.real_people == "refuse"


def test_a_kit_without_a_lint_object_gets_the_default_lint_profile(episodes):
    _series_episode(episodes, kit={"style": "s", "characters": [], "continuity_locks": legacy_goldens.LOCKS})
    profile = show_profile.resolve_show_profile("ep97")
    assert profile.source == "legacy-adapter"
    assert profile.lint == legacy_show.default_lint_rules(legacy_goldens.LOCKS)
    assert profile.lint.forbidden_phrases == ("nothing else moves",) and profile.lint.content_policy_terms
    assert [lock.id for lock in profile.lint.continuity_locks] == ["duck_identity", "duck_clip", "animal_scale",
                                                                   "grade"]


def test_legacy_lock_when_strings_read_as_predicates():
    assert legacy_show.lock_when("duck") == {"flag": "duck"}
    assert legacy_show.lock_when("animals") == {"count_gt": 0}
    assert legacy_show.lock_when("always") == {"always": True}
    assert legacy_show.lock_when("duck", ("Duck",)) == {"any": [{"flag": "duck"}, {"characters_any": ["Duck"]}]}
    assert legacy_show.lock_when("animals", (), ("Otter1",)) == {
        "any": [{"count_gt": 0}, {"characters_any": ["Otter1"]}]}
    assert legacy_show.lock_when("sometimes") is None


def test_an_episode_without_any_file_gets_the_neutral_default(episodes):
    (episodes / "ep98").mkdir(parents=True)
    profile = show_profile.resolve_show_profile("ep98")
    assert profile.source == "default" and profile == show_profile.neutral_profile()
    assert profile.sheet_lead == "Character design reference."
    assert profile.flag_figures == () and profile.shot_flags == () and profile.playbook.rules == ()


def test_the_shots_and_lint_overrides_keep_an_episode_on_the_adapter(episodes, tmp_path, monkeypatch):
    shots = tmp_path / "shots.json"
    shots.write_text(json.dumps(HOST_SHOTS))
    monkeypatch.setenv("SHOTS_JSON_PATH", str(shots))
    assert show_profile.resolve_show_profile("ep99").source == "legacy-adapter"


def test_the_old_counter_module_re_exports_the_compat_module_with_a_deprecation_warning(monkeypatch):
    import importlib
    import sys
    import warnings

    from hfvg.compat import species_counter

    monkeypatch.delitem(sys.modules, "hfvg.qc.animal_count", raising=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        old = importlib.import_module("hfvg.qc.animal_count")
    assert [str(w.message) for w in caught if w.category is DeprecationWarning] == [
        "hfvg.qc.animal_count moved to hfvg.compat.species_counter"]
    assert {name: getattr(old, name) for name in species_counter.__all__} == {
        name: getattr(species_counter, name) for name in species_counter.__all__}
    assert len(Path(old.__file__).read_text(encoding="utf-8").splitlines()) == 3


def test_the_host_identity_gate_reads_its_rules_from_the_first_series_profile():
    from hfvg.compat.host_identity import DUCK_HOST_PROFILE

    data = json.loads(FIRST_SERIES.read_text(encoding="utf-8"))
    assert DUCK_HOST_PROFILE == show_profile.parse_profile(data).identity_profiles["duck_host"]


def test_a_lint_kit_still_answers_its_flat_switches():
    from hfvg.prompt_lint import LintKit, default_lint_kit

    kit = LintKit.from_dict(SYNTHETIC_LINT_KIT)
    assert (kit.forbid_bare_waves, kit.content_policy_terms) == (True, False)  # a series kit, as before
    assert (default_lint_kit().forbid_bare_waves, default_lint_kit().content_policy_terms) == (False, True)
    neutral = LintKit(rules=show_profile.neutral_profile().lint, profile="show")
    assert (neutral.forbid_bare_waves, neutral.content_policy_terms) == (False, True)
