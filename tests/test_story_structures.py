"""Story structures per card (hfvg/story_structures.py) and how story shaping uses them.

Every card's story_structure names a real structure (no silent fallback to three acts); the dry-run
shape is a valid StoryShape that passes the critique at the card's min / default / max length; live
prompts list the card's sections instead of a fixed 3-act sheet; model replies keep those sections.
"""

import hashlib

import pytest
import yaml

from hfvg.activities.story_shaping import (
    VO_MODE_TEXT,
    _acts_and_beats,
    _build_story_prompt,
    _compute_input_hash,
    _critique_story_shape,
    _keep_act_names,
    generate_story_shape_activity,
)
from hfvg.config import config
from hfvg.genre_presets import REGISTRY_PATH, resolve_profile
from hfvg.models import Act, StoryBeat, StoryShape
from hfvg.story_structures import (
    STRUCTURES,
    beat_counts,
    build_layout,
    hook_and_keyword,
    prompt_guidance,
    structure_for,
)

YAML_IDS = [p["id"] for p in yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))["presets"]]
CARDS = [*YAML_IDS, None]  # None: Skip (freeform)
IDEA = "A paper lantern drifts across a quiet harbor at dusk"
THREE_ACTS = ["Setup", "Confrontation", "Resolution"]
MAX_BEAT = config.STORY_SHAPING_MAX_BEAT_DURATION_S

# structure -> (act names in order, first beat prefix, last beat prefix)
EXPECTED_SHAPES = {
    "three_act": (THREE_ACTS, "Open on the world of the idea", "Payoff:"),
    "loop": (["Loop"], "Opening frame:", "Settle back into the opening frame"),
    "montage": (["Montage", "Closing image"], "Opening image:", "Closing title image:"),
    "continuous_journey": (["Continuous take"], "Start moving:", "Arrive at the destination"),
    "escalation_punchline": (["Setup", "Escalation", "Punchline"], "Setup:", "Punchline card:"),
    "episode_cliffhanger": (["Cold open", "Episode", "Cliffhanger"], "Cold open hook:", "Cliffhanger:"),
    "micro_arc": (["Micro arc"], "A small wish:", "Cozy reveal:"),
    "escalation": (["Standoff", "Escalation", "Finisher"], "Standoff:", "Finisher:"),
}


def _lengths(card):
    profile = resolve_profile(card)
    lo, hi = profile["length_s"]
    return sorted({lo, profile["default_length_s"], hi})


CARD_LENGTHS = [(card, length) for card in CARDS for length in _lengths(card)]


def _shape(layout: dict) -> StoryShape:
    return StoryShape(
        logline=layout["logline"], hook=layout["hook"], hook_deadline_s=5.0,
        acts=[Act(**a) for a in layout["acts"]], beats=[StoryBeat(**b) for b in layout["beats"]],
        midpoint_beat_id=layout["midpoint_beat_id"], payoff=layout["payoff"], payoff_references_hook=True,
        total_duration_s=layout["total_duration_s"],
    )


def _with_shots(story: StoryShape) -> StoryShape:
    """The shape after shot assignment (assign_shots_to_beats_activity runs after approval)."""
    return story.model_copy(update={"beats": [b.model_copy(update={"shot_ids": ["S01"]}) for b in story.beats]})


def _assert_valid_before_and_after_shots(story: StoryShape) -> None:
    critique = _critique_story_shape(story)
    assert critique.score == 100, critique.issues
    assert critique.issues == ["Beats without shots: " + ", ".join(b.beat_id for b in story.beats)]
    assert critique.hook_present_early and critique.payoff_references_hook
    assert critique.durations_fit and critique.no_dead_beats
    assigned = _with_shots(story)
    assert assigned.validate_structure() == []
    assert _critique_story_shape(assigned).passes


def _beat(beat_id: str, seconds: float) -> dict:
    return {"beat_id": beat_id, "description": f"beat {beat_id}", "emotional_tone": "calm",
            "target_duration_s": seconds, "shot_ids": []}


def test_the_registry_lists_24_cards_and_every_structure_has_an_expected_shape():
    assert len(YAML_IDS) == 24
    assert set(EXPECTED_SHAPES) == set(STRUCTURES)


