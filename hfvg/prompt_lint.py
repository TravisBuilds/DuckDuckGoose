"""Prompt lint gate: a code gate that runs BEFORE estimate / reserve / provider call.

``lint_prompt(prompt, shot_meta, kit)`` raises PromptLintError listing every violation.
Clause texts (count clause, locked-camera clause, real-time pattern, antler/duck clauses, banned
decor list) come from a LintKit loaded from the per-episode ``prompt_kit.json`` (``"lint"`` section)
or from the file named by ``PROMPT_LINT_KIT_PATH``; none is hardcoded here.

shot_meta keys: ``count`` (int), ``exterior`` / ``water`` (bool), ``characters`` (list[str]).

Live mode: any violation (or missing kit / shot_meta) refuses. Dry mode: warn only.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_FORBIDDEN_PHRASES = ("nothing else moves", "realistic wild")
DEFAULT_CARRY_VERBS = (
    "carry", "carries", "carrying", "carried", "hold", "holds", "holding", "held",
)


class PromptLintError(ValueError):
    """The prompt violates the lint rules (refused before any paid call)."""


@dataclass(frozen=True)
class LintKit:
    count_template: str  # must contain "{n}", e.g. "<clause with {n}>"
    realtime_pattern: str  # regex for the real-time / no-slow-motion clause
    locked_camera_clause: str  # required on exterior/water shots
    antler_clause: str = ""
    antler_characters: tuple[str, ...] = ()
    duck_clause: str = ""
    duck_characters: tuple[str, ...] = ()
    banned_decor: tuple[str, ...] = ()
    forbidden_phrases: tuple[str, ...] = DEFAULT_FORBIDDEN_PHRASES
    moose_names: tuple[str, ...] = ("moose",)
    carry_verbs: tuple[str, ...] = DEFAULT_CARRY_VERBS
    _extra: dict = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LintKit":
        def tup(key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
            value = data.get(key, default)
            if isinstance(value, str) or not isinstance(value, (list, tuple)):
                raise PromptLintError(f"Lint kit field {key!r} must be a list of strings")
            return tuple(str(v) for v in value)

        for required in ("count_template", "realtime_pattern", "locked_camera_clause"):
            if not isinstance(data.get(required), str) or not data[required].strip():
                raise PromptLintError(f"Lint kit is missing required field {required!r}")
        if "{n}" not in data["count_template"]:
            raise PromptLintError("Lint kit count_template must contain '{n}'")
        try:
            re.compile(data["realtime_pattern"])
        except re.error as e:
            raise PromptLintError(f"Lint kit realtime_pattern is not a valid regex: {e}") from e
        return cls(
            count_template=data["count_template"],
            realtime_pattern=data["realtime_pattern"],
            locked_camera_clause=data["locked_camera_clause"],
            antler_clause=str(data.get("antler_clause", "")),
            antler_characters=tup("antler_characters"),
            duck_clause=str(data.get("duck_clause", "")),
            duck_characters=tup("duck_characters"),
            banned_decor=tup("banned_decor"),
            forbidden_phrases=tup("forbidden_phrases", DEFAULT_FORBIDDEN_PHRASES),
            moose_names=tup("moose_names", ("moose",)),
            carry_verbs=tup("carry_verbs", DEFAULT_CARRY_VERBS),
        )


def _contains(prompt: str, clause: str) -> bool:
    return clause.strip().lower() in prompt.lower()


def lint_violations(prompt: str, shot_meta: dict[str, Any], kit: LintKit) -> list[str]:
    """Return every violation (empty list when the prompt is clean)."""
    problems: list[str] = []
    meta = shot_meta or {}

    # 1. Count clause with N == shot_meta.count
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        problems.append("shot_meta.count must be a non-negative integer")
    else:
        pattern = re.escape(kit.count_template.strip()).replace(re.escape("{n}"), r"(\d+)")
        found = [int(m) for m in re.findall(pattern, prompt, flags=re.IGNORECASE)]
        if not found:
            problems.append(f"missing count clause: {kit.count_template.replace('{n}', str(count))!r}")
        elif any(n != count for n in found):
            problems.append(f"count clause mismatch: prompt says {found}, shot_meta.count is {count}")

    # 2. Locked camera clause on exterior / water shots
    if (meta.get("exterior") or meta.get("water")) and not _contains(prompt, kit.locked_camera_clause):
        problems.append(f"missing locked-camera clause on exterior/water shot: {kit.locked_camera_clause!r}")

    # 3. Real-time / no slow motion clause
    if not re.search(kit.realtime_pattern, prompt, flags=re.IGNORECASE | re.DOTALL):
        problems.append("missing real-time / no-slow-motion clause")

    # 4. Forbidden phrases
    lowered = prompt.lower()
    for phrase in kit.forbidden_phrases:
        if phrase.lower() in lowered:
            problems.append(f"forbidden phrase: {phrase!r}")
    if re.search(r"\bwaves\b", prompt, flags=re.IGNORECASE):
        problems.append("bare 'waves' is forbidden (only 'ONE small wave')")

    # 5. Carry/hold verbs for a moose
    verbs = "|".join(re.escape(v) for v in kit.carry_verbs)
    for sentence in re.split(r"[.!?\n]+", prompt):
        has_moose = any(re.search(rf"\b{re.escape(n)}\b", sentence, re.IGNORECASE) for n in kit.moose_names)
        if has_moose and verbs and re.search(rf"\b(?:{verbs})\b", sentence, re.IGNORECASE):
            problems.append(f"carry/hold verb used with a moose: {sentence.strip()!r}")

    # 6. Banned decor list
    for word in kit.banned_decor:
        if word and re.search(rf"\b{re.escape(word)}\b", prompt, flags=re.IGNORECASE):
            problems.append(f"banned decor word: {word!r}")

    # 7. Antler clause when an antler character is present; 8. duck clause when duck present
    present = {str(c).lower() for c in (meta.get("characters") or [])}
    if present & {c.lower() for c in kit.antler_characters}:
        if not kit.antler_clause.strip():
            problems.append("lint kit has no antler_clause but an antler character is present")
        elif not _contains(prompt, kit.antler_clause):
            problems.append(f"missing antler clause: {kit.antler_clause!r}")
    if present & {c.lower() for c in kit.duck_characters}:
        if not kit.duck_clause.strip():
            problems.append("lint kit has no duck_clause but the duck is present")
        elif not _contains(prompt, kit.duck_clause):
            problems.append(f"missing duck clause: {kit.duck_clause!r}")
    return problems


def lint_prompt(prompt: str, shot_meta: dict[str, Any], kit: LintKit) -> None:
    """Raise PromptLintError if the prompt violates any rule."""
    problems = lint_violations(prompt, shot_meta, kit)
    if problems:
        raise PromptLintError("Prompt lint failed: " + "; ".join(problems))


def load_lint_kit(episode_id: str) -> LintKit:
    """Load the lint kit: PROMPT_LINT_KIT_PATH file, else <EPISODES_DIR>/<episode>/prompt_kit.json 'lint'."""
    override = os.getenv("PROMPT_LINT_KIT_PATH", "").strip()
    if override:
        path = Path(override)
    else:
        path = Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id / "prompt_kit.json"
    if not path.is_file():
        raise PromptLintError(f"No prompt lint kit found for {episode_id} (looked at {path})")
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise PromptLintError(f"Cannot read prompt lint kit {path}: {e}") from e
    section = data if override else data.get("lint")
    if not isinstance(section, dict):
        raise PromptLintError(f"Prompt lint kit {path} has no 'lint' section")
    return LintKit.from_dict(section)


def enforce_prompt_lint(
    episode_id: str, prompt: str, shot_meta: dict[str, Any] | None, live: bool
) -> list[str]:
    """The gate used before every paid submit.

    Live: raises PromptLintError on any violation, missing kit or missing shot_meta (fail closed).
    Dry: logs warnings and returns the problems; never raises.
    """
    try:
        if shot_meta is None:
            raise PromptLintError("shot_meta is required for prompt lint")
        kit = load_lint_kit(episode_id)
        problems = lint_violations(prompt, shot_meta, kit)
    except PromptLintError as e:
        problems = [str(e)]
    if problems and live:
        raise PromptLintError("Prompt lint failed: " + "; ".join(problems))
    for problem in problems:
        logger.warning("[DRY-RUN] prompt lint warning for %s: %s", episode_id, problem)
    return problems
