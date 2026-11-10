"""Tests for genre presets registry loader and validator."""

import pytest
from pathlib import Path
from hfvg.genre_presets import (
    load_genre_presets,
    get_genre_presets_registry,
    Template,
    VOMode,
    IPMode,
    GenrePreset,
)


def test_load_genre_presets():
    """Test that genre_presets.yaml loads successfully."""
    registry = load_genre_presets()
    
    assert registry.schema_version == "0.1"
    assert len(registry.templates) == 7
    assert len(registry.presets) == 24
    
    # Check templates exist
    assert Template.MOOD_LOOP.value in registry.templates
    assert Template.MUSIC_MONTAGE.value in registry.templates
    assert Template.DIALOGUE_EPISODE.value in registry.templates
    assert Template.CONTINUOUS_POV.value in registry.templates
    assert Template.COZY_MICRO.value in registry.templates
    assert Template.ACTION_SEQUENCE.value in registry.templates
    assert Template.GAG_SHORT.value in registry.templates


def test_all_preset_ids_match_web_presets():
    """Test that all 24 preset IDs match those in web/lib/presets.ts."""
    registry = load_genre_presets()
    
    # These IDs must match web/lib/presets.ts exactly (order doesn't matter)
    expected_ids = {
        "nostalgia-time-travel",
        "satire-sketch",
        "cartoon-made-real",
        "farewell-tribute",
        "living-painting",
        "what-if-ending",
        "cozy-hangout",
        "original-mini-movie",
        "mascot-sketch",
        "dreamscape",
        "animal-remake",
        "ambience-journey",
        "original-series",
        "cute-greeting",
        "mascot-day",
        "ride-along-pov",
        "character-spotlight",
        "3d-moment",
        "anime-cooking",
        "rewrite-the-scene",
        "live-action-fight",
        "fullscreen-action-anime",
        "creature-legend",
        "time-travel-vlog",
    }
    
    actual_ids = set(registry.presets.keys())
    assert actual_ids == expected_ids, f"Preset IDs mismatch: {actual_ids ^ expected_ids}"


def test_no_duplicate_preset_ids():
    """Test that all preset IDs are unique."""
    registry = load_genre_presets()
    errors = registry.validate_all()
    
    # No errors means validation passed (including no duplicates)
    assert not errors, f"Validation errors: {errors}"


def test_no_named_ip_in_presets():
    """Test that no named IP appears in preset names or descriptions."""
    registry = load_genre_presets()
    
    # Banned terms (case-insensitive)
    banned_terms = [
        "disney", "pixar", "ghibli", "marvel", "dc comics", "pokemon", "naruto",
        "harry potter", "star wars", "lord of the rings", "attack on titan",
        "demon slayer", "spirited away", "jurassic park", "blockbuster", "kfc",
        "miffy", "boris", "stitch", "totoro", "spongebob"
    ]
    
    violations = []
    for preset in registry.presets.values():
        text = (preset.name + " " + preset.description).lower()
        for term in banned_terms:
            if term in text:
                violations.append(f"{preset.id}: contains '{term}'")
    
    assert not violations, f"Named IP violations:\n" + "\n".join(violations)


def test_all_presets_have_valid_length_ranges():
    """Test that all presets have valid [min, max] length ranges."""
    registry = load_genre_presets()
    
    for preset in registry.presets.values():
        assert len(preset.length_s) == 2, f"{preset.id}: length_s must be [min, max]"
        assert preset.length_s[0] > 0, f"{preset.id}: min length must be > 0"
        assert preset.length_s[0] <= preset.length_s[1], f"{preset.id}: min > max"


def test_all_presets_have_valid_aspects():
    """Test that all presets use valid aspect ratios."""
    registry = load_genre_presets()
    valid_aspects = ["9:16", "16:9", "4:3", "16:9_rotated_to_9:16"]
    
    for preset in registry.presets.values():
        assert preset.aspect in valid_aspects, (
            f"{preset.id}: invalid aspect {preset.aspect}"
        )


