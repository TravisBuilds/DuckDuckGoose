"""Show profile (``show.json``, schema ``hfvg.show/1``): everything one show needs that the engine does
not know, as data. docs/SHOW-PROFILE.md describes every key.

A show's characters, the clauses its prompts must carry, the words its prompts may not use near a
subject, the flags its shots.json sets, the extra figures a flag declares to the headcount, its
identity rules, its character sheet framings, its playbook rules and its gate policy file live in the
profile; the engine reads them from here and holds no show's rules of its own.

Resolution for an episode, first match wins (:func:`resolve_show_profile`):

1. ``<EPISODES_DIR>/<episode>/show.json`` (source ``episode``);
2. the file named by ``HFVG_SHOW_PROFILE_PATH`` (source ``env``);
3. an episode with a ``prompt_kit.json`` or a ``shots.json`` (or the SHOTS_JSON_PATH /
   PROMPT_LINT_KIT_PATH overrides): the legacy adapter (hfvg.compat.legacy_show), which reads them
   exactly as the code did before profiles existed (source ``legacy-adapter``);
4. the neutral default profile (:func:`neutral_profile`, source ``default``).

Every value is validated strictly; all problems of a file are reported together
(:class:`ShowProfileError`). Profiles are frozen; :func:`profile_digest` is the sha256 of their
canonical JSON (logs, the show-profile API). Loading reads files, so it happens in activities and
the API only, never in workflow code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

SCHEMA = "hfvg.show/1"
PROFILE_FILE = "show.json"
PROFILE_PATH_ENV = "HFVG_SHOW_PROFILE_PATH"
SOURCES = ("episode", "env", "legacy-adapter", "default")

PRESET_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
LINT_KINDS = ("still", "clip", "ref_edit")
FLAG_TYPES = {"bool": bool, "str": str, "int": int}
NEAR_SCOPES = ("sentence", "clause", "prompt")
NEAR_MATCHES = ("word", "letters", "substring")
NEAR_REPORTS = ("unit", "word")
NEAR_AFTER = ("forbidden_phrases", "required_clauses", "continuity_locks")
PLAYBOOK_TYPES = ("role_exclusion", "pose_words", "world_refs")
ROUTING_RULE = "moderation_routing"
REAL_PEOPLE = ("refuse",)
NEUTRAL_SHEET_LEAD = "Character design reference."
NEUTRAL_FORBIDDEN_PHRASES = ("nothing else moves",)
DEFAULT_CLAUSE_MESSAGE = "missing required clause {id!r}: {text!r}"
DEFAULT_NEAR_MESSAGE = "word {word!r} near {subject!r}: {sentence!r}"
DEFAULT_BANNED_MESSAGE = "banned word: {word!r}"
ALWAYS: dict[str, Any] = {"always": True}


class ShowProfileError(ValueError):
    """A show profile that cannot be used; ``problems`` lists every problem found."""

    def __init__(self, problems: list[str], where: str = "show profile"):
        self.problems = list(problems)
        self.where = where
        super().__init__(f"{where}: " + "; ".join(self.problems))


# ------------------------------------------------------------------------------------------ predicates
#
# ``when`` values: {"always": true}, {"flag": <shot flag>} (true when set; "default": a predicate or
# boolean used when the shot does not set it), {"characters_any": [...]} (case-insensitive),
# {"count_gt": n}, {"tag": <tag>}, {"role": <role>} (a role of the shot's cast entries) and the
# combinators {"all": [...]}, {"any": [...]}, {"not": {...}}.

_LEAVES = ("always", "flag", "characters_any", "count_gt", "tag", "role")
_COMBINATORS = ("all", "any", "not")


def validate_when(pred: Any, where: str, problems: list[str]) -> None:
    """Append why ``pred`` is not a predicate (nothing when it is)."""
    if not isinstance(pred, dict) or len([k for k in pred if k != "default"]) != 1:
        problems.append(f"{where}: a predicate is an object with exactly one of "
                        f"{list(_LEAVES + _COMBINATORS)}")
        return
    (key,) = [k for k in pred if k != "default"]
    value = pred[key]
    if "default" in pred and key != "flag":
        problems.append(f"{where}: only a flag predicate takes a default")
    if key == "always":
        if value is not True:
            problems.append(f"{where}: always must be true")
    elif key == "flag":
        if not isinstance(value, str) or not NAME_RE.match(value):
            problems.append(f"{where}: flag must be a flag name")
        default = pred.get("default", False)
        if not isinstance(default, bool):
            validate_when(default, f"{where}.default", problems)
    elif key == "characters_any":
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            problems.append(f"{where}: characters_any must be a list of strings")
    elif key == "count_gt":
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{where}: count_gt must be an integer")
    elif key in ("tag", "role"):
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: {key} must be a non-empty string")
    elif key in ("all", "any"):
        if not isinstance(value, list) or not value:
            problems.append(f"{where}: {key} must be a non-empty list of predicates")
        else:
            for i, sub in enumerate(value):
                validate_when(sub, f"{where}.{key}[{i}]", problems)
    elif key == "not":
        validate_when(value, f"{where}.not", problems)
    else:
        problems.append(f"{where}: unknown predicate {key!r}; use one of {list(_LEAVES + _COMBINATORS)}")


def flag_value(shot_meta: dict | None, name: str) -> tuple[bool, Any]:
    """(set, value) of a shot flag: ``shot_meta["flags"][name]``, else a top-level ``shot_meta[name]``
    (where the legacy adapter keeps its flags)."""
    meta = shot_meta if isinstance(shot_meta, dict) else {}
    flags = meta.get("flags")
    if isinstance(flags, dict) and name in flags:
        return True, flags[name]
    if name in meta:
        return True, meta[name]
    return False, None


def _cast_roles(meta: dict) -> set[str]:
    """The roles of the shot's characters: ``shot_meta.roles`` (written from the show's cast by
    hfvg.shot_data) and any ``role`` of its cast entries."""
    roles = {str(r).lower() for r in (meta.get("roles") or []) if isinstance(r, str)}
    cast = meta.get("cast")
    for entry in cast if isinstance(cast, list) else []:
        if isinstance(entry, dict) and isinstance(entry.get("role"), str):
            roles.add(entry["role"].lower())
    return roles


def evaluate_when(pred: Any, shot_meta: dict | None, tags: list | tuple | None = None) -> bool:
    """True when the shot matches the (validated) predicate."""
    meta = shot_meta if isinstance(shot_meta, dict) else {}
    if pred is None or pred is True:
        return True
    if pred is False:
        return False
    (key,) = [k for k in pred if k != "default"]
    value = pred[key]
    if key == "always":
        return True
    if key == "flag":
        is_set, flag = flag_value(meta, value)
        if is_set:
            return bool(flag)
        return evaluate_when(pred.get("default", False), meta, tags)
    if key == "characters_any":
        present = {str(c).lower() for c in (meta.get("characters") or [])}
        return bool(present & {str(c).lower() for c in value})
    if key == "count_gt":
        count = meta.get("count")
        return isinstance(count, int) and not isinstance(count, bool) and count > value
    if key == "tag":
        return value in (tags if tags is not None else meta.get("tags") or [])
    if key == "role":
        return value.lower() in _cast_roles(meta)
    if key == "all":
        return all(evaluate_when(p, meta, tags) for p in value)
    if key == "any":
        return any(evaluate_when(p, meta, tags) for p in value)
    if key == "not":
        return not evaluate_when(value, meta, tags)
    raise ValueError(f"unknown predicate {key!r}")


# ------------------------------------------------------------------------------------------ data


@dataclass(frozen=True)
class BodyPreset:
    """How a character sheet frames one kind of body (hfvg.cast_sheets)."""
    label: str
    frame: str
    head: str
    aspects: tuple[str, ...]
    placeholder: tuple[int, int]


@dataclass(frozen=True)
class CastEntry:
    """Show data about one cast slug (all optional)."""
    role: str = ""
    tags: tuple[str, ...] = ()
    body: str = ""
    identity_profile: str = ""


@dataclass(frozen=True)
class FlagFigure:
    """A shot flag that declares one more figure to the headcount, outside ``count``."""
    flag: str
    figure_id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class ShotFlag:
    """A shots.json key copied into ``shot_meta`` (``flags.<meta>``; ``top_level``: ``shot_meta.<meta>``,
    coerced to bool and always written, the way the legacy adapter keeps its flags)."""
    key: str
    meta: str
    type: str = "bool"
    declared_meta: str = ""
    default: Any = None
    top_level: bool = False


@dataclass(frozen=True)
class CharacterCode:
    """A beatmap character code: the cast slug it stands for and the role it plays. ``key``: the shot
    key the role is written to (legacy layout; with ``absent`` when the code is not in the row)."""
    slug: str
    role: str = ""
    key: str = ""
    absent: str = ""


@dataclass(frozen=True)
class RequiredClause:
    """A clause the prompt must contain when ``when`` matches. ``missing_text_message``: reported when
    the profile has no text for a clause that applies (fail closed)."""
    id: str
    text: str
    when: Any = field(default_factory=lambda: dict(ALWAYS))
    kinds: tuple[str, ...] = ("still", "clip")
    message: str = DEFAULT_CLAUSE_MESSAGE
    missing_text_message: str = ""


@dataclass(frozen=True)
class ForbidNear:
    """Words a prompt may not use near a subject. ``scope``: sentence (split at . ! ? and new lines),
    clause (also at ;) or the whole prompt; ``match``: word (word-bounded, any case), letters (bounded
    by non-letters, lower case) or substring; ``report``: one message per unit or per word; ``after``:
    the lint step it runs after; no ``subjects``: every unit."""
    id: str
    words: tuple[str, ...]
    subjects: tuple[str, ...] = ()
    scope: str = "sentence"
    match: str = "word"
    report: str = "unit"
    when: Any = field(default_factory=lambda: dict(ALWAYS))
    kinds: tuple[str, ...] = ("still", "clip")
    after: str = "forbidden_phrases"
    message: str = DEFAULT_NEAR_MESSAGE
    max_quote: int | None = None


@dataclass(frozen=True)
class ContinuityLock:
    """Text the submit appends to every matching prompt, then required by the lint (GAP-40).
    Iterates as (key, value) pairs, so ``dict(lock)`` reads it like the lock dicts it replaces."""
    id: str
    text: str
    when: Any = field(default_factory=lambda: dict(ALWAYS))
    kinds: tuple[str, ...] = ("still", "clip")

    def __iter__(self):
        return iter((("id", self.id), ("when", self.when), ("kinds", self.kinds), ("text", self.text)))


@dataclass(frozen=True)
class LintRules:
    """The show's prompt lint rules (hfvg.prompt_lint.LintKit is a view over them)."""
    count_template: str = ""
    count_required: bool = True
    realtime_pattern: str = ""
    locked_camera_clause: str = ""
    forbidden_phrases: tuple[str, ...] = NEUTRAL_FORBIDDEN_PHRASES
    banned_words: tuple[str, ...] = ()
    banned_words_message: str = DEFAULT_BANNED_MESSAGE
    content_policy_terms: bool = True
    forbid_bare_waves: bool = False
    required_clauses: tuple[RequiredClause, ...] = ()
    forbid_near: tuple[ForbidNear, ...] = ()
    continuity_locks: tuple[ContinuityLock, ...] = ()


