"""Prompt lint gate: a code gate that runs BEFORE estimate / reserve / provider call.

``lint_prompt(prompt, shot_meta, kit)`` raises PromptLintError listing every violation. The rules are
the show's (hfvg.show_profile.LintRules; a LintKit is a view over them): the count clause, the
locked-camera clause, the real-time pattern, forbidden phrases and banned words, the clauses a shot
must carry (``required_clauses``), the words a prompt may not use near a subject (``forbid_near``) and
the continuity locks the submit injects. Every clause text and message is show data; none is
hardcoded here.

Where an episode's rules come from (:func:`resolve_lint_kit`): its ``show.json`` lint; else the file
named by ``PROMPT_LINT_KIT_PATH``; else the ``HFVG_SHOW_PROFILE_PATH`` profile's lint; else the
episode's ``prompt_kit.json``, read by the legacy adapter (hfvg.compat.legacy_show) exactly as before:
a ``"lint"`` object is the kit's own rules, no ``"lint"`` key or ``"lint": "default"`` the built-in
default profile (:func:`default_lint_kit`: content-policy terms and the motion rules, no series clauses).

shot_meta keys: ``count`` (int), ``exterior`` / ``water`` (bool), ``characters`` (list[str]), the
show's shot flags (``flags.<name>``, or top-level for the legacy layout), optional ``cast`` /
``text_policy`` (planned by hfvg.cast_plan; still prompts only).

Live mode: any violation (or missing kit / shot_meta) refuses. Dry mode: warn only.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from hfvg.cast_prompt import cast_lint_violations, inject_cast_prompt, props_clip_lock
from hfvg.compat import legacy_show
from hfvg.compat.legacy_show import is_bath_shot  # noqa: F401  (re-exported)
from hfvg.content_policy import hard_stop_terms, precheck_banned_words
from hfvg.paths import episode_dir
from hfvg.show_profile import (
    ForbidNear,
    LintRules,
    ShowProfileError,
    declared_source,
    evaluate_when,
    resolve_show_profile,
)

logger = logging.getLogger(__name__)

DEFAULT_FORBIDDEN_PHRASES = ("nothing else moves",)
DEFAULT_LINT_PROFILE = "default"
SHOW_LINT_PROFILE = "show"


class PromptLintError(ValueError):
    """The prompt violates the lint rules (refused before any paid call)."""


@dataclass(frozen=True)
class LintKit:
    """The lint rules one episode's prompts are checked against. ``profile``: ``"kit"`` (a
    prompt_kit.json ``"lint"`` object), ``"default"`` (:func:`default_lint_kit`) or ``"show"`` (a show
    profile's lint)."""
    rules: LintRules = field(default_factory=LintRules)
    profile: str = "kit"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LintKit":
        """A prompt_kit.json ``"lint"`` object, compiled into rules by the legacy adapter (every key and
        message the kits were written with keeps its meaning)."""
        return cls(rules=legacy_show.compile_kit(data), profile="kit")

    @property
    def count_template(self) -> str:
        return self.rules.count_template

    @property
    def realtime_pattern(self) -> str:
        return self.rules.realtime_pattern

    @property
    def locked_camera_clause(self) -> str:
        return self.rules.locked_camera_clause

    @property
    def forbidden_phrases(self) -> tuple[str, ...]:
        return self.rules.forbidden_phrases

    @property
    def continuity_locks(self) -> tuple:
        return self.rules.continuity_locks


def default_lint_kit(continuity_locks: Any = None) -> LintKit:
    """The built-in default profile, for kits written without lint rules of their own (free-form prompts):
    the content-policy terms (docs/CONTENT-POLICY.md hard-stop terms plus the still precheck's banned
    words), "nothing else moves", the clip motion rules and the kit's continuity locks (GAP-40), with
    the legacy adapter's flag clauses and style split. No count, real-time or locked-camera clause."""
    return LintKit(rules=legacy_show.default_lint_rules(continuity_locks), profile=DEFAULT_LINT_PROFILE)


def applicable_locks(shot_meta: dict[str, Any] | None, kit: LintKit, kind: str) -> list[dict]:
    """The continuity locks that apply to this shot and prompt kind, in kit order; then, for the clip
    of a shot with props or a place, the shot's own lock naming them (hfvg.cast_prompt.props_clip_lock)."""
    out = [dict(lock) for lock in kit.rules.continuity_locks
           if kind in lock.kinds and evaluate_when(lock.when, shot_meta)]
    shot_lock = props_clip_lock(shot_meta)
    if shot_lock and kind in shot_lock["kinds"]:
        out.append(shot_lock)
    return out


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def inject_continuity_locks(prompt: str, shot_meta: dict[str, Any] | None, kit: LintKit,
                            kind: str = "still") -> tuple[str, list[str]]:
    """GAP-40: (prompt with every applicable lock appended once, ids appended). Idempotent: a lock whose
    text is already in the prompt (whitespace/case-insensitive) is not added again. Pure."""
    out = prompt.rstrip()
    added = []
    for lock in applicable_locks(shot_meta, kit, kind):
        if _norm(lock["text"]) in _norm(out):
            continue
        out = f"{out} {lock['text']}"
        added.append(lock["id"])
    return out, added


def _contains(prompt: str, clause: str) -> bool:
    return clause.strip().lower() in prompt.lower()


# ------------------------------------------------------------------------------------------ rules

_UNIT_SPLIT = {"sentence": r"[.!?\n]+", "clause": r"[.!?;\n]+"}


def _units(prompt: str, scope: str) -> list[str]:
    return [prompt] if scope == "prompt" else re.split(_UNIT_SPLIT[scope], prompt)


def _finder(match: str):
    """(word, text) -> found? for one ``forbid_near`` match mode."""
    if match == "letters":
        return lambda w, text: re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", text.lower()) is not None
    if match == "substring":
        return lambda w, text: bool(w.strip()) and w.lower() in text.lower()
    return lambda w, text: re.search(rf"\b{re.escape(w)}\b", text, re.IGNORECASE) is not None


def near_violations(rule: ForbidNear, prompt: str) -> list[str]:
    """One ``forbid_near`` rule over the prompt: per unit (sentence, clause or the whole prompt) that
    names a subject (any unit when the rule has none), the words found in it; one message per unit
    (the first word) or per word."""
    found = _finder(rule.match)
    problems = []
    for unit in _units(prompt, rule.scope):
        subject = next((s for s in rule.subjects if found(s, unit)), None)
        if rule.subjects and subject is None:
            continue
        words = [w for w in rule.words if found(w, unit)]
        if rule.report == "unit":
            words = words[:1]
        quote = unit.strip()[:rule.max_quote] if rule.max_quote else unit.strip()
        problems += [rule.message.format(word=w, subject=subject or "", sentence=quote) for w in words]
    return problems


def _clause_violations(prompt: str, meta: dict, rules: LintRules, kind: str) -> list[str]:
    problems = []
    for clause in rules.required_clauses:
        if kind not in clause.kinds or not evaluate_when(clause.when, meta):
            continue
        if not clause.text.strip():
            problems.append((clause.missing_text_message or "show lint has no text for required clause {id!r}")
                            .format(id=clause.id, text=clause.text))
        elif not _contains(prompt, clause.text):
            problems.append(clause.message.format(id=clause.id, text=clause.text))
    return problems


def _near_violations(prompt: str, meta: dict, rules: LintRules, kind: str, after: str) -> list[str]:
    return [p for rule in rules.forbid_near
            if rule.after == after and kind in rule.kinds and evaluate_when(rule.when, meta)
            for p in near_violations(rule, prompt)]


def lint_violations(prompt: str, shot_meta: dict[str, Any], kit: LintKit, kind: str = "still") -> list[str]:
    """Return every violation (empty list when the prompt is clean)."""
    problems: list[str] = []
    meta = shot_meta or {}
    rules = kit.rules

    # Count clause with N == shot_meta.count
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        problems.append("shot_meta.count must be a non-negative integer")
    elif rules.count_template:
        pattern = re.escape(rules.count_template.strip()).replace(re.escape("{n}"), r"(\d+)")
        found = [int(m) for m in re.findall(pattern, prompt, flags=re.IGNORECASE)]
        if not found:
            if rules.count_required:
                problems.append(f"missing count clause: {rules.count_template.replace('{n}', str(count))!r}")
        elif any(n != count for n in found):
            problems.append(f"count clause mismatch: prompt says {found}, shot_meta.count is {count}")

    # Locked camera clause on exterior / water shots
    if (rules.locked_camera_clause and (meta.get("exterior") or meta.get("water"))
            and not _contains(prompt, rules.locked_camera_clause)):
        problems.append(f"missing locked-camera clause on exterior/water shot: {rules.locked_camera_clause!r}")

    # Real-time / no slow motion clause
    if rules.realtime_pattern and not re.search(rules.realtime_pattern, prompt, flags=re.IGNORECASE | re.DOTALL):
        problems.append("missing real-time / no-slow-motion clause")

    # Forbidden phrases
    lowered = prompt.lower()
    for phrase in rules.forbidden_phrases:
        if phrase.lower() in lowered:
            problems.append(f"forbidden phrase: {phrase!r}")
    if rules.forbid_bare_waves and re.search(r"\bwaves\b", prompt, flags=re.IGNORECASE):
        problems.append("bare 'waves' is forbidden (only 'ONE small wave')")
    problems.extend(_near_violations(prompt, meta, rules, kind, "forbidden_phrases"))

    # Banned words
    for word in rules.banned_words:
        if word and re.search(rf"\b{re.escape(word)}\b", prompt, flags=re.IGNORECASE):
            problems.append(rules.banned_words_message.format(word=word))
    # Content-policy terms, each read the way its own check reads it: hard-stop terms as whole words
    # (moderation ladder), banned words as substrings (still precheck)
    if rules.content_policy_terms:
        hard = hard_stop_terms(prompt)
        for term in hard:
            problems.append(f"content-policy hard-stop term: {term!r} (docs/CONTENT-POLICY.md)")
        for word in precheck_banned_words(prompt):
            if word not in hard:
                problems.append(f"banned word {word!r}: the live still precheck refuses it (docs/CONTENT-POLICY.md)")

    # The clauses this shot must carry, then the near-word rules that follow them
    problems.extend(_clause_violations(prompt, meta, rules, kind))
    problems.extend(_near_violations(prompt, meta, rules, kind, "required_clauses"))
    # GAP-40: every applicable continuity lock must be in the prompt (the submit injects them)
    for lock in applicable_locks(meta, kit, kind):
        if _norm(lock["text"]) not in _norm(prompt):
            problems.append(f"missing continuity lock {lock['id']!r} ({kind})")
    problems.extend(_near_violations(prompt, meta, rules, kind, "continuity_locks"))
    # Cast (still prompts): each character in frame carries its locked description, each picked
    # prop and place its description and, under the no_text policy, the no-text constraint with no
    # request for on-image words (the submit injects them; hfvg.cast_prompt). Shots without
    # shot_meta cast / props / text_policy: no-op.
    if kind == "still":
        problems.extend(cast_lint_violations(prompt, meta))
    return problems


def ref_edit_violations(prompt: str, lock_meta: dict[str, Any] | None, kit: LintKit) -> list[str]:
    """The show's rules for ref-edit prompts (kind ``ref_edit``): its required clauses and near-word rules."""
    meta = lock_meta or {}
    return (_near_violations(prompt, meta, kit.rules, "ref_edit", "forbidden_phrases")
            + _clause_violations(prompt, meta, kit.rules, "ref_edit")
            + _near_violations(prompt, meta, kit.rules, "ref_edit", "required_clauses")
            + _near_violations(prompt, meta, kit.rules, "ref_edit", "continuity_locks"))


# Clip (motion) prompt rules on top of the shared kit rules: a clip prompt must not freeze the scene
# (named ambient motion instead) - HARNESS-GATES v1.1 "never end a video prompt with nothing else moves".
CLIP_STATIC_PATTERNS = (
    r"nothing\s+(?:else\s+)?moves",
    r"everything\s+else\s+(?:is\s+)?(?:still|frozen|static)",
    r"all\s+else\s+(?:is\s+)?(?:still|frozen|static)",
    r"rest\s+(?:of\s+the\s+scene\s+)?(?:is\s+)?(?:frozen|still|static)",
    r"\bfreeze[- ]?frame\b",
    r"\bstill\s+image\b",
)


def clip_lint_violations(prompt: str, shot_meta: dict[str, Any], kit: LintKit) -> list[str]:
    """Violations of the CLIP (motion) prompt: every kit rule plus the motion rules."""
    problems = lint_violations(prompt, shot_meta, kit, kind="clip")
    for pattern in CLIP_STATIC_PATTERNS:
        m = re.search(pattern, prompt, flags=re.IGNORECASE)
        if m:
            problems.append(f"clip prompt freezes the scene ({m.group(0)!r}); name the ambient motion instead")
    return problems


def lint_prompt(prompt: str, shot_meta: dict[str, Any], kit: LintKit) -> None:
    """Raise PromptLintError if the prompt violates any rule."""
    problems = lint_violations(prompt, shot_meta, kit)
    if problems:
        raise PromptLintError("Prompt lint failed: " + "; ".join(problems))


# ------------------------------------------------------------------------------------------ resolution


def resolve_lint_kit(episode_id: str) -> tuple[LintKit, str | None]:
    """(kit, notice): the episode's lint kit, and a notice when the built-in default profile
    applies only because the per-episode prompt_kit.json has no "lint" key (None otherwise).

    The episode's show.json lint; else the PROMPT_LINT_KIT_PATH file (a bare lint section or a full
    kit); else the HFVG_SHOW_PROFILE_PATH profile's lint; else <EPISODES_DIR>/<episode>/prompt_kit.json.
    A kit's "lint" object is its own rules; ``"lint": "default"`` (or, per-episode, no "lint" key) is
    :func:`default_lint_kit`. Raises PromptLintError for a missing or unreadable kit, any other "lint"
    value, an override with no lint rules, or a show profile that does not load.
    """
    declared = declared_source(episode_id)
    override = os.getenv("PROMPT_LINT_KIT_PATH", "").strip()
    if declared == "episode" or (declared == "env" and not override):
        return _show_kit(episode_id), None
    if override:
        path = Path(override)
    else:
        path = episode_dir(episode_id) / "prompt_kit.json"
    if not path.is_file():
        raise PromptLintError(f"No prompt lint kit found for {episode_id} (looked at {path})")
    data = _read_kit(path)
    # A full prompt_kit.json (with a "lint" section) may be given as the override; a bare lint
    # section file is still accepted.
    section = data.get("lint") if (not override or isinstance(data.get("lint"), dict)) else data
    if override and section is data and isinstance(data, dict) and "count_template" not in data:
        section = None  # a full prompt_kit.json without a lint section, not a bare lint file
    if not isinstance(section, dict):
        return _default_profile(path, data, bool(override))
    if section is not data and "continuity_locks" not in section and "continuity_locks" in data:
        # GAP-40: the kit's top-level continuity_locks (CONTINUITY section) apply to the lint too
        section = {**section, "continuity_locks": data["continuity_locks"]}
    return LintKit.from_dict(section), None


def _read_kit(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PromptLintError(f"Cannot read prompt lint kit {path}: {e}") from e
    if not isinstance(data, dict):
        raise PromptLintError(f"Prompt lint kit {path} must be a JSON object")
    return data


def _show_kit(episode_id: str) -> LintKit:
    """The lint of the episode's show profile, with the top-level continuity locks of its
    prompt_kit.json (when it has one). A ``"lint"`` object in that kit is refused: the show profile
    holds the episode's lint rules, and two rule sets would leave one of them unread."""
    try:
        profile = resolve_show_profile(episode_id)
    except ShowProfileError as e:
        raise PromptLintError(f"Show profile does not load: {e}") from e
    rules = profile.lint
    path = episode_dir(episode_id) / "prompt_kit.json"
    if path.is_file():
        data = _read_kit(path)
        if isinstance(data.get("lint"), dict):
            raise PromptLintError(
                f"Prompt lint kit {path} has its own \"lint\" rules but the episode's lint comes from its show "
                f"profile ({profile.path}): move them into the profile's \"lint\" (docs/SHOW-PROFILE.md)")
        if data.get("lint", DEFAULT_LINT_PROFILE) != DEFAULT_LINT_PROFILE:
            raise PromptLintError(f"Prompt lint kit {path}: \"lint\" must be an object or \"{DEFAULT_LINT_PROFILE}\", "
                                  f"got {json.dumps(data['lint'])}")
        locks = legacy_show.build_locks(legacy_show.parse_locks(data.get("continuity_locks", [])))
        clash = sorted({lk.id for lk in locks} & {lk.id for lk in rules.continuity_locks})
        if clash:
            raise PromptLintError(f"Continuity lock ids {clash} are in both {path} and the show profile")
        rules = replace(rules, continuity_locks=rules.continuity_locks + locks)
    return LintKit(rules=rules, profile=SHOW_LINT_PROFILE)


def _default_profile(path: Path, data: dict, override: bool) -> tuple[LintKit, str | None]:
    """A kit without a "lint" object: the default profile when it asks for it (or, per-episode,
    has no "lint" key at all), else the error the kit has always raised."""
    if "lint" in data and data["lint"] != DEFAULT_LINT_PROFILE:
        raise PromptLintError(f"Prompt lint kit {path}: \"lint\" must be an object (the kit's own rules) or "
                              f"\"{DEFAULT_LINT_PROFILE}\" (the built-in default profile), got {json.dumps(data['lint'])}")
    if "lint" not in data and override:
        raise PromptLintError(f"Prompt lint kit {path} has no 'lint' section")
    notice = None if "lint" in data else (
        f"Prompt lint kit {path} has no 'lint' section: the built-in default lint profile applies "
        "(content-policy terms and motion rules, no series clauses)")
    # GAP-40: the kit's top-level continuity locks apply to the default profile too
    return default_lint_kit(data.get("continuity_locks")), notice


def load_lint_kit(episode_id: str) -> LintKit:
    """The episode's lint kit (:func:`resolve_lint_kit` without the notice)."""
    return resolve_lint_kit(episode_id)[0]


def lint_kit_problems(episode_id: str) -> list[str]:
    """Why the lint kit the paid submits would use cannot load ([] when it loads): checked by the
    live preflight and live starts so a broken lint config refuses before any spend."""
    try:
        resolve_lint_kit(episode_id)
    except PromptLintError as e:
        return [f"prompt lint kit: {e}"]
    return []


def apply_continuity_locks(episode_id: str, prompt: str, shot_meta: dict[str, Any] | None,
                           live: bool, kind: str = "still") -> tuple[str, list[str]]:
    """GAP-40: the submit-side injection (before lint). Live: a missing/unreadable kit raises (the lint
    would refuse anyway). Dry: returns the prompt unchanged on a kit problem."""
    try:
        kit = load_lint_kit(episode_id)
    except PromptLintError:
        if live:
            raise
        return prompt, []
    return inject_continuity_locks(prompt, shot_meta, kit, kind)


def lint_summary(episode_id: str, plans: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Offline lint of every prompt a live run would submit (G1 plan / preflight; no provider call).

    ``plans`` maps shot id -> plan with ``prompt``, ``clip_prompt``, ``shot_meta`` and
    ``pre_approved_still`` (``shot_data.parse_shots`` output, or the merged shot plans). A shot with
    a pre-approved start still submits no still, so only its clip prompt (clip_prompt, else the
    still prompt) is linted; otherwise both. Same kit, continuity-lock and cast injection and rules as
    the live submit gate (apply_continuity_locks + inject_cast_prompt + enforce_prompt_lint). JSON-safe result:
    ``{"ok", "profile": "kit" | "default" | "show" | None, "notice", "kit_error", "shots": {shot_id:
    [problem, ...]} (shots with problems only), "problem_count"}``.
    """
    try:
        kit, notice = resolve_lint_kit(episode_id)
    except PromptLintError as e:
        return {"ok": False, "profile": None, "notice": None, "kit_error": str(e), "shots": {},
                "problem_count": 0}
    shots: dict[str, list[str]] = {}
    for sid, plan in plans.items():
        meta = plan.get("shot_meta") or {}
        problems: list[str] = []
        if not plan.get("pre_approved_still"):
            still, _ = inject_continuity_locks(plan.get("prompt") or "", meta, kit, "still")
            still, _ = inject_cast_prompt(still, meta)
            problems += [f"still: {p}" for p in lint_violations(still, meta, kit)]
        clip, _ = inject_continuity_locks(plan.get("clip_prompt") or plan.get("prompt") or "", meta, kit, "clip")
        problems += [f"clip: {p}" for p in clip_lint_violations(clip, meta, kit)]
        if problems:
            shots[sid] = problems
    return {"ok": not shots, "profile": kit.profile, "notice": notice, "kit_error": None, "shots": shots,
            "problem_count": sum(len(p) for p in shots.values())}


def lint_report(episode_id: str, plans: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """:func:`lint_summary` as {shot_id: [problem, ...]} for shots with problems; a missing/broken
    kit, or the notice that the built-in default profile applies, is reported once under "*"."""
    summary = lint_summary(episode_id, plans)
    if summary["kit_error"]:
        return {"*": [summary["kit_error"]]}
    out: dict[str, list[str]] = {"*": [summary["notice"]]} if summary["notice"] else {}
    out.update(summary["shots"])
    return out


def enforce_prompt_lint(
    episode_id: str, prompt: str, shot_meta: dict[str, Any] | None, live: bool, kind: str = "still"
) -> list[str]:
    """The gate used before every paid submit. kind="clip" adds the motion-prompt rules.

    Live: raises PromptLintError on any violation, missing kit or missing shot_meta (fail closed).
    Dry: logs warnings and returns the problems; never raises.
    """
    try:
        if shot_meta is None:
            raise PromptLintError("shot_meta is required for prompt lint")
        kit = load_lint_kit(episode_id)
        check = clip_lint_violations if kind == "clip" else lint_violations
        problems = check(prompt, shot_meta, kit)
    except PromptLintError as e:
        problems = [str(e)]
    if problems and live:
        raise PromptLintError("Prompt lint failed: " + "; ".join(problems))
    for problem in problems:
        logger.warning("[DRY-RUN] prompt lint warning for %s: %s", episode_id, problem)
    return problems


def apply_ref_edit_locks(episode_id: str, prompt: str, lock_meta: dict[str, Any] | None,
                         live: bool) -> tuple[str, list[str]]:
    """GAP-42: a ref-edit prompt whose refs.json entry declares ``lock_meta`` (shot flags and characters,
    e.g. {"flags": {"on_camera": true}}; the legacy layout's top-level flags are read too) gets the
    kit's still continuity locks injected, and the show's ref-edit rules (:func:`ref_edit_violations`)
    are enforced. Live: a kit problem or a rule hit raises before any upload/estimate/spend. No
    lock_meta: the prompt is returned unchanged."""
    if not lock_meta:
        return prompt, []
    try:
        kit = load_lint_kit(episode_id)
    except PromptLintError:
        if live:
            raise
        return prompt, []
    out, added = inject_continuity_locks(prompt, lock_meta, kit, "still")
    problems = ref_edit_violations(out, lock_meta, kit)
    if problems and live:
        raise PromptLintError("Ref edit prompt lint failed: " + "; ".join(problems))
    for problem in problems:
        logger.warning("[DRY-RUN] ref edit lint warning for %s: %s", episode_id, problem)
    return out, added