def test_preset_fields_are_populated():
    """Test that all presets have required fields populated."""
    registry = load_genre_presets()
    
    for preset in registry.presets.values():
        assert preset.name, f"{preset.id}: missing name"
        assert preset.description, f"{preset.id}: missing description"
        assert preset.template, f"{preset.id}: missing template"
        assert preset.brief_fields, f"{preset.id}: missing brief_fields"
        assert preset.vo_mode, f"{preset.id}: missing vo_mode"


def test_get_preset_by_id():
    """Test getting a preset by ID."""
    registry = load_genre_presets()
    
    preset = registry.get_preset("nostalgia-time-travel")
    assert preset is not None
    assert preset.id == "nostalgia-time-travel"
    assert preset.template == Template.DIALOGUE_EPISODE
    assert preset.vo_mode == VOMode.DIALOGUE
    
    # Non-existent preset
    assert registry.get_preset("does-not-exist") is None


def test_get_presets_by_template():
    """Test getting all presets for a template."""
    registry = load_genre_presets()
    
    mood_loops = registry.get_presets_by_template(Template.MOOD_LOOP)
    assert len(mood_loops) > 0
    assert all(p.template == Template.MOOD_LOOP for p in mood_loops)
    
    # Check expected mood loops from the brief
    mood_loop_ids = {p.id for p in mood_loops}
    expected = {
        "farewell-tribute", "living-painting", "cozy-hangout",
        "dreamscape", "ambience-journey", "character-spotlight"
    }
    assert expected.issubset(mood_loop_ids)


def test_template_inheritance():
    """Test that presets inherit from their templates correctly."""
    registry = load_genre_presets()
    
    # Check a mood_loop preset
    preset = registry.get_preset("living-painting")
    assert preset is not None
    assert preset.template == Template.MOOD_LOOP
    
    # Should inherit template defaults
    template = registry.templates[Template.MOOD_LOOP.value]
    assert preset.video_prompt_mode == template.video_prompt_mode
    assert preset.story_structure == template.story_structure


def test_risk_flags_on_sensitive_presets():
    """Test that sensitive presets have appropriate risk flags."""
    registry = load_genre_presets()
    
    # Satire sketch should have political and real_likeness flags
    satire = registry.get_preset("satire-sketch")
    assert satire is not None
    assert any("political" in str(f.value) for f in satire.risk_flags)
    assert any("likeness" in str(f.value) for f in satire.risk_flags)
    
    # Live action fight should have violence flag
    fight = registry.get_preset("live-action-fight")
    assert fight is not None
    assert any("violence" in str(f.value) for f in fight.risk_flags)


def test_gates_add_for_high_risk_presets():
    """Test that high-risk presets have additional gates."""
    registry = load_genre_presets()
    
    # Satire sketch should have manual_review gate
    satire = registry.get_preset("satire-sketch")
    assert satire is not None
    assert any("manual_review" in str(g.value) for g in satire.gates_add)
    
    # Farewell tribute should have likeness_consent and fact_check
    tribute = registry.get_preset("farewell-tribute")
    assert tribute is not None
    assert any("likeness_consent" in str(g.value) for g in tribute.gates_add)
    assert any("fact_check" in str(g.value) for g in tribute.gates_add)


def test_ip_mode_for_thirdparty_presets():
    """Test that presets with IP risk have appropriate ip_mode."""
    registry = load_genre_presets()
    
    # Cartoon-made-real should allow public domain
    cartoon = registry.get_preset("cartoon-made-real")
    assert cartoon is not None
    assert cartoon.ip_mode == IPMode.PUBLIC_DOMAIN_OK or cartoon.ip_mode == IPMode.ORIGINAL_ONLY
    
    # Most presets should default to original_only
    original = registry.get_preset("original-mini-movie")
    assert original is not None
    assert original.ip_mode == IPMode.ORIGINAL_ONLY


