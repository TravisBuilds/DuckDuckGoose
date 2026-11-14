"""Prompt lint gate: a code gate that runs BEFORE estimate / reserve / provider call.

``lint_prompt(prompt, shot_meta, kit)`` raises PromptLintError listing every violation.
Clause texts (count clause, locked-camera clause, real-time pattern, antler/duck clauses, banned
decor list) come from a LintKit loaded from the per-episode ``prompt_kit.json`` (``"lint"`` section)
or from the file named by ``PROMPT_LINT_KIT_PATH``; none is hardcoded here.

A per-episode kit with no ``"lint"`` key (e.g. a draft-from-idea kit) or with ``"lint": "default"``
gets the built-in default profile instead (:func:`default_lint_kit`): content-policy terms and the
motion rules, no series clauses. A ``"lint"`` object is parsed exactly as before.

shot_meta keys: ``count`` (int), ``exterior`` / ``water`` (bool), ``characters`` (list[str]),
optional ``antler`` / ``duck`` flags (clause required whatever the character codes say), optional
``cast`` / ``text_policy`` (planned by hfvg.cast_plan; still prompts only, rule 13).

Live mode: any violation (or missing kit / shot_meta) refuses. Dry mode: warn only.
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hfvg.cast_prompt import cast_lint_violations, inject_cast_prompt
from hfvg.content_policy import hard_stop_terms, precheck_banned_words
from hfvg.paths import episode_dir

logger = logging.getLogger(__name__)

DEFAULT_FORBIDDEN_PHRASES = ("nothing else moves", "realistic wild")
DEFAULT_CARRY_VERBS = (
    "carry", "carries", "carrying", "carried", "hold", "holds", "holding", "held",
)

# Built-in default profile: only rules that hold for any episode. "realistic wild", bare "waves"
# and the moose carry/hold rule are series style rules (and "realistic wild" matches "photorealistic
# wildlife"), so they stay with the kits that ask for them.
DEFAULT_LINT_PROFILE = "default"
DEFAULT_PROFILE_FORBIDDEN_PHRASES = ("nothing else moves",)


class PromptLintError(ValueError):
    """The prompt violates the lint rules (refused before any paid call)."""


@dataclass(frozen=True)
class LintKit:
    # The three clause rules are required in a kit's own "lint" object and empty (off) only in
    # the built-in default profile.
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
    # Bath rule (optional): on a bath shot (animals soaking: shot_meta wet + water + count > 0,
    # or an explicit shot_meta.bath flag) the prompt must carry bath_clause (e.g. "submerged up to
    # the shoulders, visible waterline, ...") and none of bath_forbidden ("on top of the water").
    bath_clause: str = ""
    bath_forbidden: tuple[str, ...] = ()
    # GAP-40: continuity locks the harness INJECTS into every matching prompt (still and/or clip)
    # before lint, and lint then requires (fail closed if an injection was skipped). Each lock is
    # {"id", "when": "duck" | "animals" | "always", "kinds": ["still", "clip"], "text"}.
    continuity_locks: tuple = ()
    animal_characters: tuple[str, ...] = ()
    # GAP-40: style split - only the duck is plush-stylised, so a sentence that names the duck may
    # never also carry a realism word (realistic / photoreal / lifelike ...).
    duck_names: tuple[str, ...] = ()
    duck_realism_words: tuple[str, ...] = ()
    # Built-in default profile only (a kit's "lint" object never sets these): the content-policy
    # terms (hfvg.content_policy) refused anywhere in the prompt, and the series bare-"waves" rule off.
    content_policy_terms: bool = False
    forbid_bare_waves: bool = True
    profile: str = "kit"  # "kit": the kit's own "lint" rules; "default": default_lint_kit()
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
            bath_clause=str(data.get("bath_clause", "")),
            bath_forbidden=tup("bath_forbidden"),
            continuity_locks=parse_continuity_locks(data.get("continuity_locks", [])),
            animal_characters=tup("animal_characters"),
            duck_names=tup("duck_names"),
            duck_realism_words=tup("duck_realism_words"),
        )


LOCK_WHEN = ("duck", "animals", "always")
LOCK_KINDS = ("still", "clip")
DEFAULT_DUCK_NAMES = ("duck",)
DEFAULT_DUCK_REALISM_WORDS = ("realistic", "photoreal", "photorealistic", "photo-real", "photo-realistic",
                              "lifelike", "life-like", "hyperreal", "hyper-real", "hyperrealistic")


def parse_continuity_locks(raw: Any) -> tuple:
    """Validate the kit's continuity_locks list (GAP-40). Returns a tuple of frozen dict items."""
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise PromptLintError("Lint kit field 'continuity_locks' must be a list")
    out, seen = [], set()
    for i, lock in enumerate(raw):
        if not isinstance(lock, dict):
            raise PromptLintError(f"continuity_locks[{i}] must be an object")
        lid = str(lock.get("id") or "").strip()
        text = " ".join(str(lock.get("text") or "").split())
        when = lock.get("when")
        kinds = lock.get("kinds", list(LOCK_KINDS))
        if not lid or lid in seen:
            raise PromptLintError(f"continuity_locks[{i}] needs a unique non-empty id")
        if not text:
            raise PromptLintError(f"continuity lock {lid!r} has no text")
        if when not in LOCK_WHEN:
            raise PromptLintError(f"continuity lock {lid!r}: when must be one of {LOCK_WHEN}")
        if not isinstance(kinds, list) or not kinds or any(k not in LOCK_KINDS for k in kinds):
            raise PromptLintError(f"continuity lock {lid!r}: kinds must be a non-empty subset of {LOCK_KINDS}")
        seen.add(lid)
        out.append((("id", lid), ("when", when), ("kinds", tuple(kinds)), ("text", text)))
    return tuple(out)


