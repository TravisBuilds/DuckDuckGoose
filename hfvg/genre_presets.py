"""Genre preset registry: hfvg/data/genre_presets.yaml, loaded and validated once per process.

The API resolves a card id into a plain JSON profile (``resolve_profile``) and hands that dict to
the workflow as an input, so workflow code never reads this file. ``web/lib/presets.ts`` mirrors
the card data and tests/test_preset_sync.py fails when the two drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

REGISTRY_PATH = Path(__file__).parent / "data" / "genre_presets.yaml"
VALID_ASPECTS = ("9:16", "16:9", "4:3", "16:9_rotated_to_9:16")

# Bounds of any requested draft length; each card then clamps to its own length_s range.
DRAFT_MIN_S = 10
DRAFT_MAX_S = 180

# "Skip" in the wizard: no card. Same 30-120 s window and 60 s default the wizard always had.
FREEFORM_ID = "freeform"
FREEFORM_PROFILE: dict[str, Any] = {
    "id": FREEFORM_ID,
    "name": "Freeform",
    "template": "freeform",
    "story_structure": "three_act",
    "video_prompt_mode": "cut",
    "aspect": "9:16",
    "length_s": [30, 120],
    "default_length_s": 60,
    "cuts_per_10s": 2.0,
    "transitions": ["cut"],
    "shot_grammar": [],
    "brief_fields": [],
    "vo_mode": "none",
    "music_mode": "optional",
    "music": "cinematic underscore",
    "risk_flags": [],
    "gates_add": [],
    "ip_mode": "original_only",
}

BANNED_TERMS = (
    "disney", "pixar", "ghibli", "marvel", "dc comics", "pokemon", "naruto", "harry potter",
    "star wars", "lord of the rings", "attack on titan", "demon slayer", "spirited away",
    "jurassic park", "blockbuster", "kfc",
)


class Template(str, Enum):
    MOOD_LOOP = "mood_loop"
    MUSIC_MONTAGE = "music_montage"
    DIALOGUE_EPISODE = "dialogue_episode"
    CONTINUOUS_POV = "continuous_pov"
    COZY_MICRO = "cozy_micro"
    ACTION_SEQUENCE = "action_sequence"
    GAG_SHORT = "gag_short"


class VOMode(str, Enum):
    NONE = "none"
    NARRATION = "narration"
    DIALOGUE = "dialogue"
    CAPTION_ONLY = "caption_only"


class MusicMode(str, Enum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class VideoPromptMode(str, Enum):
    CUT = "cut"
    CONTINUOUS = "continuous"
    LOOP = "loop"


class StoryStructure(str, Enum):
    THREE_ACT = "three_act"
    LOOP = "loop"
    ESCALATION_PUNCHLINE = "escalation_punchline"
    MONTAGE = "montage"
    CONTINUOUS_JOURNEY = "continuous_journey"
    EPISODE_CLIFFHANGER = "episode_cliffhanger"
    MICRO_ARC = "micro_arc"
    ESCALATION = "escalation"


class IPMode(str, Enum):
    ORIGINAL_ONLY = "original_only"
    PUBLIC_DOMAIN_OK = "public_domain_ok"
    LICENCE_ATTESTED = "licence_attested"


class RiskFlag(str, Enum):
    IP_THIRDPARTY = "ip_thirdparty"
    REAL_LIKENESS = "real_likeness"
    POLITICAL = "political"
    VIOLENCE = "violence"
    SEXUALIZATION = "sexualization"
    MINORS = "minors"
    TRADEMARK = "trademark"
    FACTUAL_CLAIMS = "factual_claims"
    SYNTHETIC_HUMAN = "synthetic_human"


class Gate(str, Enum):
    BRIEF_SAFETY = "brief_safety"
    BEATMAP_APPROVAL = "beatmap_approval"
    STILLS_STRIP = "stills_strip"
    STILLS_SAFETY = "stills_safety"
    MODERATION_PRECHECK = "moderation_precheck"
    LIKENESS_CONSENT = "likeness_consent"
    FACT_CHECK = "fact_check"
    IP_ATTESTATION = "ip_attestation"
    MANUAL_REVIEW = "manual_review"
    FINAL_APPROVAL = "final_approval"


class IdentityLock(str, Enum):
    CHARACTER = "character"
    STYLE = "style"
    SETTING = "setting"
    PROP = "prop"
    VOICE = "voice"
    POV_RIG = "pov_rig"
    LIKENESS_CONSENT = "likeness_consent"


@dataclass
class PacingData:
    cuts_per_10s: float
    transitions: list[str] = field(default_factory=lambda: ["cut"])


@dataclass
class TemplateData:
    """A format family: defaults every preset of that template inherits."""
    description: str
    length_s: list[int]
    pacing: PacingData
    vo_mode: VOMode
    music_mode: MusicMode
    video_prompt_mode: VideoPromptMode
    story_structure: StoryStructure
    stage_overrides: dict[str, str] = field(default_factory=dict)
    gates_add: list[Gate] = field(default_factory=list)


@dataclass
class GenrePreset:
    """One archetype card: template, aspect, length range, pacing, sound modes, risks and gates."""
    id: str
    name: str
    description: str
    template: Template
    aspect: str
    length_s: list[int]
    pacing: PacingData
    shot_grammar: list[str]
    brief_fields: list[str]
    vo_mode: VOMode
    music: str
    default_length_s: int = 0
    identity_locks: list[IdentityLock] = field(default_factory=list)
    risk_flags: list[RiskFlag] = field(default_factory=list)
    stage_overrides: dict[str, str] = field(default_factory=dict)
    gates_add: list[Gate] = field(default_factory=list)
    ip_mode: IPMode = IPMode.ORIGINAL_ONLY
    music_mode: MusicMode = MusicMode.OPTIONAL
    video_prompt_mode: VideoPromptMode | None = None
    story_structure: StoryStructure | None = None

    def __post_init__(self) -> None:
        if len(self.length_s) != 2 or not all(isinstance(v, int) for v in self.length_s):
            raise ValueError(f"Preset {self.id}: length_s must be [min, max] whole seconds")
        lo, hi = self.length_s
        if not DRAFT_MIN_S <= lo <= hi <= DRAFT_MAX_S:
            raise ValueError(
                f"Preset {self.id}: length_s {self.length_s} must lie within "
                f"[{DRAFT_MIN_S}, {DRAFT_MAX_S}] with min <= max")
        if not self.default_length_s:
            self.default_length_s = (lo + hi) // 2
        if not lo <= self.default_length_s <= hi:
            raise ValueError(
                f"Preset {self.id}: default_length_s {self.default_length_s} outside {self.length_s}")
        if self.aspect not in VALID_ASPECTS:
            raise ValueError(f"Preset {self.id}: invalid aspect ratio {self.aspect}")

    def profile(self) -> dict[str, Any]:
        """JSON-safe snapshot of everything drafting, story shaping and audio need from the card."""
        return {
            "id": self.id,
            "name": self.name,
            "template": self.template.value,
            "story_structure": (self.story_structure or StoryStructure.THREE_ACT).value,
            "video_prompt_mode": (self.video_prompt_mode or VideoPromptMode.CUT).value,
            "aspect": self.aspect,
            "length_s": list(self.length_s),
            "default_length_s": self.default_length_s,
            "cuts_per_10s": float(self.pacing.cuts_per_10s),
            "transitions": list(self.pacing.transitions),
            "shot_grammar": list(self.shot_grammar),
            "brief_fields": list(self.brief_fields),
            "vo_mode": self.vo_mode.value,
            "music_mode": self.music_mode.value,
            "music": self.music,
            "risk_flags": [f.value for f in self.risk_flags],
            "gates_add": [g.value for g in self.gates_add],
            "ip_mode": self.ip_mode.value,
        }


@dataclass
class GenrePresetsRegistry:
    """All templates and presets of one registry file."""
    schema_version: str
    templates: dict[str, TemplateData]
    defaults: dict[str, Any]
    presets: dict[str, GenrePreset]

    def get_preset(self, preset_id: str) -> GenrePreset | None:
        return self.presets.get(preset_id)

    def get_presets_by_template(self, template: Template) -> list[GenrePreset]:
        return [p for p in self.presets.values() if p.template == template]

    def validate_all(self) -> list[str]:
        """Cross-preset checks; an empty list means the registry is usable."""
        errors = []
        for preset in self.presets.values():
            if preset.template.value not in self.templates:
                errors.append(f"Preset {preset.id}: unknown template {preset.template.value}")
            text = f"{preset.name} {preset.description}".lower()
            errors.extend(f"Preset {preset.id}: contains named IP '{term}' in name/description"
                          for term in BANNED_TERMS if term in text)
        return errors


def _template(name: str, raw: dict[str, Any]) -> TemplateData:
    pacing = raw.get("pacing") or {}
    return TemplateData(
        description=raw["description"],
        length_s=list(raw["length_s"]),
        pacing=PacingData(cuts_per_10s=pacing["cuts_per_10s"],
                          transitions=list(pacing.get("transitions", ["cut"]))),
        vo_mode=VOMode(raw["vo_mode"]),
        music_mode=MusicMode(raw.get("music_mode", "optional")),
        video_prompt_mode=VideoPromptMode(raw["video_prompt_mode"]),
        story_structure=StoryStructure(raw["story_structure"]),
        stage_overrides=dict(raw.get("stage_overrides") or {}),
        gates_add=[Gate(g) for g in raw.get("gates_add") or []],
    )


def _preset(raw: dict[str, Any], templates: dict[str, TemplateData],
            defaults: dict[str, Any]) -> GenrePreset:
    preset_id = raw["id"]
    template = templates.get(raw["template"])
    if template is None:
        raise ValueError(f"Preset {preset_id}: unknown template {raw['template']}")
    pacing = raw.get("pacing") or {}
    return GenrePreset(
        id=preset_id,
        name=raw["name"],
        description=raw["description"],
        template=Template(raw["template"]),
        aspect=raw["aspect"],
        length_s=list(raw.get("length_s", template.length_s)),
        default_length_s=int(raw.get("default_length_s") or 0),
        pacing=PacingData(
            cuts_per_10s=pacing.get("cuts_per_10s", template.pacing.cuts_per_10s),
            transitions=list(pacing.get("transitions", template.pacing.transitions)),
        ),
        shot_grammar=list(raw.get("shot_grammar") or []),
        brief_fields=list(raw.get("brief_fields") or []),
        vo_mode=VOMode(raw.get("vo_mode", template.vo_mode.value)),
        music=raw.get("music", ""),
        music_mode=MusicMode(raw.get("music_mode", template.music_mode.value)),
        video_prompt_mode=VideoPromptMode(raw.get("video_prompt_mode", template.video_prompt_mode.value)),
        story_structure=StoryStructure(raw.get("story_structure", template.story_structure.value)),
        identity_locks=[IdentityLock(v) for v in raw.get("identity_locks") or []],
        risk_flags=[RiskFlag(v) for v in raw.get("risk_flags") or []],
        stage_overrides={**template.stage_overrides, **(raw.get("stage_overrides") or {})},
        gates_add=list(dict.fromkeys(
            Gate(g) for g in [*template.gates_add, *(raw.get("gates_add") or [])])),
        ip_mode=IPMode(raw.get("ip_mode", defaults.get("ip_mode", "original_only"))),
    )


def load_genre_presets(yaml_path: Path | None = None) -> GenrePresetsRegistry:
    """Parse and validate the registry file. Raises ValueError on any schema problem."""
    path = yaml_path or REGISTRY_PATH
    if not path.exists():
        raise FileNotFoundError(f"Genre presets file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
        raise ValueError("Genre presets YAML must be a mapping with a 'presets' list")
    templates = {name: _template(name, raw) for name, raw in (data.get("templates") or {}).items()}
    defaults = dict(data.get("defaults") or {})
    presets: dict[str, GenrePreset] = {}
    for raw in data["presets"]:
        preset = _preset(raw, templates, defaults)
        if preset.id in presets or preset.id == FREEFORM_ID:
            raise ValueError(f"Duplicate or reserved preset id {preset.id}")
        presets[preset.id] = preset
    registry = GenrePresetsRegistry(schema_version=str(data.get("schema_version", "0.1")),
                                    templates=templates, defaults=defaults, presets=presets)
    errors = registry.validate_all()
    if errors:
        raise ValueError("Genre presets validation failed:\n" + "\n".join(errors))
    return registry


_REGISTRY: GenrePresetsRegistry | None = None


def get_genre_presets_registry() -> GenrePresetsRegistry:
    """The process-wide registry (loaded on first use)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_genre_presets()
    return _REGISTRY


