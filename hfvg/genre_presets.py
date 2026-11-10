"""Genre presets registry: load and validate genre_presets.yaml at startup."""

import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum

# ============================================================================
# Schema definitions
# ============================================================================

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
    transitions: List[str] = field(default_factory=lambda: ["cut"])


@dataclass
class GenrePreset:
    """A genre preset defining template, aspect, length, pacing, voice/music modes, etc."""
    id: str
    name: str
    description: str
    template: Template
    aspect: str
    length_s: List[int]  # [min, max]
    pacing: PacingData
    shot_grammar: List[str]
    brief_fields: List[str]
    vo_mode: VOMode
    music: str
    identity_locks: List[IdentityLock] = field(default_factory=list)
    risk_flags: List[RiskFlag] = field(default_factory=list)
    stage_overrides: Dict[str, str] = field(default_factory=dict)
    gates_add: List[Gate] = field(default_factory=list)
    ip_mode: IPMode = IPMode.ORIGINAL_ONLY
    music_mode: MusicMode = MusicMode.OPTIONAL
    video_prompt_mode: Optional[VideoPromptMode] = None
    story_structure: Optional[StoryStructure] = None

    def __post_init__(self):
        # Validate length range
        if len(self.length_s) != 2:
            raise ValueError(f"Preset {self.id}: length_s must be [min, max]")
        if self.length_s[0] > self.length_s[1]:
            raise ValueError(f"Preset {self.id}: min length > max length")
        
        # Validate aspect ratio format
        valid_aspects = ["9:16", "16:9", "4:3", "16:9_rotated_to_9:16"]
        if self.aspect not in valid_aspects:
            raise ValueError(f"Preset {self.id}: invalid aspect ratio {self.aspect}")


@dataclass
class TemplateData:
    """Template definition with defaults."""
    description: str
    length_s: List[int]
    pacing: PacingData
    vo_mode: VOMode
    music_mode: MusicMode
    video_prompt_mode: VideoPromptMode
    story_structure: StoryStructure
    stage_overrides: Dict[str, str] = field(default_factory=dict)
    gates_add: List[Gate] = field(default_factory=list)


@dataclass
class GenrePresetsRegistry:
    """The complete registry of genre presets and templates."""
    schema_version: str
    templates: Dict[str, TemplateData]
    defaults: Dict[str, Any]
    presets: Dict[str, GenrePreset]  # id -> preset

    def get_preset(self, preset_id: str) -> Optional[GenrePreset]:
        """Get preset by ID."""
        return self.presets.get(preset_id)

    def get_presets_by_template(self, template: Template) -> List[GenrePreset]:
        """Get all presets for a given template."""
        return [p for p in self.presets.values() if p.template == template]

    def validate_all(self) -> List[str]:
        """Validate all presets and return list of errors."""
        errors = []
        
        # Check for duplicate IDs
        if len(self.presets) != len(set(self.presets.keys())):
            errors.append("Duplicate preset IDs found")
        
        # Check each preset has valid template
        for preset in self.presets.values():
            if preset.template.value not in self.templates:
                errors.append(f"Preset {preset.id}: unknown template {preset.template}")
        
        # Check for IP violations in names/descriptions
        banned_terms = [
            "disney", "pixar", "ghibli", "marvel", "dc comics", "pokemon", "naruto",
            "harry potter", "star wars", "lord of the rings", "attack on titan",
            "demon slayer", "spirited away", "jurassic park", "blockbuster", "kfc"
        ]
        for preset in self.presets.values():
            text = (preset.name + " " + preset.description).lower()
            for term in banned_terms:
                if term in text:
                    errors.append(
                        f"Preset {preset.id}: contains named IP '{term}' in name/description"
                    )
        
        return errors


# ============================================================================
# Registry loader
# ============================================================================

_REGISTRY: Optional[GenrePresetsRegistry] = None


