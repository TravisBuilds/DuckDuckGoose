"""Prompt validation rules (HARNESS-GATES v1.1).

Rule: Never end a video prompt with 'nothing else moves'.
Require named ambient motion instead.
"""

import re
from typing import Any


class PromptValidationError(Exception):
    """Raised when a prompt violates validation rules."""
    pass


def validate_prompt(prompt: str, shot_params: dict[str, Any] | None = None) -> str:
    """
    Validate a video prompt against HARNESS rules.
    
    Args:
        prompt: The video generation prompt
        shot_params: Optional shot parameters (for context)
    
    Returns:
        Validated prompt (possibly auto-corrected)
    
    Raises:
        PromptValidationError: If prompt violates rules and cannot be auto-corrected
    """
    # Rule: reject "nothing else moves" endings
    # Check for variations at end of prompt
    forbidden_endings = [
        r"nothing else moves\.?$",
        r"everything else (?:is )?still\.?$",
        r"all else (?:is )?static\.?$",
        r"only .+ moves\.?$",
        r"rest (?:of the scene )?(?:is )?frozen\.?$",
    ]
    
    for pattern in forbidden_endings:
        if re.search(pattern, prompt.strip(), re.IGNORECASE):
            raise PromptValidationError(
                f"Prompt ends with forbidden static clause: '{prompt[-50:]}'. "
                "Name the ambient motion instead (snowfall, steam, breath, aurora glow, "
                "lamp flicker, ear flicks, etc.)."
            )
    
    # Check for generic static references in the middle
    if re.search(r"\bnothing (?:else )?moves\b", prompt, re.IGNORECASE):
        raise PromptValidationError(
            "Prompt contains 'nothing moves' clause. "
            "Every shot must be animatable with named ambient motion."
        )
    
    return prompt


def suggest_ambient_motion(shot_context: dict[str, Any]) -> list[str]:
    """
    Suggest appropriate ambient motion for a shot based on context.
    
    Args:
        shot_context: Shot parameters (tags, room, tod, etc.)
    
    Returns:
        List of suggested ambient motion elements
    """
    suggestions = []
    
    # Get context
    tags = shot_context.get("tags", [])
    tags_lower = [t.lower() for t in tags]
    room = shot_context.get("room", "").lower()
    tod = shot_context.get("tod", "").lower()
    
    # Outdoor/weather
    if any(w in tags_lower for w in ["exterior", "outdoor"]):
        if "snow" in tod or "snow" in room:
            suggestions.extend(["snowfall", "snow drifting", "snow sparkle"])
        if "wind" in tags_lower:
            suggestions.extend(["branches swaying", "snow blowing"])
    
    # Interior lighting
    if any(w in tags_lower for w in ["interior", "indoor"]):
        suggestions.extend(["warm light flicker", "lamp glow"])
    
    # Water/steam
    if any(w in tags_lower for w in ["pool", "water", "steam"]):
        suggestions.extend(["steam drifting", "ripples", "water droplets"])
    
    # Night/aurora
    if "night" in tod or "aurora" in tod:
        suggestions.extend(["aurora glowing softly", "dome windows glow"])
    
    # Animal motion
    if "moose" in room or any("moose" in t for t in tags_lower):
        suggestions.extend(["breath steam", "ear flicks", "tail swish", "head turn"])
    
    if "duck" in room or "duck" in tags_lower:
        suggestions.extend(["tuque pom bobs", "bill tilt", "head turn"])
    
    # Default atmospheric
    if not suggestions:
        suggestions.extend(["breath steam", "gentle motion", "subtle movement"])
    
    return suggestions[:3]  # Return top 3 suggestions


def check_ambient_motion_named(prompt: str) -> bool:
    """
    Check if prompt names specific ambient motion.
    
    Returns:
        True if ambient motion is explicitly named
    """
    ambient_keywords = [
        "snowfall", "snow", "steam", "breath", "aurora", "glow",
        "flicker", "ripples", "ear flick", "tail", "pom bob",
        "swaying", "drifting", "sparkling", "shimmer",
    ]
    
    prompt_lower = prompt.lower()
    return any(keyword in prompt_lower for keyword in ambient_keywords)
