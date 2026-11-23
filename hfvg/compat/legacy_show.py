"""Legacy adapter: the first series (a duck host, animal guests, one resort) as a show profile.

An episode without ``show.json`` that has a ``prompt_kit.json`` or a ``shots.json`` gets
:func:`adapter_profile`: the rules below plus the lint compiled from its kit's ``"lint"`` section
(:func:`compile_kit`, every message exactly as the code wrote it before show profiles), so its lint
and playbook messages, shot_meta keys, headcount figures, sheet prompts and judge prompts do not change.
``examples/first-series/show.json`` is this profile for the series fixture kit, written out as data
(tests/test_show_profile_legacy_adapter.py keeps the two equal).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from hfvg.show_profile import (
    CharacterCode,
    ContinuityLock,
    FlagFigure,
    ForbidNear,
    LintRules,
    MotionSuggestion,
    Playbook,
    PlaybookRule,
    RequiredClause,
    ShotFlag,
    ShowProfile,
    evaluate_when,
    validate_when,
)

NAME = "First series (legacy adapter)"
SHEET_LEAD = "Character design reference of an original character."

# shots.json keys and the shot_meta flags they set (written at the top level of shot_meta, as before)
HOST_FLAG, HOST_DECLARED, HOST_KEY = "duck", "duck_declared", "duck_present"
ANTLER_FLAG, ANTLER_KEY = "antler", "antler_character"
SHOT_FLAGS = (
    ShotFlag(key=ANTLER_KEY, meta=ANTLER_FLAG, type="bool", default=False, top_level=True),
    ShotFlag(key=HOST_KEY, meta=HOST_FLAG, type="bool", declared_meta=HOST_DECLARED, default=False, top_level=True),
)
# The host is counted by the headcount as one more figure when shot_meta.duck is set
HOST_FIGURE = FlagFigure(flag=HOST_FLAG, figure_id="_duck_host", name="duck",
                         description="the series' duck host, not one of the shot's main characters")
# BEATMAP code D: the host; every parsed shot carries duck_role ("host" or "absent")
CHARACTER_CODES = {"D": CharacterCode(slug="duck", role="host", key="duck_role", absent="absent")}
ROLE_PREFIX = "{role} duck"
SCALE_REFERENCE = "duck"


def prompt_lead(shot: dict[str, Any]) -> str:
    """The lead hfvg.episode_parser.build_prompt_from_shot gives a shot parsed with the legacy codes
    (its ``duck_role``): "host duck" / "absent duck"; "" without the key."""
    return " ".join(ROLE_PREFIX.format(role=shot[c.key], slug=c.slug)
                    for c in CHARACTER_CODES.values() if c.key and shot.get(c.key))

# ------------------------------------------------------------------------------------------ lint

LOCK_WHEN = ("duck", "animals", "always")
LOCK_KINDS = ("still", "clip")
FORBIDDEN_PHRASES = ("nothing else moves", "realistic wild")
DEFAULT_PROFILE_FORBIDDEN_PHRASES = ("nothing else moves",)
CARRY_VERBS = ("carry", "carries", "carrying", "carried", "hold", "holds", "holding", "held")
MOOSE_NAMES = ("moose",)
HOST_NAMES = ("duck",)
REALISM_WORDS = ("realistic", "photoreal", "photorealistic", "photo-real", "photo-realistic", "lifelike",
                 "life-like", "hyperreal", "hyper-real", "hyperrealistic")
BATH_WHEN = {"flag": "bath", "default": {"all": [{"flag": "wet"}, {"flag": "water"}, {"count_gt": 0}]}}
MESSAGES = {
    "antler_clause": ("missing antler clause: {text!r}",
                      "lint kit has no antler_clause but an antler character is present"),
    "duck_clause": ("missing duck clause: {text!r}", "lint kit has no duck_clause but the duck is present"),
    "antler_flag": ("missing antler clause (shot_meta.antler): {text!r}",
                    "lint kit has no antler_clause but shot_meta.antler is set"),
    "duck_flag": ("missing duck clause (shot_meta.duck): {text!r}",
                  "lint kit has no duck_clause but shot_meta.duck is set"),
    "bath_clause": ("missing bath clause on bath shot: {text!r}", ""),
    "carry_hold": "carry/hold verb used with a moose: {sentence!r}",
    "bath_forbidden": "bath shot prompt puts an animal on the water: {word!r}",
    "realism_split": ("realism word {word!r} attached to the duck (only the duck is plush-stylised): "
                      "{sentence!r}"),
    "banned_decor": "banned decor word: {word!r}",
}


def is_bath_shot(shot_meta: dict[str, Any] | None) -> bool:
    """Animals soaking in the water (the kits' bath rule): explicit shot_meta.bath, else wet + water +
    count > 0."""
    return evaluate_when(BATH_WHEN, shot_meta)


def _lint_error(message: str):
    from hfvg.prompt_lint import PromptLintError
    return PromptLintError(message)


def lock_when(when: Any, host_codes: tuple[str, ...] = (), animal_codes: tuple[str, ...] = ()) -> Any:
    """The predicate of a lock written with the older ``when`` strings (None for any other value)."""
    if when == "always":
        return {"always": True}
    if when == "duck":
        return _host_present(host_codes)
    if when == "animals":
        return {"any": [{"count_gt": 0}, {"characters_any": list(animal_codes)}]} if animal_codes else {"count_gt": 0}
    return None


def _host_present(host_codes: tuple[str, ...]) -> dict:
    flag = {"flag": HOST_FLAG}
    return {"any": [flag, {"characters_any": list(host_codes)}]} if host_codes else flag


def parse_locks(raw: Any) -> list[tuple[str, Any, tuple[str, ...], str]]:
    """A kit's continuity_locks list checked the way kits always were: [(id, when, kinds, text)].
    ``when`` may also be a predicate (docs/SHOW-PROFILE.md)."""
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise _lint_error("Lint kit field 'continuity_locks' must be a list")
    out, seen = [], set()
    for i, lock in enumerate(raw):
        if not isinstance(lock, dict):
            raise _lint_error(f"continuity_locks[{i}] must be an object")
        lid = str(lock.get("id") or "").strip()
        text = " ".join(str(lock.get("text") or "").split())
        when = lock.get("when")
        kinds = lock.get("kinds", list(LOCK_KINDS))
        if not lid or lid in seen:
            raise _lint_error(f"continuity_locks[{i}] needs a unique non-empty id")
        if not text:
            raise _lint_error(f"continuity lock {lid!r} has no text")
        if isinstance(when, dict):
            problems: list[str] = []
            validate_when(when, "when", problems)
            if problems:
                raise _lint_error(f"continuity lock {lid!r}: " + "; ".join(problems))
        elif when not in LOCK_WHEN:
            raise _lint_error(f"continuity lock {lid!r}: when must be one of {LOCK_WHEN}")
        if not isinstance(kinds, list) or not kinds or any(k not in LOCK_KINDS for k in kinds):
            raise _lint_error(f"continuity lock {lid!r}: kinds must be a non-empty subset of {LOCK_KINDS}")
        seen.add(lid)
        out.append((lid, when, tuple(kinds), text))
    return out


def build_locks(parsed: list, host_codes: tuple[str, ...] = (), animal_codes: tuple[str, ...] = ()) -> tuple:
    return tuple(ContinuityLock(id=lid, text=text, kinds=kinds,
                                when=when if isinstance(when, dict) else lock_when(when, host_codes, animal_codes))
                 for lid, when, kinds, text in parsed)


def _realism_rule(names: tuple[str, ...], words: tuple[str, ...], host_codes: tuple[str, ...]) -> ForbidNear:
    return ForbidNear(id="realism_split", words=tuple(w.lower() for w in words),
                      subjects=tuple(n.lower() for n in names), scope="clause", match="letters", report="unit",
                      when=_host_present(host_codes), kinds=("still", "clip", "ref_edit"), after="continuity_locks",
                      message=MESSAGES["realism_split"], max_quote=120)


def _flag_clause(rid: str, flag: str, text: str, codes: tuple[str, ...]) -> RequiredClause:
    when = {"all": [{"flag": flag}, {"not": {"characters_any": list(codes)}}]} if codes else {"flag": flag}
    message, missing = MESSAGES[rid]
    return RequiredClause(id=rid, text=text, when=when, message=message, missing_text_message=missing)


def compile_kit(data: dict[str, Any]) -> LintRules:
    """A kit's ``"lint"`` object as profile rules, validated (and refused) exactly as before."""
    def tup(key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
        value = data.get(key, default)
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise _lint_error(f"Lint kit field {key!r} must be a list of strings")
        return tuple(str(v) for v in value)

    for required in ("count_template", "realtime_pattern", "locked_camera_clause"):
        if not isinstance(data.get(required), str) or not data[required].strip():
            raise _lint_error(f"Lint kit is missing required field {required!r}")
    if "{n}" not in data["count_template"]:
        raise _lint_error("Lint kit count_template must contain '{n}'")
    try:
        re.compile(data["realtime_pattern"])
    except re.error as e:
        raise _lint_error(f"Lint kit realtime_pattern is not a valid regex: {e}") from e
    antler_clause = str(data.get("antler_clause", ""))
    antler_codes = tup("antler_characters")
    host_clause = str(data.get("duck_clause", ""))
    host_codes = tup("duck_characters")
    banned = tup("banned_decor")
    forbidden = tup("forbidden_phrases", FORBIDDEN_PHRASES)
    moose = tup("moose_names", MOOSE_NAMES)
    carry = tup("carry_verbs", CARRY_VERBS)
    bath_clause = str(data.get("bath_clause", ""))
    bath_forbidden = tup("bath_forbidden")
    locks = parse_locks(data.get("continuity_locks", []))
    animal_codes = tup("animal_characters")
    names = tup("duck_names")
    words = tup("duck_realism_words")

    clauses = []
    if antler_codes:
        message, missing = MESSAGES["antler_clause"]
        clauses.append(RequiredClause(id="antler_clause", text=antler_clause,
                                      when={"characters_any": list(antler_codes)}, message=message,
                                      missing_text_message=missing))
    if host_codes:
        message, missing = MESSAGES["duck_clause"]
        clauses.append(RequiredClause(id="duck_clause", text=host_clause, when={"characters_any": list(host_codes)},
                                      message=message, missing_text_message=missing))
    clauses.append(_flag_clause("antler_flag", ANTLER_FLAG, antler_clause, antler_codes))
    clauses.append(_flag_clause("duck_flag", HOST_FLAG, host_clause, host_codes))
    if bath_clause.strip():
        clauses.append(RequiredClause(id="bath_clause", text=bath_clause, when=BATH_WHEN,
                                      message=MESSAGES["bath_clause"][0]))
    near = []
    if moose:
        near.append(ForbidNear(id="carry_hold", words=carry, subjects=moose, scope="sentence", match="word",
                               report="unit", message=MESSAGES["carry_hold"]))
    if bath_forbidden:
        near.append(ForbidNear(id="bath_forbidden", words=bath_forbidden, scope="prompt", match="substring",
                               report="word", when=BATH_WHEN, after="required_clauses",
                               message=MESSAGES["bath_forbidden"]))
    near.append(_realism_rule(names or HOST_NAMES, words or REALISM_WORDS, host_codes))
    return LintRules(
        count_template=data["count_template"], count_required=True, realtime_pattern=data["realtime_pattern"],
        locked_camera_clause=data["locked_camera_clause"], forbidden_phrases=forbidden, banned_words=banned,
        banned_words_message=MESSAGES["banned_decor"], content_policy_terms=False, forbid_bare_waves=True,
        required_clauses=tuple(clauses), forbid_near=tuple(near),
        continuity_locks=build_locks(locks, host_codes, animal_codes))


def default_lint_rules(continuity_locks: Any = None) -> LintRules:
    """The built-in default lint profile kits without a ``"lint"`` object always got: content-policy
    terms, "nothing else moves" and the motion rules, the kit's continuity locks, the realism split
    and the fail-closed antler / duck shot_meta flags; no series clause."""
    return LintRules(
        count_template="", realtime_pattern="", locked_camera_clause="",
        forbidden_phrases=DEFAULT_PROFILE_FORBIDDEN_PHRASES, banned_words=(),
        banned_words_message=MESSAGES["banned_decor"], content_policy_terms=True, forbid_bare_waves=False,
        required_clauses=(_flag_clause("antler_flag", ANTLER_FLAG, "", ()),
                          _flag_clause("duck_flag", HOST_FLAG, "", ())),
        forbid_near=(_realism_rule(HOST_NAMES, REALISM_WORDS, ()),),
        continuity_locks=build_locks(parse_locks(continuity_locks)))


# ------------------------------------------------------------------------------------------ playbook

RULE_IDS = ("duck_role", "animal_poses", "same_resort")
PLAYBOOK = Playbook(rules=(
    PlaybookRule(type="role_exclusion", id="duck_role",
                 params={"role": "host", "absent_flag": HOST_FLAG, "absent_tags": ["guest-only"],
                         "require_text": "no duck", "forbid_tags": ["duck-in-pack"]},
                 messages={"message": "Shot {shot_id}: Guest-only shot must include 'no duck' hard negative. "
                                      "Duck-as-host rule (§3.1).",
                           "forbid_message": "Shot {shot_id}: Duck cannot be in guest pack. Duck is host, not "
                                             "fifth guest (§5)."}),
    PlaybookRule(type="pose_words", id="animal_poses",
                 params={"forbid_near": "guest",
                         "words": ["standing upright", "walking upright", "bipedal", "carrying", "holding",
                                   "hand over", "handing"],
                         "require_any": ["on all fours", "quadrupedal"],
                         "require_when": {"any": [{"tag": "animal"}, {"tag": "guest"}]}},
                 messages={"message": "Shot {shot_id}: Prompt contains '{word}' for guest animal. NO "
                                      "UPRIGHT/CARRYING rule (§7).",
                           "require_message": "Shot {shot_id}: Animal shot must specify 'on all fours' or "
                                              "'quadrupedal' gait (§7 process)."}),
    PlaybookRule(type="world_refs", id="same_resort",
                 params={"env": "HFVG_PLAYBOOK_WORLD_REFS", "types": ["room_plate"], "tags": ["env"],
                         "exempt_tags": ["pre-resort"]},
                 messages={"message": "Shot {shot_id}: Room generation must include world refs from locked "
                                      "resort frames. ONE-RESORT RULE (§6.1)."}),
))

MOTION_SUGGESTIONS = (
    MotionSuggestion(subject="moose", suggestions=("breath steam", "ear flicks", "tail swish", "head turn"),
                     tag_match="contains"),
    MotionSuggestion(subject="duck", suggestions=("tuque pom bobs", "bill tilt", "head turn"), tag_match="equals"),
)


def _host_identity():
    from hfvg.qc.identity import IdentityProfile
    return IdentityProfile(
        name="duck_host",
        label="Duck",
        signs={
            "bill": "bill shape and length",
            "head": "head shape",
            "neck_ring": "a white neck ring (0 absent, 2 clearly present)",
            "body": "body shape",
            "too_small": "is the duck big enough to read (0 readable, 1 borderline, 2 unreadable)",
        },
        apparel={"tuque": "cream tuque with a pom", "vest": "vest fading from cream to olive"},
        hint_signs=("neck_ring",),
        state_key="duck_state",
        state_features={"B": ("tuft", "3-lobe tuft at a quarter of the head height")},
        min_height_fraction=0.12,
        presence=("duck_role", "host"),
        presence_note="host role",
    )


def host_identity_profile():
    """The duck host's identity rules (hfvg.compat.host_identity)."""
    return _host_identity()


def series_profile(lint: LintRules | None = None, *, path: str | None = None) -> ShowProfile:
    """The first series' profile with ``lint`` (default: the built-in default lint profile)."""
    return ShowProfile(
        name=NAME, sheet_lead=SHEET_LEAD, identity_profiles={"duck_host": _host_identity()},
        flag_figures=(HOST_FIGURE,), shot_flags=SHOT_FLAGS, character_codes=dict(CHARACTER_CODES),
        role_prefix=ROLE_PREFIX, lint=lint if lint is not None else default_lint_rules(),
        playbook=PLAYBOOK, motion_suggestions=MOTION_SUGGESTIONS, scale_reference=SCALE_REFERENCE,
        source="legacy-adapter", path=path)


# ------------------------------------------------------------------------------------------ adapter

EPISODE_FILES = ("prompt_kit.json", "shots.json")
OVERRIDE_ENVS = ("SHOTS_JSON_PATH", "PROMPT_LINT_KIT_PATH")


def has_legacy_inputs(folder: Path | None) -> bool:
    """The episode has a prompt_kit.json or a shots.json (in its folder, or named by the env
    overrides the pipeline reads them from): it is read the way it always was."""
    if any(os.getenv(name, "").strip() for name in OVERRIDE_ENVS):
        return True
    return folder is not None and any((folder / name).is_file() for name in EPISODE_FILES)


def kit_lint_rules(kit: Any) -> LintRules:
    """The lint of a prompt_kit.json (as hfvg.prompt_lint resolves it for an episode): its ``"lint"``
    object with the kit's top-level continuity locks, else the default lint profile."""
    if not isinstance(kit, dict):
        return default_lint_rules()
    section = kit.get("lint")
    if isinstance(section, dict):
        if "continuity_locks" not in section and "continuity_locks" in kit:
            section = {**section, "continuity_locks": kit["continuity_locks"]}
        return compile_kit(section)
    return default_lint_rules(kit.get("continuity_locks"))


def adapter_profile(folder: Path | None) -> ShowProfile:
    """The legacy adapter's profile of an episode folder (None: no episode, the env overrides only).
    A kit that does not load leaves the default lint here; the lint gate itself refuses it
    (hfvg.prompt_lint.lint_kit_problems)."""
    if folder is None:
        return series_profile()
    kit_path = folder / "prompt_kit.json"
    try:
        kit = json.loads(kit_path.read_text(encoding="utf-8")) if kit_path.is_file() else None
        lint = kit_lint_rules(kit)
    except (OSError, ValueError):
        lint = default_lint_rules()
    return series_profile(lint, path=str(folder))