def default_lint_kit(continuity_locks: Any = None) -> LintKit:
    """The built-in default profile, for kits written without series lint rules (free-form prompts).

    Kept: the content-policy terms (docs/CONTENT-POLICY.md hard-stop terms plus the still precheck's
    banned words), "nothing else moves", the clip motion rules, the kit's continuity locks
    (GAP-40), the duck realism split and the fail-closed antler/duck shot_meta flags. Off: every
    series clause (count, real-time, locked-camera, antler/duck/bath) and the series style rules.
    """
    return LintKit(
        count_template="",
        realtime_pattern="",
        locked_camera_clause="",
        forbidden_phrases=DEFAULT_PROFILE_FORBIDDEN_PHRASES,
        moose_names=(),
        content_policy_terms=True,
        forbid_bare_waves=False,
        continuity_locks=parse_continuity_locks(continuity_locks),
        profile=DEFAULT_LINT_PROFILE,
    )


def _lock(item) -> dict:
    return dict(item)


def duck_present(shot_meta: dict[str, Any] | None, kit: "LintKit") -> bool:
    meta = shot_meta or {}
    present = {str(c).lower() for c in (meta.get("characters") or [])}
    return bool(meta.get("duck")) or bool(present & {c.lower() for c in kit.duck_characters})


def animals_present(shot_meta: dict[str, Any] | None, kit: "LintKit") -> bool:
    meta = shot_meta or {}
    count = meta.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return True
    present = {str(c).lower() for c in (meta.get("characters") or [])}
    return bool(present & {c.lower() for c in kit.animal_characters})