@pytest.mark.parametrize("card", YAML_IDS)
def test_every_card_names_a_known_structure_other_than_three_acts(card):
    profile = resolve_profile(card)
    assert profile["story_structure"] in STRUCTURES
    assert structure_for(profile).name == profile["story_structure"] != "three_act"


def test_skip_and_legacy_hints_get_three_acts():
    assert structure_for(resolve_profile(None)).name == "three_act"
    assert structure_for("comedy").name == "three_act"
    assert structure_for(None).name == "three_act"
    assert structure_for({"story_structure": "no-such-structure"}).name == "three_act"


@pytest.mark.parametrize("card,length", CARD_LENGTHS)
async def test_dry_run_shape_follows_the_card_and_passes_the_critique(card, length):
    profile = resolve_profile(card)
    result = await generate_story_shape_activity("ep01", IDEA, profile, float(length), True)
    story = StoryShape(**result["story_shape"])
    acts, first_beat, last_beat = EXPECTED_SHAPES[profile["story_structure"]]

    assert [a.description for a in story.acts] == acts
    if card is not None:
        assert [a.description for a in story.acts] != THREE_ACTS
    assert story.beats[0].description.startswith(first_beat)
    assert story.beats[-1].description.startswith(last_beat)
    _assert_valid_before_and_after_shots(story)
    assert story.total_duration_s == length
    assert sum(b.target_duration_s for b in story.beats) == pytest.approx(length, abs=1e-6)
    by_id = {b.beat_id: b for b in story.beats}
    assert sorted(by_id) == sorted(i for a in story.acts for i in a.beat_ids)
    for act in story.acts:
        assert sum(by_id[i].target_duration_s for i in act.beat_ids) == pytest.approx(act.target_duration_s, abs=1e-6)


@pytest.mark.parametrize("name", sorted(STRUCTURES))
@pytest.mark.parametrize("total", [10, 12, 15, 21, 30, 45, 60, 61, 90, 119, 120, 150, 180])
def test_beat_plan_respects_the_beat_limit_and_sums_exactly(name, total):
    layout = build_layout({"story_structure": name}, IDEA, float(total), MAX_BEAT)
    beats = layout["beats"]
    assert len(beats) >= 3
    assert all(0 < b["target_duration_s"] <= MAX_BEAT for b in beats)
    assert sum(b["target_duration_s"] for b in beats) == pytest.approx(total, abs=1e-6)
    assert sum(a["target_duration_s"] for a in layout["acts"]) == pytest.approx(total, abs=1e-6)
    assert len({b["beat_id"] for b in beats}) == len(beats)
    assert layout["midpoint_beat_id"] in {b["beat_id"] for b in beats}
    assert [a["act_number"] for a in layout["acts"]] == list(range(1, len(layout["acts"]) + 1)) and len(layout["acts"]) <= 5
    assert beat_counts(STRUCTURES[name], float(total), MAX_BEAT) == [len(a["beat_ids"]) for a in layout["acts"]]
    _assert_valid_before_and_after_shots(_shape(layout))


@pytest.mark.parametrize("idea", ["Up we go to it", "x" * 3000, "Llamas!!! on, a beach?", IDEA])
@pytest.mark.parametrize("name", sorted(STRUCTURES))
def test_payoff_shares_a_word_with_the_hook_for_any_idea(idea, name):
    hook, keyword = hook_and_keyword(idea)
    assert len(hook) >= 10 and keyword in hook
    story = _shape(build_layout({"story_structure": name}, idea, 30.0, MAX_BEAT))
    assert keyword in story.payoff
    assert _critique_story_shape(story).payoff_references_hook


@pytest.mark.parametrize("card", CARDS)
def test_live_prompt_lists_the_cards_sections(card):
    profile = resolve_profile(card)
    length = float(profile["default_length_s"])
    prompt = _build_story_prompt(IDEA, profile, length)
    structure = structure_for(profile)

    assert prompt_guidance(profile, length, MAX_BEAT) in prompt
    assert f"Story structure: {structure.title}." in prompt
    assert f"Use exactly {len(structure.sections)} act(s)" in prompt
    for a, section in enumerate(structure.sections, start=1):
        assert f'Act {a} "{section.name}"' in prompt
    assert f"({int(length)} seconds)" in prompt
    assert f"Format: {profile.get('name', 'Freeform')}" in prompt
    assert VO_MODE_TEXT[profile["vo_mode"]] in prompt
    assert "3-act" not in prompt and "~2 minutes" not in prompt
    if structure.name != "three_act":
        assert "Confrontation" not in prompt