def load_genre_presets(yaml_path: Optional[Path] = None) -> GenrePresetsRegistry:
    """
    Load and validate genre presets from YAML file.
    
    Args:
        yaml_path: Path to genre_presets.yaml (defaults to hfvg/data/genre_presets.yaml)
    
    Returns:
        GenrePresetsRegistry instance
    
    Raises:
        FileNotFoundError: If YAML file not found
        ValueError: If YAML is invalid or fails validation
    """
    if yaml_path is None:
        yaml_path = Path(__file__).parent / "data" / "genre_presets.yaml"
    
    if not yaml_path.exists():
        raise FileNotFoundError(f"Genre presets file not found: {yaml_path}")
    
    with open(yaml_path, "r") as f:
        data = yaml.safe_load(f)
    
    if not isinstance(data, dict):
        raise ValueError("Genre presets YAML must be a dictionary")
    
    # Parse schema version
    schema_version = data.get("schema_version", "0.1")
    
    # Parse templates
    templates_raw = data.get("templates", {})
    templates = {}
    for template_name, template_data in templates_raw.items():
        pacing_raw = template_data.get("pacing", {})
        templates[template_name] = TemplateData(
            description=template_data["description"],
            length_s=template_data["length_s"],
            pacing=PacingData(
                cuts_per_10s=pacing_raw["cuts_per_10s"],
                transitions=pacing_raw.get("transitions", ["cut"])
            ),
            vo_mode=VOMode(template_data["vo_mode"]),
            music_mode=MusicMode(template_data.get("music_mode", "optional")),
            video_prompt_mode=VideoPromptMode(template_data["video_prompt_mode"]),
            story_structure=StoryStructure(template_data["story_structure"]),
            stage_overrides=template_data.get("stage_overrides", {}),
            gates_add=[Gate(g) for g in template_data.get("gates_add", [])]
        )
    
    # Parse defaults
    defaults = data.get("defaults", {})
    
    # Parse presets
    presets_raw = data.get("presets", [])
    presets = {}
    for preset_data in presets_raw:
        preset_id = preset_data["id"]
        template_name = preset_data["template"]
        template = templates.get(template_name)
        
        if template is None:
            raise ValueError(f"Preset {preset_id}: unknown template {template_name}")
        
        # Build pacing from preset or template
        pacing_raw = preset_data.get("pacing", {})
        pacing = PacingData(
            cuts_per_10s=pacing_raw.get("cuts_per_10s", template.pacing.cuts_per_10s),
            transitions=pacing_raw.get("transitions", template.pacing.transitions)
        )
        
        # Inherit from template where not specified
        preset = GenrePreset(
            id=preset_id,
            name=preset_data["name"],
            description=preset_data["description"],
            template=Template(template_name),
            aspect=preset_data["aspect"],
            length_s=preset_data.get("length_s", template.length_s),
            pacing=pacing,
            shot_grammar=preset_data.get("shot_grammar", []),
            brief_fields=preset_data.get("brief_fields", []),
            vo_mode=VOMode(preset_data.get("vo_mode", template.vo_mode.value)),
            music=preset_data.get("music", ""),
            music_mode=MusicMode(preset_data.get("music_mode", template.music_mode.value)),
            video_prompt_mode=VideoPromptMode(
                preset_data.get("video_prompt_mode", template.video_prompt_mode.value)
            ) if preset_data.get("video_prompt_mode") or template.video_prompt_mode else None,
            story_structure=StoryStructure(
                preset_data.get("story_structure", template.story_structure.value)
            ) if preset_data.get("story_structure") or template.story_structure else None,
            identity_locks=[IdentityLock(lock) for lock in preset_data.get("identity_locks", [])],
            risk_flags=[RiskFlag(flag) for flag in preset_data.get("risk_flags", [])],
            stage_overrides={**template.stage_overrides, **preset_data.get("stage_overrides", {})},
            gates_add=[Gate(g) for g in preset_data.get("gates_add", [])],
            ip_mode=IPMode(preset_data.get("ip_mode", defaults.get("ip_mode", "original_only")))
        )
        
        presets[preset_id] = preset
    
    registry = GenrePresetsRegistry(
        schema_version=schema_version,
        templates=templates,
        defaults=defaults,
        presets=presets
    )
    
    # Validate
    errors = registry.validate_all()
    if errors:
        raise ValueError(f"Genre presets validation failed:\n" + "\n".join(errors))
    
    return registry


def get_genre_presets_registry() -> GenrePresetsRegistry:
    """
    Get the singleton genre presets registry (loaded once at startup).
    
    Returns:
        GenrePresetsRegistry instance
    
    Raises:
        RuntimeError: If registry not initialized (call load_genre_presets first)
    """
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_genre_presets()
    return _REGISTRY


def get_preset(preset_id: str) -> Optional[GenrePreset]:
    """Get preset by ID (convenience wrapper)."""
    return get_genre_presets_registry().get_preset(preset_id)


def get_presets_by_template(template: Template) -> List[GenrePreset]:
    """Get all presets for a given template (convenience wrapper)."""
    return get_genre_presets_registry().get_presets_by_template(template)