@dataclass(frozen=True)
class PlaybookRule:
    """One playbook rule (hfvg.playbook): ``type`` role_exclusion | pose_words | world_refs, its own
    ``params`` and the message templates in ``messages``."""
    type: str
    id: str
    params: dict = field(default_factory=dict)
    messages: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Playbook:
    rules: tuple[PlaybookRule, ...] = ()
    hard: tuple[str, ...] = ()  # rule ids that refuse a live submit (HFVG_PLAYBOOK_HARD_RULES adds to them)

    def rule_ids(self) -> tuple[str, ...]:
        return tuple(r.id for r in self.rules)


@dataclass(frozen=True)
class MotionSuggestion:
    """Ambient motion suggested for shots naming ``subject`` in their room or tags (hfvg.qc.prompt_validation)."""
    subject: str
    suggestions: tuple[str, ...]
    tag_match: str = "contains"  # contains | equals


@dataclass(frozen=True)
class ShowProfile:
    schema: str = SCHEMA
    name: str = ""
    body_presets: dict = field(default_factory=dict)  # id -> BodyPreset
    sheet_lead: str = NEUTRAL_SHEET_LEAD
    cast: dict = field(default_factory=dict)  # slug -> CastEntry
    identity_profiles: dict = field(default_factory=dict)  # id -> hfvg.qc.identity.IdentityProfile
    style_note: str = ""
    flag_figures: tuple[FlagFigure, ...] = ()
    shot_flags: tuple[ShotFlag, ...] = ()
    character_codes: dict = field(default_factory=dict)  # code -> CharacterCode
    role_prefix: str = "{role}"
    lint: LintRules = field(default_factory=LintRules)
    playbook: Playbook = field(default_factory=Playbook)
    motion_suggestions: tuple[MotionSuggestion, ...] = ()
    scale_reference: str = ""
    gate_policy_file: str = ""
    real_people: str = "refuse"
    source: str = field(default="default", compare=False)
    path: str | None = field(default=None, compare=False)

    @property
    def digest(self) -> str:
        return profile_digest(self)

    def shot_flag(self, meta: str) -> ShotFlag | None:
        return next((f for f in self.shot_flags if f.meta == meta), None)

    def flag_names(self) -> set[str]:
        """Every shot_meta flag name the show declares (values and their declared markers)."""
        names = {f.meta for f in self.shot_flags}
        return names | {f.declared_meta for f in self.shot_flags if f.declared_meta}

    def flag_declared_false(self, shot_meta: dict | None, name: str) -> bool:
        """The shot declares flag ``name`` and it is false (a flag it does not declare makes no claim):
        declared through the flag's ``declared_meta`` marker when it has one, else by its presence."""
        meta = shot_meta if isinstance(shot_meta, dict) else {}
        spec = self.shot_flag(name)
        if spec is not None and spec.declared_meta:
            declared = bool(flag_value(meta, spec.declared_meta)[1])
        else:
            declared = isinstance(meta.get("flags"), dict) and name in meta["flags"]
        return declared and flag_value(meta, name)[1] is False

    def body_preset(self, preset_id: str | None) -> BodyPreset | None:
        return self.body_presets.get(preset_id) if preset_id else None

    def identity_profile_for(self, slug: str | None):
        entry = self.cast.get(slug) if slug else None
        if entry is None or not entry.identity_profile:
            return None
        return self.identity_profiles.get(entry.identity_profile)

    def summary(self) -> dict[str, Any]:
        """What the show-profile API and the preflight report (JSON-safe; no rule text)."""
        return {"schema": self.schema, "name": self.name, "source": self.source, "digest": self.digest,
                "body_presets": {k: b.label for k, b in sorted(self.body_presets.items())},
                "shot_flags": [f.meta for f in self.shot_flags],
                "identity_profiles": sorted(self.identity_profiles),
                "flag_figures": [f.figure_id for f in self.flag_figures],
                "playbook_rules": list(self.playbook.rule_ids()),
                "policy": {"real_people": self.real_people}}