def test_legacy_genre_hint_keeps_its_guidance_and_three_acts():
    prompt = _build_story_prompt(IDEA, "comedy", 120.0)
    assert ("Genre guidance (comedy): Focus on comedic timing, setup/punchline structure, "
            "and character reactions.") in prompt
    assert 'Act 1 "Setup"' in prompt and 'Act 2 "Confrontation"' in prompt and 'Act 3 "Resolution"' in prompt
    assert "(120 seconds)" in prompt


def test_model_reply_with_nested_acts_keeps_the_cards_sections():
    profile = next(resolve_profile(c) for c in YAML_IDS if resolve_profile(c)["story_structure"] == "montage")
    layout = build_layout(profile, IDEA, 30.0, MAX_BEAT)
    beats = {b["beat_id"]: b for b in layout["beats"]}
    reply = {"acts": [{"act_number": a["act_number"], "description": a["description"],
                       "beats": [beats[i] for i in a["beat_ids"]]} for a in layout["acts"]]}
    acts, parsed = _acts_and_beats(reply, profile)
    assert [a.description for a in acts] == ["Montage", "Closing image"]
    assert [a.beat_ids for a in acts] == [a["beat_ids"] for a in layout["acts"]]
    assert [a.target_duration_s for a in acts] == pytest.approx([a["target_duration_s"] for a in layout["acts"]])
    assert [b.beat_id for b in parsed] == [b["beat_id"] for b in layout["beats"]]


def test_flat_beats_reply_becomes_one_act_named_after_the_structure():
    loop = next(resolve_profile(c) for c in YAML_IDS if resolve_profile(c)["story_structure"] == "loop")
    acts, beats = _acts_and_beats({"beats": [_beat("b1", 4), _beat("b2", 4), _beat("b3", 4)]}, loop)
    assert [(a.act_number, a.description, a.beat_ids) for a in acts] == [(1, "Seamless loop", ["b1", "b2", "b3"])]
    assert acts[0].target_duration_s == 12
    assert len(beats) == 3


def test_more_than_five_acts_fold_into_the_fifth_and_names_fall_back():
    reply = {"acts": [{"act_number": i, **({"name": f"Part {i}"} if i == 2 else {}),
                       **({"description": f"Act named {i}"} if i == 1 else {}),
                       "beats": [_beat(f"b{i}", 3)]} for i in range(1, 8)]}
    acts, beats = _acts_and_beats(reply, None)
    assert [a.description for a in acts] == ["Act named 1", "Part 2", "Act 3", "Act 4", "Act 5"]
    assert acts[4].beat_ids == ["b5", "b6", "b7"] and acts[4].target_duration_s == 9
    assert len(beats) == 7


def test_revision_that_keeps_the_beats_keeps_the_act_names():
    acts = [Act(act_number=1, description="Standoff", target_duration_s=4, beat_ids=["b1"]),
            Act(act_number=2, description="Escalation", target_duration_s=8, beat_ids=["b2", "b3"])]
    revised = [StoryBeat(**_beat("b1", 5)), StoryBeat(**_beat("b2", 3)), StoryBeat(**_beat("b3", 4))]
    kept = _keep_act_names(acts, revised)
    assert [(a.description, a.target_duration_s) for a in kept] == [("Standoff", 5), ("Escalation", 7)]
    assert _keep_act_names(acts, [*revised[:2], StoryBeat(**_beat("new", 4))]) is None


def test_cache_key_separates_cards_and_pre_structure_entries():
    a = _compute_input_hash(IDEA, resolve_profile("cozy-hangout"), 12.0)
    b = _compute_input_hash(IDEA, resolve_profile("living-painting"), 12.0)
    assert a != b
    assert a == _compute_input_hash(IDEA, resolve_profile("cozy-hangout"), 12.0)
    before_structures = hashlib.sha256(f"{IDEA}||120.0".encode()).hexdigest()[:16]
    assert _compute_input_hash(IDEA, None, 120.0) != before_structures