def applicable_locks(shot_meta: dict[str, Any] | None, kit: "LintKit", kind: str) -> list[dict]:
    """The continuity locks that apply to this shot and prompt kind, in kit order."""
    out = []
    for item in kit.continuity_locks:
        lock = _lock(item)
        if kind not in lock["kinds"]:
            continue
        when = lock["when"]
        if when == "always" or (when == "duck" and duck_present(shot_meta, kit)) or (
                when == "animals" and animals_present(shot_meta, kit)):
            out.append(lock)
    return out


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def inject_continuity_locks(prompt: str, shot_meta: dict[str, Any] | None, kit: "LintKit",
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


def duck_realism_violations(prompt: str, shot_meta: dict[str, Any] | None, kit: "LintKit") -> list[str]:
    """GAP-40 style split: on a duck shot no sentence that names the duck may carry a realism word."""
    if not duck_present(shot_meta, kit):
        return []
    names = tuple(n.lower() for n in (kit.duck_names or DEFAULT_DUCK_NAMES))
    words = tuple(w.lower() for w in (kit.duck_realism_words or DEFAULT_DUCK_REALISM_WORDS))
    problems = []
    for sentence in re.split(r"[.!?;\n]+", prompt):
        low = sentence.lower()
        if not any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", low) for n in names):
            continue
        for w in words:
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", low):
                problems.append(f"realism word {w!r} attached to the duck (only the duck is plush-stylised): "
                                f"{sentence.strip()[:120]!r}")
                break
    return problems


def _contains(prompt: str, clause: str) -> bool:
    return clause.strip().lower() in prompt.lower()


def is_bath_shot(shot_meta: dict[str, Any] | None) -> bool:
    """Animals soaking in the water: explicit shot_meta.bath, else wet + water + count > 0."""
    meta = shot_meta or {}
    if "bath" in meta:
        return bool(meta["bath"])
    count = meta.get("count")
    has_animals = isinstance(count, int) and not isinstance(count, bool) and count > 0
    return bool(meta.get("wet") and meta.get("water") and has_animals)


def lint_violations(prompt: str, shot_meta: dict[str, Any], kit: LintKit, kind: str = "still") -> list[str]:
    """Return every violation (empty list when the prompt is clean)."""
    problems: list[str] = []
    meta = shot_meta or {}

    # 1. Count clause with N == shot_meta.count
    count = meta.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        problems.append("shot_meta.count must be a non-negative integer")
    elif kit.count_template:
        pattern = re.escape(kit.count_template.strip()).replace(re.escape("{n}"), r"(\d+)")
        found = [int(m) for m in re.findall(pattern, prompt, flags=re.IGNORECASE)]
        if not found:
            problems.append(f"missing count clause: {kit.count_template.replace('{n}', str(count))!r}")
        elif any(n != count for n in found):
            problems.append(f"count clause mismatch: prompt says {found}, shot_meta.count is {count}")

    # 2. Locked camera clause on exterior / water shots
    if (kit.locked_camera_clause and (meta.get("exterior") or meta.get("water"))
            and not _contains(prompt, kit.locked_camera_clause)):
        problems.append(f"missing locked-camera clause on exterior/water shot: {kit.locked_camera_clause!r}")

    # 3. Real-time / no slow motion clause
    if kit.realtime_pattern and not re.search(kit.realtime_pattern, prompt, flags=re.IGNORECASE | re.DOTALL):
        problems.append("missing real-time / no-slow-motion clause")

    # 4. Forbidden phrases
    lowered = prompt.lower()
    for phrase in kit.forbidden_phrases:
        if phrase.lower() in lowered:
            problems.append(f"forbidden phrase: {phrase!r}")
    if kit.forbid_bare_waves and re.search(r"\bwaves\b", prompt, flags=re.IGNORECASE):
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
    # 6b. Content-policy terms (built-in default profile), each read the way its own check reads
    # it: hard-stop terms as whole words (moderation ladder), banned words as substrings (still precheck)
    if kit.content_policy_terms:
        hard = hard_stop_terms(prompt)
        for term in hard:
            problems.append(f"content-policy hard-stop term: {term!r} (docs/CONTENT-POLICY.md)")
        for word in precheck_banned_words(prompt):
            if word not in hard:
                problems.append(f"banned word {word!r}: the live still precheck refuses it (docs/CONTENT-POLICY.md)")

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
    # 9. Explicit shot_meta flags (antler / duck) require the clause even if no character code matched
    for flag, clause, codes, label in (
        ("antler", kit.antler_clause, kit.antler_characters, "antler"),
        ("duck", kit.duck_clause, kit.duck_characters, "duck"),
    ):
        if meta.get(flag) and not (present & {c.lower() for c in codes}):
            if not clause.strip():
                problems.append(f"lint kit has no {label}_clause but shot_meta.{flag} is set")
            elif not _contains(prompt, clause):
                problems.append(f"missing {label} clause (shot_meta.{flag}): {clause!r}")
    # 10. Bath rule: soaking with a visible waterline, never standing/floating on top of the water
    if is_bath_shot(meta):
        if kit.bath_clause.strip() and not _contains(prompt, kit.bath_clause):
            problems.append(f"missing bath clause on bath shot: {kit.bath_clause!r}")
        for phrase in kit.bath_forbidden:
            if phrase.strip() and phrase.lower() in lowered:
                problems.append(f"bath shot prompt puts an animal on the water: {phrase!r}")
    # 11. GAP-40: every applicable continuity lock must be in the prompt (the submit injects them)
    for lock in applicable_locks(meta, kit, kind):
        if _norm(lock["text"]) not in _norm(prompt):
            problems.append(f"missing continuity lock {lock['id']!r} ({kind})")
    # 12. GAP-40: no realism word attached to the duck
    problems.extend(duck_realism_violations(prompt, meta, kit))
    # 13. Cast (still prompts): each character in frame carries its locked description and, under the
    # no_text policy, the no-text constraint with no request for on-image words (the submit injects
    # both; hfvg.cast_prompt). Shots without shot_meta cast / text_policy: no-op.
    if kind == "still":
        problems.extend(cast_lint_violations(prompt, meta))
    return problems


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


def resolve_lint_kit(episode_id: str) -> tuple[LintKit, str | None]:
    """(kit, notice): the episode's lint kit, and a notice when the built-in default profile
    applies only because the per-episode prompt_kit.json has no "lint" key (None otherwise).

    PROMPT_LINT_KIT_PATH file (a bare lint section or a full kit), else
    <EPISODES_DIR>/<episode>/prompt_kit.json. A "lint" object is the kit's own rules; ``"lint":
    "default"`` (or, per-episode, no "lint" key) is :func:`default_lint_kit`. Raises PromptLintError
    for a missing or unreadable kit, any other "lint" value, or an override with no lint rules.
    """
    override = os.getenv("PROMPT_LINT_KIT_PATH", "").strip()
    if override:
        path = Path(override)
    else:
        path = episode_dir(episode_id) / "prompt_kit.json"
    if not path.is_file():
        raise PromptLintError(f"No prompt lint kit found for {episode_id} (looked at {path})")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise PromptLintError(f"Cannot read prompt lint kit {path}: {e}") from e
    if not isinstance(data, dict):
        raise PromptLintError(f"Prompt lint kit {path} must be a JSON object")
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
    ``{"ok", "profile": "kit" | "default" | None, "notice", "kit_error", "shots": {shot_id:
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
    """GAP-42: a ref-edit prompt whose refs.json entry declares ``lock_meta`` (e.g. {"duck": true})
    gets the kit's still continuity locks injected, and the duck style split (no realism word in a
    sentence naming the duck) is enforced. Live: a kit problem or a realism hit raises before any
    upload/estimate/spend. No lock_meta: the prompt is returned unchanged."""
    if not lock_meta:
        return prompt, []
    try:
        kit = load_lint_kit(episode_id)
    except PromptLintError:
        if live:
            raise
        return prompt, []
    out, added = inject_continuity_locks(prompt, lock_meta, kit, "still")
    problems = duck_realism_violations(out, lock_meta, kit)
    if problems and live:
        raise PromptLintError("Ref edit prompt lint failed: " + "; ".join(problems))
    for problem in problems:
        logger.warning("[DRY-RUN] ref edit lint warning for %s: %s", episode_id, problem)
    return out, added
