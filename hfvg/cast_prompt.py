"""Cast prompt rules (pure, no IO): the shots.json ``cast`` request, the character clause every still
prompt carries for a character in frame, the no-text constraint, and the lint rule that requires
what the still submit injects.

A shot declares who is in frame with ``"cast": ["pip", {"character": "moss", "version": 2}]``: the
character id is the character's slug in the episode's cast library, the version defaults to the
character's default version (its first locked version, else version 1). Planning
(``hfvg.cast_plan``) turns that into identity refs plus ``shot_meta["cast"]`` (one entry per
character with its ``clause``) and ``shot_meta["text_policy"]``. The still submit appends the clauses
and, under the ``no_text`` policy, :data:`NO_TEXT_CLAUSE` (idempotent, like the continuity locks);
the prompt lint then requires them, so a skipped injection refuses live instead of drifting.

Shots without a cast and kits without a ``text_policy`` carry neither key: their prompts and lint
are exactly as before.
"""

import re
from typing import Any

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
MAX_CAST_PER_SHOT = 6
TEXT_POLICIES = ("no_text", "allow_text")
NO_TEXT_CLAUSE = "No text, no captions, no speech bubbles, no lettering."
CAST_LEAD_IN = "Characters in frame, each exactly as described and as in the reference images:"
CAST_KEY_PREFIX = "cast:"

# Phrases that ask for words drawn into the image. Under the no_text policy a prompt that asks for
# them contradicts the constraint the submit appends, so the lint names them (negated mentions such
# as "no captions" or "without lettering" are fine).
TEXT_REQUEST_PATTERNS = (
    r"speech[\s-]*bubbles?",
    r"thought[\s-]*bubbles?",
    r"word[\s-]*balloons?",
    r"\bcaptions?\b",
    r"\bsubtitles?\b",
    r"\blettering\b",
    r"\btypography\b",
    r"\b(?:says|saying|reads|reading|labeled|labelled|titled)\s*[:\"'\u201c\u2018]",
    r"\btext\s+(?:that\s+)?(?:says|reads)\b",
    r"\bwritten\s+(?:on|across)\b",
)


def slugify(name: str, fallback: str = "character") -> str:
    """A library id from a display name: lower-case letters, digits and dashes, at most 32 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")[:32].strip("-")
    return slug if slug and SLUG_RE.match(slug) else fallback


def is_cast_key(key: Any) -> bool:
    """A ref key planned from the cast library (resolved by hfvg.cast_plan, not by refs.json)."""
    return isinstance(key, str) and key.startswith(CAST_KEY_PREFIX)


def parse_cast_request(raw: Any, sid: str) -> list[dict]:
    """shots.json ``cast`` -> ``[{"character": slug, "version": int | None}]`` (raises ValueError)."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"shot {sid}: cast must be a list of character ids")
    if len(raw) > MAX_CAST_PER_SHOT:
        raise ValueError(f"shot {sid}: cast lists {len(raw)} characters; at most {MAX_CAST_PER_SHOT} per shot")
    out: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(raw):
        version = None
        if isinstance(entry, str):
            slug = entry.strip()
        elif isinstance(entry, dict):
            unknown = sorted(set(entry) - {"character", "version"})
            if unknown:
                raise ValueError(f"shot {sid}: cast[{i}] has unknown keys {unknown} (use character, version)")
            slug = entry.get("character")
            if not isinstance(slug, str):
                raise ValueError(f"shot {sid}: cast[{i}].character must be a character id string")
            slug = slug.strip()
            version = entry.get("version")
            if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
                raise ValueError(f"shot {sid}: cast[{i}].version must be a positive integer, got {version!r}")
        else:
            raise ValueError(f"shot {sid}: cast[{i}] must be a character id or {{\"character\", \"version\"}}")
        if not SLUG_RE.match(slug):
            raise ValueError(f"shot {sid}: cast[{i}] {slug!r} is not a character id "
                             "(lower-case letters, digits and dashes)")
        if slug in seen:
            raise ValueError(f"shot {sid}: character {slug!r} is listed twice in cast")
        seen.add(slug)
        out.append({"character": slug, "version": version})
    return out


def parse_text_policy(raw: Any, where: str) -> str | None:
    """A kit's ``text_policy``: None (absent), "no_text" or "allow_text" (raises ValueError)."""
    if raw is None:
        return None
    if raw not in TEXT_POLICIES:
        raise ValueError(f"{where}: text_policy must be one of {list(TEXT_POLICIES)}, got {raw!r}")
    return raw


def _one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


def character_clause(name: str, description: str, outfit: str = "") -> str:
    """The sentence a still prompt carries for one character in frame."""
    name = _one_line(name)
    description = _one_line(description).rstrip(".")
    outfit = _one_line(outfit).rstrip(".")
    text = f"{name}: {description}" if description else name
    if outfit:
        text += f"; outfit: {outfit}"
    return text + "."


def _norm(text: str) -> str:
    return " ".join(str(text or "").split()).lower()


def cast_entries(shot_meta: dict[str, Any] | None) -> list[dict]:
    """The planned in-frame characters that carry a clause (shot_meta["cast"])."""
    return [c for c in ((shot_meta or {}).get("cast") or [])
            if isinstance(c, dict) and _one_line(c.get("clause"))]


def no_text_required(shot_meta: dict[str, Any] | None) -> bool:
    return (shot_meta or {}).get("text_policy") == "no_text"