# ------------------------------------------------------------------------------------------ parsing


def _strs(value: Any, where: str, problems: list[str], *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        problems.append(f"{where} must be a list of strings")
        return ()
    if not allow_empty and not value:
        problems.append(f"{where} must not be empty")
    return tuple(value)


def _str(value: Any, where: str, problems: list[str], *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        problems.append(f"{where} must be a {'non-empty ' if required else ''}string")
        return ""
    return value


def _bool(value: Any, where: str, problems: list[str], default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        problems.append(f"{where} must be true or false")
        return default
    return value


def _obj(value: Any, where: str, problems: list[str], keys: tuple[str, ...]) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} must be an object")
        return {}
    unknown = sorted(set(value) - set(keys))
    if unknown:
        problems.append(f"{where}: unknown keys {unknown}; known: {list(keys)}")
    return value


def _kinds(value: Any, where: str, problems: list[str], default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    kinds = _strs(value, where, problems, allow_empty=False)
    bad = [k for k in kinds if k not in LINT_KINDS]
    if bad:
        problems.append(f"{where}: {bad} not in {list(LINT_KINDS)}")
    return kinds


def _when(value: Any, where: str, problems: list[str]) -> Any:
    if value is None:
        return dict(ALWAYS)
    validate_when(value, where, problems)
    return value


def _template(value: Any, where: str, problems: list[str], default: str, fields: tuple[str, ...]) -> str:
    text = _str(value, where, problems) if value is not None else default
    try:
        text.format(**{f: "" for f in fields})
    except (KeyError, IndexError, ValueError) as e:
        problems.append(f"{where}: placeholders must be among {list(fields)} ({e})")
    return text


def _body_presets(raw: Any, problems: list[str]) -> dict:
    out = {}
    if raw is not None and not isinstance(raw, dict):
        problems.append("body_presets must be an object of preset id -> preset")
    for pid, data in (raw.items() if isinstance(raw, dict) else []):
        where = f"body_presets.{pid}"
        if not PRESET_ID_RE.match(str(pid)):
            problems.append(f"{where}: preset id must match {PRESET_ID_RE.pattern}")
            continue
        d = _obj(data, where, problems, ("label", "frame", "head", "aspects", "placeholder"))
        aspects = _strs(d.get("aspects", ["2:3", "3:4", "1:1"]), f"{where}.aspects", problems, allow_empty=False)
        bad = [a for a in aspects if not re.match(r"^\d+:\d+$", a)]
        if bad:
            problems.append(f"{where}.aspects: {bad} are not ratios like 3:4")
        size = d.get("placeholder", [768, 1152])
        if (not isinstance(size, list) or len(size) != 2
                or any(isinstance(v, bool) or not isinstance(v, int) or not 64 <= v <= 4096 for v in size)):
            problems.append(f"{where}.placeholder must be [width, height] in pixels (64-4096)")
            size = [768, 1152]
        out[pid] = BodyPreset(label=_str(d.get("label"), f"{where}.label", problems, required=True),
                              frame=_str(d.get("frame"), f"{where}.frame", problems, required=True),
                              head=_str(d.get("head"), f"{where}.head", problems, required=True),
                              aspects=aspects, placeholder=(size[0], size[1]))
    return out


def _cast(raw: Any, presets: dict, identity_ids: set[str], problems: list[str]) -> dict:
    from hfvg.cast_prompt import SLUG_RE
    from hfvg.cast_store import BODY_TYPES

    out = {}
    for slug, data in (raw.items() if isinstance(raw, dict) else []):
        where = f"cast.{slug}"
        if not SLUG_RE.match(str(slug)):
            problems.append(f"{where}: a cast id is lower-case letters, digits and dashes ({SLUG_RE.pattern})")
            continue
        d = _obj(data, where, problems, ("role", "tags", "body", "identity_profile"))
        body = _str(d.get("body"), f"{where}.body", problems)
        if body and body not in BODY_TYPES and body not in presets:
            problems.append(f"{where}.body {body!r} is not a body preset ({sorted({*BODY_TYPES, *presets})})")
        ident = _str(d.get("identity_profile"), f"{where}.identity_profile", problems)
        if ident and ident not in identity_ids:
            problems.append(f"{where}.identity_profile {ident!r} is not one of identity_profiles {sorted(identity_ids)}")
        out[slug] = CastEntry(role=_str(d.get("role"), f"{where}.role", problems),
                              tags=_strs(d.get("tags", []), f"{where}.tags", problems), body=body,
                              identity_profile=ident)
    if raw is not None and not isinstance(raw, dict):
        problems.append("cast must be an object of cast slugs")
    return out


def _identity_profiles(raw: Any, problems: list[str]) -> dict:
    from hfvg.qc.identity import IdentityProfile

    out = {}
    keys = ("name", "label", "signs", "apparel", "hint_signs", "state_key", "state_features",
            "min_height_fraction", "presence", "presence_note")
    for pid, data in (raw.items() if isinstance(raw, dict) else []):
        where = f"identity_profiles.{pid}"
        d = _obj(data, where, problems, keys)
        signs = d.get("signs")
        if not isinstance(signs, dict) or not signs or any(
                not isinstance(k, str) or not isinstance(v, str) or not v.strip() for k, v in signs.items()):
            problems.append(f"{where}.signs must be an object of sign -> what the judge compares")
            signs = {}
        apparel = d.get("apparel", {})
        if not isinstance(apparel, dict) or any(not isinstance(v, str) for v in apparel.values()):
            problems.append(f"{where}.apparel must be an object of item -> what correct is")
            apparel = {}
        hints = _strs(d.get("hint_signs", []), f"{where}.hint_signs", problems)
        if any(h not in signs for h in hints):
            problems.append(f"{where}.hint_signs must name signs")
        features = d.get("state_features", {})
        if not isinstance(features, dict) or any(
                not isinstance(v, list) or len(v) != 2 or any(not isinstance(x, str) for x in v)
                for v in features.values()):
            problems.append(f"{where}.state_features must be an object of state -> [feature, what correct is]")
            features = {}
        floor = d.get("min_height_fraction")
        if floor is not None and (isinstance(floor, bool) or not isinstance(floor, (int, float))
                                  or not 0 < floor < 1):
            problems.append(f"{where}.min_height_fraction must be a number between 0 and 1")
            floor = None
        presence = d.get("presence")
        if presence is not None and (not isinstance(presence, list) or len(presence) != 2
                                     or not isinstance(presence[0], str)):
            problems.append(f"{where}.presence must be [shot_context key, value] or null")
            presence = None
        out[pid] = IdentityProfile(
            name=_str(d.get("name", pid), f"{where}.name", problems, required=True),
            label=_str(d.get("label", "Character"), f"{where}.label", problems, required=True),
            signs=dict(signs), apparel=dict(apparel), hint_signs=hints,
            state_key=_str(d.get("state_key"), f"{where}.state_key", problems) or None,
            state_features={k: (v[0], v[1]) for k, v in features.items()},
            min_height_fraction=float(floor) if floor is not None else None,
            presence=(presence[0], presence[1]) if presence else None,
            presence_note=_str(d.get("presence_note", "declared in this shot"), f"{where}.presence_note", problems,
                               required=True))
    if raw is not None and not isinstance(raw, dict):
        problems.append("identity_profiles must be an object")
    return out


def _flag_figures(raw: Any, problems: list[str]) -> tuple[FlagFigure, ...]:
    out = []
    if raw is not None and not isinstance(raw, list):
        problems.append("flag_figures must be a list")
        return ()
    seen = set()
    for i, data in enumerate(raw or []):
        where = f"flag_figures[{i}]"
        d = _obj(data, where, problems, ("flag", "figure_id", "name", "description"))
        flag = _str(d.get("flag"), f"{where}.flag", problems, required=True)
        fid = _str(d.get("figure_id"), f"{where}.figure_id", problems, required=True)
        if fid and not re.match(r"^_[a-z0-9_]{1,63}$", fid):
            problems.append(f"{where}.figure_id must start with '_' (cast slugs never do), e.g. '_host'")
        if fid in seen:
            problems.append(f"{where}.figure_id {fid!r} is used twice")
        seen.add(fid)
        out.append(FlagFigure(flag=flag, figure_id=fid,
                              name=_str(d.get("name"), f"{where}.name", problems, required=True),
                              description=_str(d.get("description"), f"{where}.description", problems)))
    return tuple(out)


def _shot_flags(raw: Any, problems: list[str]) -> tuple[ShotFlag, ...]:
    if raw is not None and not isinstance(raw, list):
        problems.append("shot_flags must be a list")
        return ()
    out, metas = [], set()
    for i, data in enumerate(raw or []):
        where = f"shot_flags[{i}]"
        d = _obj(data, where, problems, ("key", "meta", "type", "declared_meta", "default", "top_level"))
        key = _str(d.get("key"), f"{where}.key", problems, required=True)
        meta = _str(d.get("meta", key), f"{where}.meta", problems, required=True)
        kind = d.get("type", "bool")
        if kind not in FLAG_TYPES:
            problems.append(f"{where}.type must be one of {list(FLAG_TYPES)}")
            kind = "bool"
        declared = _str(d.get("declared_meta"), f"{where}.declared_meta", problems)
        for name in (meta, declared):
            if name and not NAME_RE.match(name):
                problems.append(f"{where}: {name!r} is not a flag name ({NAME_RE.pattern})")
            if name and name in metas:
                problems.append(f"{where}: shot_meta name {name!r} is declared twice")
            metas.add(name)
        default = d.get("default")
        if default is not None and (type(default) is not FLAG_TYPES[kind]):
            problems.append(f"{where}.default must be a {kind}")
            default = None
        out.append(ShotFlag(key=key, meta=meta, type=kind, declared_meta=declared, default=default,
                            top_level=_bool(d.get("top_level"), f"{where}.top_level", problems, False)))
    return tuple(out)


def _beatmap(raw: Any, problems: list[str]) -> tuple[dict, str]:
    d = _obj(raw, "beatmap", problems, ("character_codes", "role_prefix"))
    codes = {}
    raw_codes = d.get("character_codes", {})
    if not isinstance(raw_codes, dict):
        problems.append("beatmap.character_codes must be an object of code -> {slug, role}")
        raw_codes = {}
    for code, data in raw_codes.items():
        where = f"beatmap.character_codes.{code}"
        if not re.match(r"^[A-Z]+\d*$", str(code)):
            problems.append(f"{where}: a character code is capital letters and an optional number (e.g. M1)")
        c = _obj(data, where, problems, ("slug", "role", "key", "absent"))
        key = _str(c.get("key"), f"{where}.key", problems)
        if key and not NAME_RE.match(key):
            problems.append(f"{where}.key must be a shot key name")
        codes[code] = CharacterCode(slug=_str(c.get("slug"), f"{where}.slug", problems, required=True),
                                    role=_str(c.get("role"), f"{where}.role", problems), key=key,
                                    absent=_str(c.get("absent"), f"{where}.absent", problems))
    prefix = _template(d.get("role_prefix"), "beatmap.role_prefix", problems, "{role}", ("role", "slug"))
    return codes, prefix


def _required_clauses(raw: Any, problems: list[str]) -> tuple[RequiredClause, ...]:
    if raw is not None and not isinstance(raw, list):
        problems.append("lint.required_clauses must be a list")
        return ()
    out, seen = [], set()
    keys = ("id", "text", "when", "kinds", "message", "missing_text_message")
    for i, data in enumerate(raw or []):
        where = f"lint.required_clauses[{i}]"
        d = _obj(data, where, problems, keys)
        cid = _str(d.get("id"), f"{where}.id", problems, required=True)
        if cid in seen:
            problems.append(f"{where}: id {cid!r} is used twice")
        seen.add(cid)
        text = _str(d.get("text"), f"{where}.text", problems)
        missing = _template(d.get("missing_text_message"), f"{where}.missing_text_message", problems, "",
                            ("id", "text"))
        if not text.strip() and not missing:
            problems.append(f"{where}: text must not be empty")
        out.append(RequiredClause(
            id=cid, text=text, when=_when(d.get("when"), f"{where}.when", problems),
            kinds=_kinds(d.get("kinds"), f"{where}.kinds", problems, ("still", "clip")),
            message=_template(d.get("message"), f"{where}.message", problems, DEFAULT_CLAUSE_MESSAGE, ("id", "text")),
            missing_text_message=missing))
    return tuple(out)


def _forbid_near(raw: Any, problems: list[str]) -> tuple[ForbidNear, ...]:
    if raw is not None and not isinstance(raw, list):
        problems.append("lint.forbid_near must be a list")
        return ()
    out, seen = [], set()
    keys = ("id", "words", "subjects", "scope", "match", "report", "when", "kinds", "after", "message", "max_quote")
    for i, data in enumerate(raw or []):
        where = f"lint.forbid_near[{i}]"
        d = _obj(data, where, problems, keys)
        rid = _str(d.get("id"), f"{where}.id", problems, required=True)
        if rid in seen:
            problems.append(f"{where}: id {rid!r} is used twice")
        seen.add(rid)
        choice = {}
        for name, allowed, default in (("scope", NEAR_SCOPES, "sentence"), ("match", NEAR_MATCHES, "word"),
                                       ("report", NEAR_REPORTS, "unit"), ("after", NEAR_AFTER, "forbidden_phrases")):
            value = d.get(name, default)
            if value not in allowed:
                problems.append(f"{where}.{name} must be one of {list(allowed)}")
                value = default
            choice[name] = value
        quote = d.get("max_quote")
        if quote is not None and (isinstance(quote, bool) or not isinstance(quote, int) or quote < 1):
            problems.append(f"{where}.max_quote must be a positive integer")
            quote = None
        out.append(ForbidNear(
            id=rid, words=_strs(d.get("words"), f"{where}.words", problems, allow_empty=False),
            subjects=_strs(d.get("subjects", []), f"{where}.subjects", problems),
            when=_when(d.get("when"), f"{where}.when", problems),
            kinds=_kinds(d.get("kinds"), f"{where}.kinds", problems, ("still", "clip")),
            message=_template(d.get("message"), f"{where}.message", problems, DEFAULT_NEAR_MESSAGE,
                              ("word", "subject", "sentence")),
            max_quote=quote, **choice))
    return tuple(out)


def parse_locks(raw: Any, problems: list[str], where: str = "lint.continuity_locks") -> tuple[ContinuityLock, ...]:
    """Continuity locks of a show profile (``when`` a predicate; the strings older kits used are read
    through hfvg.compat.legacy_show)."""
    from hfvg.compat.legacy_show import lock_when

    if raw is not None and not isinstance(raw, list):
        problems.append(f"{where} must be a list")
        return ()
    out, seen = [], set()
    for i, data in enumerate(raw or []):
        w = f"{where}[{i}]"
        d = _obj(data, w, problems, ("id", "text", "when", "kinds"))
        lid = _str(d.get("id"), f"{w}.id", problems, required=True)
        if lid in seen:
            problems.append(f"{w}: id {lid!r} is used twice")
        seen.add(lid)
        text = " ".join(_str(d.get("text"), f"{w}.text", problems, required=True).split())
        when = d.get("when")
        when = lock_when(when) if isinstance(when, str) and lock_when(when) is not None else when
        kinds = _kinds(d.get("kinds"), f"{w}.kinds", problems, ("still", "clip"))
        if any(k == "ref_edit" for k in kinds):
            problems.append(f"{w}.kinds: locks apply to still and clip prompts only")
        out.append(ContinuityLock(id=lid, text=text, when=_when(when, f"{w}.when", problems), kinds=kinds))
    return tuple(out)


def _lint(raw: Any, problems: list[str]) -> LintRules:
    keys = ("count_clause", "realtime_pattern", "locked_camera_clause", "forbidden_phrases", "banned_words",
            "banned_words_message", "content_policy_terms", "forbid_bare_waves", "required_clauses",
            "forbid_near", "continuity_locks")
    d = _obj(raw, "lint", problems, keys)
    count = _obj(d.get("count_clause"), "lint.count_clause", problems, ("template", "required"))
    template = _str(count.get("template"), "lint.count_clause.template", problems)
    if template and "{n}" not in template:
        problems.append("lint.count_clause.template must contain '{n}'")
    realtime = _str(d.get("realtime_pattern"), "lint.realtime_pattern", problems)
    try:
        re.compile(realtime)
    except re.error as e:
        problems.append(f"lint.realtime_pattern is not a valid regex: {e}")
    return LintRules(
        count_template=template,
        count_required=_bool(count.get("required"), "lint.count_clause.required", problems, True),
        realtime_pattern=realtime,
        locked_camera_clause=_str(d.get("locked_camera_clause"), "lint.locked_camera_clause", problems),
        forbidden_phrases=_strs(d.get("forbidden_phrases", list(NEUTRAL_FORBIDDEN_PHRASES)),
                                "lint.forbidden_phrases", problems),
        banned_words=_strs(d.get("banned_words", []), "lint.banned_words", problems),
        banned_words_message=_template(d.get("banned_words_message"), "lint.banned_words_message", problems,
                                       DEFAULT_BANNED_MESSAGE, ("word",)),
        content_policy_terms=_bool(d.get("content_policy_terms"), "lint.content_policy_terms", problems, True),
        forbid_bare_waves=_bool(d.get("forbid_bare_waves"), "lint.forbid_bare_waves", problems, False),
        required_clauses=_required_clauses(d.get("required_clauses"), problems),
        forbid_near=_forbid_near(d.get("forbid_near"), problems),
        continuity_locks=parse_locks(d.get("continuity_locks"), problems))


_PLAYBOOK_PARAMS = {
    "role_exclusion": ("role", "absent_flag", "absent_tags", "require_text", "forbid_tags"),
    "pose_words": ("when", "forbid_near", "words", "require_any", "require_when"),
    "world_refs": ("env", "types", "tags", "exempt_tags"),
}
_PLAYBOOK_MESSAGES = {
    "role_exclusion": ("message", "forbid_message"),
    "pose_words": ("message", "require_message"),
    "world_refs": ("message",),
}


def _playbook(raw: Any, problems: list[str]) -> Playbook:
    d = _obj(raw, "playbook", problems, ("rules", "hard"))
    rules, ids = [], []
    raw_rules = d.get("rules", [])
    if not isinstance(raw_rules, list):
        problems.append("playbook.rules must be a list")
        raw_rules = []
    for i, data in enumerate(raw_rules):
        where = f"playbook.rules[{i}]"
        kind = data.get("type") if isinstance(data, dict) else None
        if kind not in PLAYBOOK_TYPES:
            problems.append(f"{where}.type must be one of {list(PLAYBOOK_TYPES)}")
            continue
        r = _obj(data, where, problems, ("type", "id", *_PLAYBOOK_PARAMS[kind], *_PLAYBOOK_MESSAGES[kind]))
        rid = _str(r.get("id"), f"{where}.id", problems, required=True)
        if rid in ids or rid == ROUTING_RULE:
            problems.append(f"{where}: id {rid!r} is used twice or reserved")
        ids.append(rid)
        params = {k: r[k] for k in _PLAYBOOK_PARAMS[kind] if k in r}
        for key in ("when", "require_when"):
            if key in params:
                validate_when(params[key], f"{where}.{key}", problems)
        for key in ("absent_tags", "forbid_tags", "words", "require_any", "types", "tags", "exempt_tags"):
            if key in params:
                params[key] = list(_strs(params[key], f"{where}.{key}", problems))
        for key in ("role", "absent_flag", "require_text", "forbid_near", "env"):
            if key in params:
                _str(params[key], f"{where}.{key}", problems)
        if kind == "world_refs" and not params.get("env"):
            params["env"] = "HFVG_PLAYBOOK_WORLD_REFS"
        messages = {}
        for key in _PLAYBOOK_MESSAGES[kind]:
            if key in r:
                messages[key] = _template(r[key], f"{where}.{key}", problems, "", ("shot_id", "word"))
        if "message" not in messages:
            problems.append(f"{where}.message is required")
        rules.append(PlaybookRule(type=kind, id=rid, params=params, messages=messages))
    hard = _strs(d.get("hard", []), "playbook.hard", problems)
    unknown = [h for h in hard if h not in ids and h != ROUTING_RULE]
    if unknown:
        problems.append(f"playbook.hard names unknown rules {unknown}; rules: {[*ids, ROUTING_RULE]}")
    return Playbook(rules=tuple(rules), hard=hard)


def _motion(raw: Any, problems: list[str]) -> tuple[MotionSuggestion, ...]:
    if raw is not None and not isinstance(raw, list):
        problems.append("motion_suggestions must be a list")
        return ()
    out = []
    for i, data in enumerate(raw or []):
        where = f"motion_suggestions[{i}]"
        d = _obj(data, where, problems, ("subject", "suggestions", "tag_match"))
        match = d.get("tag_match", "contains")
        if match not in ("contains", "equals"):
            problems.append(f"{where}.tag_match must be contains or equals")
            match = "contains"
        out.append(MotionSuggestion(subject=_str(d.get("subject"), f"{where}.subject", problems, required=True).lower(),
                                    suggestions=_strs(d.get("suggestions"), f"{where}.suggestions", problems,
                                                      allow_empty=False), tag_match=match))
    return tuple(out)


TOP_KEYS = ("schema", "name", "body_presets", "sheet", "cast", "identity_profiles", "judges", "flag_figures",
            "shot_flags", "beatmap", "lint", "playbook", "motion_suggestions", "plate", "gates", "policy")


def parse_profile(data: Any, *, source: str = "episode", path: str | None = None) -> ShowProfile:
    """A validated profile, or ShowProfileError listing every problem."""
    problems: list[str] = []
    where = path or "show profile"
    if not isinstance(data, dict):
        raise ShowProfileError(["the profile must be a JSON object"], where)
    unknown = sorted(set(data) - set(TOP_KEYS))
    if unknown:
        problems.append(f"unknown keys {unknown}; known: {list(TOP_KEYS)}")
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}")
    presets = _body_presets(data.get("body_presets"), problems)
    identity_profiles = _identity_profiles(data.get("identity_profiles"), problems)
    sheet = _obj(data.get("sheet"), "sheet", problems, ("lead",))
    judges = _obj(data.get("judges"), "judges", problems, ("style_note",))
    plate = _obj(data.get("plate"), "plate", problems, ("scale_reference",))
    gates = _obj(data.get("gates"), "gates", problems, ("policy_file",))
    policy = _obj(data.get("policy"), "policy", problems, ("real_people",))
    real_people = policy.get("real_people", "refuse")
    if real_people not in REAL_PEOPLE:
        problems.append(f"policy.real_people must be one of {list(REAL_PEOPLE)} (docs/CONTENT-POLICY.md)")
    gate_file = _str(gates.get("policy_file"), "gates.policy_file", problems)
    if gate_file and (Path(gate_file).is_absolute() or ".." in Path(gate_file).parts):
        problems.append("gates.policy_file must be a file name inside the show's folder")
    codes, prefix = _beatmap(data.get("beatmap"), problems)
    shot_flags = _shot_flags(data.get("shot_flags"), problems)
    flag_figures = _flag_figures(data.get("flag_figures"), problems)
    known_flags = {f.meta for f in shot_flags}
    for i, fig in enumerate(flag_figures):
        if fig.flag and fig.flag not in known_flags:
            problems.append(f"flag_figures[{i}].flag {fig.flag!r} is not one of shot_flags {sorted(known_flags)}")
    profile = ShowProfile(
        name=_str(data.get("name"), "name", problems),
        body_presets=presets,
        sheet_lead=_str(sheet.get("lead", NEUTRAL_SHEET_LEAD), "sheet.lead", problems, required=True),
        cast=_cast(data.get("cast"), presets, set(identity_profiles), problems),
        identity_profiles=identity_profiles,
        style_note=_str(judges.get("style_note"), "judges.style_note", problems),
        flag_figures=flag_figures, shot_flags=shot_flags, character_codes=codes, role_prefix=prefix,
        lint=_lint(data.get("lint"), problems), playbook=_playbook(data.get("playbook"), problems),
        motion_suggestions=_motion(data.get("motion_suggestions"), problems),
        scale_reference=_str(plate.get("scale_reference"), "plate.scale_reference", problems),
        gate_policy_file=gate_file, real_people=real_people if real_people in REAL_PEOPLE else "refuse",
        source=source, path=path)
    if problems:
        raise ShowProfileError(problems, where)
    return profile


def neutral_profile() -> ShowProfile:
    """What a show with no data gets: content-policy lint terms, "nothing else moves", the motion
    rules, no subject or role rules, no playbook rule but the moderation routing, the neutral sheet
    lead and the neutral gate policy."""
    return ShowProfile(source="default")


# ------------------------------------------------------------------------------------------ canonical form


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _profile_identity(p) -> dict[str, Any]:
    return {"name": p.name, "label": p.label, "signs": dict(p.signs), "apparel": dict(p.apparel),
            "hint_signs": list(p.hint_signs), "state_key": p.state_key,
            "state_features": {k: list(v) for k, v in p.state_features.items()},
            "min_height_fraction": p.min_height_fraction, "presence": list(p.presence) if p.presence else None,
            "presence_note": p.presence_note}


def profile_dict(profile: ShowProfile) -> dict[str, Any]:
    """The profile as a complete show.json (every key written out): loading it gives an equal profile."""
    lint = profile.lint
    return _clean({
        "schema": profile.schema,
        "name": profile.name,
        "body_presets": {k: {"label": b.label, "frame": b.frame, "head": b.head, "aspects": b.aspects,
                             "placeholder": b.placeholder} for k, b in profile.body_presets.items()},
        "sheet": {"lead": profile.sheet_lead},
        "cast": {k: {"role": c.role, "tags": c.tags, "body": c.body, "identity_profile": c.identity_profile}
                 for k, c in profile.cast.items()},
        "identity_profiles": {k: _profile_identity(p) for k, p in profile.identity_profiles.items()},
        "judges": {"style_note": profile.style_note},
        "flag_figures": [{"flag": f.flag, "figure_id": f.figure_id, "name": f.name, "description": f.description}
                         for f in profile.flag_figures],
        "shot_flags": [{"key": f.key, "meta": f.meta, "type": f.type, "declared_meta": f.declared_meta,
                        "default": f.default, "top_level": f.top_level} for f in profile.shot_flags],
        "beatmap": {"character_codes": {k: {"slug": c.slug, "role": c.role, "key": c.key, "absent": c.absent}
                                        for k, c in profile.character_codes.items()},
                    "role_prefix": profile.role_prefix},
        "lint": {
            "count_clause": {"template": lint.count_template, "required": lint.count_required},
            "realtime_pattern": lint.realtime_pattern,
            "locked_camera_clause": lint.locked_camera_clause,
            "forbidden_phrases": lint.forbidden_phrases,
            "banned_words": lint.banned_words,
            "banned_words_message": lint.banned_words_message,
            "content_policy_terms": lint.content_policy_terms,
            "forbid_bare_waves": lint.forbid_bare_waves,
            "required_clauses": [{"id": c.id, "text": c.text, "when": c.when, "kinds": c.kinds,
                                  "message": c.message, "missing_text_message": c.missing_text_message}
                                 for c in lint.required_clauses],
            "forbid_near": [{"id": r.id, "words": r.words, "subjects": r.subjects, "scope": r.scope,
                             "match": r.match, "report": r.report, "when": r.when, "kinds": r.kinds,
                             "after": r.after, "message": r.message, "max_quote": r.max_quote}
                            for r in lint.forbid_near],
            "continuity_locks": [{"id": lk.id, "text": lk.text, "when": lk.when, "kinds": lk.kinds}
                                 for lk in lint.continuity_locks],
        },
        "playbook": {"rules": [{"type": r.type, "id": r.id, **r.params, **r.messages} for r in profile.playbook.rules],
                     "hard": profile.playbook.hard},
        "motion_suggestions": [{"subject": m.subject, "suggestions": m.suggestions, "tag_match": m.tag_match}
                               for m in profile.motion_suggestions],
        "plate": {"scale_reference": profile.scale_reference},
        "gates": {"policy_file": profile.gate_policy_file},
        "policy": {"real_people": profile.real_people},
    })


def profile_digest(profile: ShowProfile) -> str:
    blob = json.dumps(profile_dict(profile), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------ loading


def recompose_flags_problem(profile: ShowProfile, flags: dict) -> str | None:
    """Why a recompose's ``shot_meta.flags`` patch does not fit the show (None: it does): every name
    one of its shot flags (or a flag's declared marker), every value of that flag's type."""
    names = profile.flag_names()
    undeclared = sorted(str(k) for k in flags if k not in names)
    if undeclared:
        return f"shot_meta.flags {undeclared} are not shot flags of this show; it declares {sorted(names)}"
    for name, value in flags.items():
        spec = profile.shot_flag(name)
        kind = spec.type if spec is not None else "bool"
        if type(value) is not FLAG_TYPES[kind]:
            return f"shot_meta.flags.{name} must be a {kind}, got {value!r}"
    return None


def load_show_profile(path: str | os.PathLike, source: str = "episode") -> ShowProfile:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ShowProfileError([f"cannot read the profile ({e})"], str(p)) from e
    return parse_profile(data, source=source, path=str(p))


def episode_profile_path(episode_id: str) -> Path:
    from hfvg.paths import episode_dir
    return episode_dir(episode_id) / PROFILE_FILE


def resolve_show_profile(episode_id: str | None) -> ShowProfile:
    """The episode's show profile (module docstring for the order). Raises ShowProfileError for a
    show.json (or HFVG_SHOW_PROFILE_PATH file) that does not validate: never a silent fallback."""
    from hfvg.compat import legacy_show
    from hfvg.paths import episode_dir

    folder = episode_dir(episode_id) if episode_id else None
    if folder is not None and (folder / PROFILE_FILE).is_file():
        return load_show_profile(folder / PROFILE_FILE, "episode")
    env_path = os.getenv(PROFILE_PATH_ENV, "").strip()
    if env_path:
        return load_show_profile(env_path, "env")
    if legacy_show.has_legacy_inputs(folder):
        return legacy_show.adapter_profile(folder)
    return neutral_profile()


def declared_source(episode_id: str | None) -> str | None:
    """"episode" or "env" when the episode's profile is a show.json someone wrote, else None."""
    if episode_id and episode_profile_path(episode_id).is_file():
        return "episode"
    return "env" if os.getenv(PROFILE_PATH_ENV, "").strip() else None


def show_profile_problems(episode_id: str) -> list[str]:
    """Why the episode's show profile cannot load ([] when it loads): checked by the preflight."""
    try:
        resolve_show_profile(episode_id)
    except ShowProfileError as e:
        return [f"show profile {e.where}: {p}" for p in e.problems]
    return []


def with_source(profile: ShowProfile, source: str, path: str | None = None) -> ShowProfile:
    return replace(profile, source=source, path=path)