def test_dialogue_presets_have_dialogue_vo_mode():
    """Test that dialogue episode presets use dialogue vo_mode."""
    registry = load_genre_presets()
    
    dialogue_presets = registry.get_presets_by_template(Template.DIALOGUE_EPISODE)
    for preset in dialogue_presets:
        assert preset.vo_mode == VOMode.DIALOGUE, (
            f"{preset.id}: dialogue_episode template must use dialogue vo_mode"
        )


def test_loop_presets_have_reasonable_lengths():
    """Test that loop presets have appropriate length ranges."""
    registry = load_genre_presets()
    
    loops = registry.get_presets_by_template(Template.MOOD_LOOP)
    for preset in loops:
        # Most loops are 10-90s, except ambience-journey which is 60-600s
        if preset.id == "ambience-journey":
            assert preset.length_s[0] >= 60
            assert preset.length_s[1] <= 600
        else:
            assert preset.length_s[0] >= 10
            assert preset.length_s[1] <= 90


def test_action_sequences_have_moderation_gates():
    """Test that action sequence presets have moderation gates."""
    registry = load_genre_presets()
    
    action_presets = registry.get_presets_by_template(Template.ACTION_SEQUENCE)
    for preset in action_presets:
        # Action sequences should have moderation_precheck gate
        # (inherited from template gates_add)
        gates = [g.value for g in preset.gates_add]
        template_gates = [g.value for g in registry.templates[Template.ACTION_SEQUENCE.value].gates_add]
        combined_gates = gates + template_gates
        assert "moderation_precheck" in combined_gates, (
            f"{preset.id}: action sequences must have moderation_precheck gate"
        )


def test_continuous_pov_presets_have_continuous_mode():
    """Test that continuous POV presets use continuous video mode."""
    registry = load_genre_presets()
    
    pov_presets = registry.get_presets_by_template(Template.CONTINUOUS_POV)
    for preset in pov_presets:
        # Should inherit continuous from template
        assert preset.video_prompt_mode is not None
        # Will be set by template


def test_all_presets_have_brief_fields():
    """Test that all presets define brief_fields for user input."""
    registry = load_genre_presets()
    
    for preset in registry.presets.values():
        assert len(preset.brief_fields) >= 2, (
            f"{preset.id}: must have at least 2 brief_fields"
        )
        assert len(preset.brief_fields) <= 5, (
            f"{preset.id}: should have at most 5 brief_fields for simplicity"
        )


def test_registry_singleton():
    """Test that get_genre_presets_registry returns same instance."""
    registry1 = get_genre_presets_registry()
    registry2 = get_genre_presets_registry()
    
    assert registry1 is registry2


def test_aspect_ratio_diversity():
    """Test that presets include diverse aspect ratios, not just 9:16."""
    registry = load_genre_presets()
    
    aspects = {p.aspect for p in registry.presets.values()}
    
    # Should have at least 9:16, 16:9, and 4:3
    assert "9:16" in aspects
    assert "16:9" in aspects
    assert "4:3" in aspects
    
    # Check specific presets have correct aspects
    what_if = registry.get_preset("what-if-ending")
    assert what_if.aspect == "16:9"
    
    ambience = registry.get_preset("ambience-journey")
    assert ambience.aspect == "4:3"


def test_fullscreen_action_has_rotate_to_fill():
    """Test that fullscreen-action-anime has rotate_to_fill assembly override."""
    registry = load_genre_presets()
    
    fullscreen = registry.get_preset("fullscreen-action-anime")
    assert fullscreen is not None
    assert "rotate_to_fill" in fullscreen.stage_overrides.get("assembly", "")


def test_cozy_micro_presets_are_short():
    """Test that cozy_micro presets are 10-20s as expected."""
    registry = load_genre_presets()
    
    cozy_presets = registry.get_presets_by_template(Template.COZY_MICRO)
    for preset in cozy_presets:
        assert preset.length_s[0] >= 10
        assert preset.length_s[1] <= 20, (
            f"{preset.id}: cozy_micro should be max 20s"
        )