def inject_cast_prompt(prompt: str, shot_meta: dict[str, Any] | None) -> tuple[str, list[str]]:
    """(prompt with each in-frame character's clause and, under no_text, the no-text clause appended
    once; what was added). Idempotent: text already in the prompt (whitespace/case-insensitive) is
    not added again. No cast and no text policy: the prompt is returned unchanged."""
    out = (prompt or "").rstrip()
    added: list[str] = []
    missing = [c for c in cast_entries(shot_meta) if _norm(c["clause"]) not in _norm(out)]
    if missing:
        if _norm(CAST_LEAD_IN) not in _norm(out):
            out = f"{out} {CAST_LEAD_IN}"
        out = f"{out} " + " ".join(_one_line(c["clause"]) for c in missing)
        added += [f"cast:{c.get('character')}" for c in missing]
    if no_text_required(shot_meta) and _norm(NO_TEXT_CLAUSE) not in _norm(out):
        out = f"{out} {NO_TEXT_CLAUSE}"
        added.append("no_text")
    return (out.strip() if added else (prompt or "")), added


def text_requests(prompt: str) -> list[str]:
    """Phrases in the prompt that ask for words in the image (negated mentions excluded)."""
    text = re.sub(re.escape(NO_TEXT_CLAUSE), " ", prompt or "", flags=re.IGNORECASE)
    found: list[str] = []
    for pattern in TEXT_REQUEST_PATTERNS:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            before = text[max(0, m.start() - 24):m.start()].lower()
            words = re.findall(r"[a-z]+", before)[-3:]
            tail = " ".join(words)
            if any(w in words for w in ("no", "without", "never", "not", "avoid")) or "free of" in tail:
                continue
            phrase = m.group(0).strip()
            if phrase.lower() not in (f.lower() for f in found):
                found.append(phrase)
    return found


def cast_lint_violations(prompt: str, shot_meta: dict[str, Any] | None) -> list[str]:
    """Lint rule (still prompts): every in-frame character's clause is present; under no_text the
    no-text clause is present and the prompt asks for no on-image words. [] for legacy shots."""
    problems: list[str] = []
    for c in cast_entries(shot_meta):
        if _norm(c["clause"]) not in _norm(prompt):
            problems.append(f"missing the locked description of cast character {c.get('character')!r} "
                            "(the still submit injects it)")
    if no_text_required(shot_meta):
        if _norm(NO_TEXT_CLAUSE) not in _norm(prompt):
            problems.append(f"missing the no-text constraint {NO_TEXT_CLAUSE!r}")
        for phrase in text_requests(prompt):
            problems.append(f"the prompt asks for on-image text ({phrase!r}) but the text policy is no_text; "
                            "reword it, or allow text for this shot (shots.json allow_text) or kit "
                            "(prompt_kit.json text_policy \"allow_text\")")
    return problems


def draft_cast(prompt_kit: dict, shots: list[dict]) -> tuple[dict, list[dict]]:
    """The cast of a drafted episode (draft from idea): ``(prompt kit, cast)``.

    Each kit character (``{code: {"name", "role", "description"}}``; a bare string is a description)
    becomes a cast entry ``{code, slug, name, role, description, shots}`` (slugs unique, a missing
    name is "Character <code>", ``shots`` the ids of the shots it is in). The kit gets the default
    ``text_policy`` "no_text" unless it names a valid one. The shots are not changed: the create
    wizard adds the entries to the episode's cast, then gives each shot its ``cast`` before it
    uploads the shots (so a client that uploads the draft as it is keeps working)."""
    raw = prompt_kit.get("characters") if isinstance(prompt_kit.get("characters"), dict) else {}
    cast: list[dict] = []
    kit_characters: dict[str, Any] = {}
    taken: set[str] = set()
    for code, entry in raw.items():
        entry = dict(entry) if isinstance(entry, dict) else {"description": _one_line(entry)}
        name = _one_line(entry.get("name"))[:60].strip() or f"Character {code}"
        base = slugify(name)
        slug, n = base, 2
        while slug in taken:
            slug, n = f"{base[:29]}-{n}", n + 1
        taken.add(slug)
        cast.append({"code": str(code), "slug": slug, "name": name, "role": _one_line(entry.get("role"))[:80],
                     "description": _clip(_one_line(entry.get("description")), 600),
                     "shots": [s["id"] for s in shots if str(code) in [str(c) for c in s.get("characters") or []]]})
        kit_characters[code] = {**entry, "name": name}
    kit = {**prompt_kit, "characters": kit_characters if raw else prompt_kit.get("characters", {})}
    if kit.get("text_policy") not in TEXT_POLICIES:
        kit["text_policy"] = "no_text"
    return kit, cast


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0]


def with_cast_prompts(shots: list[dict]) -> list[dict]:
    """Copies of the planned shots with the still (and end-frame) prompts as the submit sends them,
    for the G1 preview. Shots without a cast or text policy are returned as they are."""
    out = []
    for shot in shots:
        meta = shot.get("shot_meta") or {}
        if not cast_entries(meta) and not no_text_required(meta):
            out.append(shot)
            continue
        new = dict(shot)
        new["prompt"] = inject_cast_prompt(shot.get("prompt") or "", meta)[0]
        if shot.get("end_prompt"):
            new["end_prompt"] = inject_cast_prompt(shot["end_prompt"], meta)[0]
        out.append(new)
    return out