def get_preset(preset_id: str) -> GenrePreset | None:
    return get_genre_presets_registry().get_preset(preset_id)


def get_presets_by_template(template: Template) -> list[GenrePreset]:
    return get_genre_presets_registry().get_presets_by_template(template)


def resolve_profile(archetype_id: str | None) -> dict[str, Any]:
    """The JSON profile for a card id; no id (wizard Skip) gives the freeform profile.

    Raises KeyError for an id that is not in the registry."""
    if not archetype_id or archetype_id == FREEFORM_ID:
        return {**FREEFORM_PROFILE, "length_s": list(FREEFORM_PROFILE["length_s"])}
    preset = get_preset(archetype_id)
    if preset is None:
        raise KeyError(archetype_id)
    return preset.profile()


def clamp_length(profile: dict[str, Any], requested: float | None) -> tuple[int, str | None]:
    """The requested length clamped into the card's range, plus a note when it had to move."""
    lo, hi = (int(v) for v in profile["length_s"])
    if requested is None:
        return int(profile["default_length_s"]), None
    value = int(round(float(requested)))
    clamped = min(hi, max(lo, value))
    if clamped == value:
        return clamped, None
    return clamped, (f"{profile['name']} videos run {lo}-{hi} seconds, so the plan uses "
                     f"{clamped} seconds instead of {value}.")
