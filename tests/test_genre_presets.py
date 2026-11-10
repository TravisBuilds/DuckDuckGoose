"""Genre preset registry: loading, validation, inheritance and the JSON profiles the API resolves."""

import json
import re
from pathlib import Path

import pytest
import yaml

from hfvg.genre_presets import (
    DRAFT_MAX_S,
    DRAFT_MIN_S,
    FREEFORM_PROFILE,
    REGISTRY_PATH,
    GenrePreset,
    IPMode,
    Template,
    VOMode,
    clamp_length,
    get_genre_presets_registry,
    load_genre_presets,
    resolve_profile,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
YAML_IDS = [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]


def test_registry_file_is_the_repo_copy():
    assert REGISTRY_PATH == REPO_ROOT / "hfvg" / "data" / "genre_presets.yaml"
    assert len(YAML_IDS) == 24


def test_load_genre_presets():
    registry = load_genre_presets()

    assert registry.schema_version == "0.1"
    assert len(registry.templates) == 7
    assert len(registry.presets) == 24
    for template in Template:
        assert template.value in registry.templates


def test_all_preset_ids_match_web_presets():
    ts_text = (REPO_ROOT / "web" / "lib" / "presets.ts").read_text(encoding="utf-8")
    ts_ids = set(re.findall(r'^    id: "([^"]+)",$', ts_text, flags=re.MULTILINE))
    assert set(load_genre_presets().presets) == set(YAML_IDS) == ts_ids


def test_no_duplicate_preset_ids_and_no_validation_errors():
    registry = load_genre_presets()
    assert len(set(YAML_IDS)) == len(YAML_IDS)
    assert not registry.validate_all()


def test_duplicate_id_is_rejected(tmp_path):
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["presets"].append(dict(data["presets"][0]))
    bad = tmp_path / "presets.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_genre_presets(bad)


def test_default_length_outside_range_is_rejected(tmp_path):
    data = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    data["presets"][0]["default_length_s"] = data["presets"][0]["length_s"][1] + 1
    bad = tmp_path / "presets.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="default_length_s"):
        load_genre_presets(bad)


def test_no_named_ip_in_presets():
    registry = load_genre_presets()
    banned_terms = [
        "disney", "pixar", "ghibli", "marvel", "dc comics", "pokemon", "naruto",
        "harry potter", "star wars", "lord of the rings", "attack on titan",
        "demon slayer", "spirited away", "jurassic park", "blockbuster", "kfc",
        "miffy", "boris", "stitch", "totoro", "spongebob",
    ]
    violations = [
        f"{p.id}: contains '{term}'"
        for p in registry.presets.values()
        for term in banned_terms
        if term in json.dumps(p.profile()).lower() or term in p.description.lower()
    ]
    assert not violations, "Named IP violations:\n" + "\n".join(violations)


@pytest.mark.parametrize("preset_id", YAML_IDS)
def test_preset_length_range_and_default(preset_id):
    preset = load_genre_presets().get_preset(preset_id)
    lo, hi = preset.length_s
    assert DRAFT_MIN_S <= lo <= hi <= DRAFT_MAX_S
    assert lo <= preset.default_length_s <= hi


@pytest.mark.parametrize("preset_id", YAML_IDS)
def test_preset_fields_are_populated(preset_id):
    preset = load_genre_presets().get_preset(preset_id)
    assert preset.name
    assert preset.description
    assert preset.template
    assert 2 <= len(preset.brief_fields) <= 5
    assert preset.vo_mode
    assert preset.aspect in ["9:16", "16:9", "4:3", "16:9_rotated_to_9:16"]


@pytest.mark.parametrize("preset_id", YAML_IDS)
def test_profile_is_json_and_carries_structure(preset_id):
    preset = load_genre_presets().get_preset(preset_id)
    profile = preset.profile()
    assert json.loads(json.dumps(profile)) == profile
    template = load_genre_presets().templates[preset.template.value]
    assert profile["story_structure"] == template.story_structure.value
    assert profile["length_s"] == preset.length_s
    assert profile["default_length_s"] == preset.default_length_s
    assert profile["vo_mode"] == preset.vo_mode.value


def test_get_preset_by_id():
    registry = load_genre_presets()
    preset = registry.get_preset("nostalgia-time-travel")
    assert isinstance(preset, GenrePreset)
    assert preset.template == Template.DIALOGUE_EPISODE
    assert preset.vo_mode == VOMode.DIALOGUE
    assert registry.get_preset("does-not-exist") is None


def test_get_presets_by_template():
    registry = load_genre_presets()
    mood_loops = registry.get_presets_by_template(Template.MOOD_LOOP)
    assert all(p.template == Template.MOOD_LOOP for p in mood_loops)
    assert {p.id for p in mood_loops} >= {
        "farewell-tribute", "living-painting", "cozy-hangout",
        "dreamscape", "ambience-journey", "character-spotlight",
    }


def test_template_inheritance():
    registry = load_genre_presets()
    preset = registry.get_preset("living-painting")
    template = registry.templates[Template.MOOD_LOOP.value]
    assert preset.video_prompt_mode == template.video_prompt_mode
    assert preset.story_structure == template.story_structure


def test_story_structures_differ_by_template():
    structures = {p.template.value: p.profile()["story_structure"]
                  for p in load_genre_presets().presets.values()}
    assert structures["mood_loop"] == "loop"
    assert structures["music_montage"] == "montage"
    assert structures["gag_short"] == "escalation_punchline"
    assert structures["continuous_pov"] == "continuous_journey"
    assert structures["dialogue_episode"] == "episode_cliffhanger"
    assert "three_act" not in structures.values()


def test_risk_flags_on_sensitive_presets():
    registry = load_genre_presets()
    satire = registry.get_preset("satire-sketch")
    assert any("political" in f.value for f in satire.risk_flags)
    assert any("likeness" in f.value for f in satire.risk_flags)
    fight = registry.get_preset("live-action-fight")
    assert any("violence" in f.value for f in fight.risk_flags)


def test_gates_add_for_high_risk_presets():
    registry = load_genre_presets()
    assert any("manual_review" in g.value for g in registry.get_preset("satire-sketch").gates_add)
    tribute = registry.get_preset("farewell-tribute")
    assert any("likeness_consent" in g.value for g in tribute.gates_add)
    assert any("fact_check" in g.value for g in tribute.gates_add)


def test_ip_mode_for_thirdparty_presets():
    registry = load_genre_presets()
    assert registry.get_preset("cartoon-made-real").ip_mode in (IPMode.PUBLIC_DOMAIN_OK, IPMode.ORIGINAL_ONLY)
    assert registry.get_preset("original-mini-movie").ip_mode == IPMode.ORIGINAL_ONLY


def test_dialogue_presets_have_dialogue_vo_mode():
    registry = load_genre_presets()
    for preset in registry.get_presets_by_template(Template.DIALOGUE_EPISODE):
        assert preset.vo_mode == VOMode.DIALOGUE, preset.id


def test_loop_presets_have_reasonable_lengths():
    registry = load_genre_presets()
    for preset in registry.get_presets_by_template(Template.MOOD_LOOP):
        assert preset.length_s[0] >= 10, preset.id
        assert preset.length_s[1] <= 90, preset.id
    assert registry.get_preset("ambience-journey").length_s == [60, 90]


def test_action_sequences_have_moderation_gates():
    registry = load_genre_presets()
    for preset in registry.get_presets_by_template(Template.ACTION_SEQUENCE):
        gates = [g.value for g in preset.gates_add]
        assert "moderation_precheck" in gates, preset.id
        assert "manual_review" in gates, preset.id


def test_continuous_pov_presets_have_continuous_mode():
    registry = load_genre_presets()
    for preset in registry.get_presets_by_template(Template.CONTINUOUS_POV):
        assert preset.video_prompt_mode is not None
        assert preset.video_prompt_mode.value == "continuous", preset.id


def test_registry_singleton():
    assert get_genre_presets_registry() is get_genre_presets_registry()


def test_aspect_ratio_diversity():
    registry = load_genre_presets()
    aspects = {p.aspect for p in registry.presets.values()}
    assert {"9:16", "16:9", "4:3"} <= aspects
    assert registry.get_preset("what-if-ending").aspect == "16:9"
    assert registry.get_preset("ambience-journey").aspect == "4:3"


def test_fullscreen_action_has_rotate_to_fill():
    fullscreen = load_genre_presets().get_preset("fullscreen-action-anime")
    assert fullscreen.aspect == "16:9_rotated_to_9:16"
    assert "rotate_to_fill" in fullscreen.stage_overrides.get("assembly", "")


def test_cozy_micro_presets_are_short():
    registry = load_genre_presets()
    for preset in registry.get_presets_by_template(Template.COZY_MICRO):
        assert preset.length_s[0] >= 10
        assert preset.length_s[1] <= 20, preset.id


def test_resolve_profile_freeform_for_skip():
    for archetype in (None, "", "freeform"):
        profile = resolve_profile(archetype)
        assert profile == FREEFORM_PROFILE
        assert profile["length_s"] == [30, 120]
        assert profile["default_length_s"] == 60
        assert profile["story_structure"] == "three_act"
    with pytest.raises(KeyError):
        resolve_profile("does-not-exist")


def test_clamp_length():
    cozy = resolve_profile("cozy-hangout")
    assert clamp_length(cozy, None) == (12, None)
    assert clamp_length(cozy, 14) == (14, None)
    value, note = clamp_length(cozy, 60)
    assert value == 15
    assert "10-15 seconds" in note and "60" in note
    assert clamp_length(cozy, 3)[0] == 10
    assert clamp_length(resolve_profile(None), 60) == (60, None)
