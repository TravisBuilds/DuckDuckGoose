"""The engine carries no show vocabulary: no species, creature or show words in the code, comments,
docstrings or data of hfvg/, api/ and the web app's source, and none in a module's file name. A show's
own words live in its show profile (docs/SHOW-PROFILE.md), never in the engine.

The scan is the Python equivalent of

    rg -n -i -w '(\\w*_)?ducks?(_\\w*)?|duckl\\w*|quack\\w*|sheep|crows?|moose|antlers?|animals?|species|wings?|feathers?|fleece|beaks?|hoof|hooves|paws?|fur|furry|anthropomorph\\w*|quadruped\\w*|four-legged|bipedal|plush|tuque|mallard|waddl\\w*|resort|bram|nettle' hfvg api web/app web/components web/lib

over the tracked files that are not tests, after removing "DuckDuckGoose" everywhere and the audio
ducking tokens (duck_db, duck_mode, duck_envelope, ducking, ducked, DUCK_MODES) in the audio files.
A hit passes only through an entry of tests/show_vocabulary_allowlist.json ({path, pattern, reason,
remove_by}); an entry without a reason, past its remove_by date or that allows nothing fails.
"""

from __future__ import annotations

import fnmatch
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST = REPO_ROOT / "tests" / "show_vocabulary_allowlist.json"
SCANNED = ("hfvg", "api", "web/app", "web/components", "web/lib")
NAMED = ("hfvg/", "api/")  # the module-name guard
NAME_EXEMPT = "hfvg/compat/"
SPECIES_FREE = ("examples/neutral", "tests/fixtures/shows/human_satire")

TERMS = (
    r"(?:\w*_)?ducks?(?:_\w*)?",  # duck(s), duck_*, *_duck
    r"duckl\w*", r"quack\w*", r"sheep", r"crows?", r"moose", r"antlers?", r"animals?", r"species",
    r"wings?", r"feathers?", r"fleece", r"beaks?", r"hoof", r"hooves", r"paws?", r"fur", r"furry",
    r"anthropomorph\w*", r"quadruped\w*", r"four-legged", r"bipedal", r"plush", r"tuque", r"mallard",
    r"waddl\w*", r"resort",
    r"bram", r"nettle",  # character names of the first series
)
WORDS = re.compile(r"(?<!\w)(?:" + "|".join(TERMS) + r")(?!\w)", re.IGNORECASE)
NAME_WORDS = re.compile(r"(?<![a-z0-9])(?:" + "|".join(TERMS) + r")(?![a-z0-9])", re.IGNORECASE)
PRODUCT = re.compile(r"duckduckgoose", re.IGNORECASE)
AUDIO_FILES = frozenset({"hfvg/audio_mix.py", "hfvg/activities/audio.py", "hfvg/cli.py"})
AUDIO_TOKENS = re.compile(r"duck_db|duck_mode|duck_envelope|ducking|ducked|DUCK_MODES", re.IGNORECASE)


@dataclass(frozen=True)
class Hit:
    path: str
    line_no: int  # 0: the file name
    word: str
    line: str

    def __str__(self) -> str:
        where = f"{self.path} (file name)" if self.line_no == 0 else f"{self.path}:{self.line_no}"
        return f"{where}: {self.word!r} | {self.line[:160]}"


def is_test(path: str) -> bool:
    p = Path(path)
    return (any(part in ("tests", "test", "__tests__") for part in p.parts[:-1])
            or p.name.startswith("test_") or p.name.endswith("_test.py")
            or ".test." in p.name or ".spec." in p.name)


def is_audio(path: str) -> bool:
    return path in AUDIO_FILES or (path.startswith("api/") and "audio" in Path(path).name)


def scan(files: dict[str, str]) -> list[Hit]:
    """Every term in the text of ``files`` (path -> text)."""
    hits = []
    for path, text in sorted(files.items()):
        for n, line in enumerate(PRODUCT.sub("", text).splitlines(), 1):
            if is_audio(path):
                line = AUDIO_TOKENS.sub("", line)
            hits += [Hit(path, n, m.group(0), line.strip()) for m in WORDS.finditer(line)]
    return hits


def scan_names(paths: list[str]) -> list[Hit]:
    """Every term in a file name under hfvg/ or api/ (outside hfvg/compat/)."""
    return [Hit(path, 0, m.group(0), path) for path in sorted(paths)
            if path.startswith(NAMED) and not path.startswith(NAME_EXEMPT)
            for m in NAME_WORDS.finditer(path)]


def tracked(*roots: str) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *roots], cwd=REPO_ROOT, capture_output=True, check=False)
    assert out.returncode == 0, f"git ls-files failed: {out.stderr.decode(errors='replace')}"
    return [f for f in out.stdout.decode("utf-8", errors="surrogateescape").split("\0") if f]


def read_texts(paths: list[str]) -> dict[str, str]:
    texts = {}
    for path in paths:
        try:
            texts[path] = (REPO_ROOT / path).read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError):
            continue  # deleted in the working tree, or not text
    return texts


def load_allowlist() -> list[dict]:
    return json.loads(ALLOWLIST.read_text(encoding="utf-8"))["entries"]


def allows(entry: dict, hit: Hit) -> bool:
    return fnmatch.fnmatchcase(hit.path, entry["path"]) and re.search(entry["pattern"], hit.line) is not None


def core_hits() -> list[Hit]:
    paths = [p for p in tracked(*SCANNED) if not is_test(p)]
    assert len(paths) > 100, f"git ls-files returned only {len(paths)} core files"
    return scan(read_texts(paths)) + scan_names(paths)


def violations(hits: list[Hit], allowlist: list[dict]) -> list[Hit]:
    return [h for h in hits if not any(allows(e, h) for e in allowlist)]


def test_core_has_no_show_vocabulary():
    found = violations(core_hits(), load_allowlist())
    assert not found, ("show vocabulary in core files (move it to the show profile, or allowlist it with a "
                       "reason and a remove_by date):\n" + "\n".join(map(str, found)))


def test_every_allowlist_entry_has_a_reason_and_is_not_past_its_date():
    today = date.today()
    problems = []
    for entry in load_allowlist():
        name = f"{entry.get('path')!r} / {entry.get('pattern')!r}"
        if set(entry) != {"path", "pattern", "reason", "remove_by"}:
            problems.append(f"{name}: needs exactly path, pattern, reason and remove_by")
            continue
        if not str(entry["reason"]).strip():
            problems.append(f"{name}: no reason")
        try:
            re.compile(entry["pattern"])
        except re.error as e:
            problems.append(f"{name}: pattern does not compile ({e})")
        try:
            if date.fromisoformat(entry["remove_by"]) < today:
                problems.append(f"{name}: past its remove_by date {entry['remove_by']}")
        except (TypeError, ValueError):
            problems.append(f"{name}: remove_by {entry['remove_by']!r} is not a YYYY-MM-DD date")
    assert not problems, "\n".join(problems)


def test_no_allowlist_entry_is_stale():
    hits = core_hits()
    stale = [f"{e['path']!r} / {e['pattern']!r}" for e in load_allowlist() if not any(allows(e, h) for h in hits)]
    assert not stale, "allowlist entries that allow nothing (remove them):\n" + "\n".join(stale)


def test_guard_catches_a_planted_word():
    planted = {
        "hfvg/qc/planted.py": '"""The duck waits by the door."""\nfrom hfvg.prompt_lint import duck_clause\n',
        "hfvg/audio_mix.py": "duck_db = -12  # ducking under the voice\n",
        "hfvg/qc/planted_audio.py": "DUCK_MODES = ()\n",
        "web/components/Planted.tsx": "const label = 'Our Mascot (DuckDuckGoose)';\n",
    }
    found = violations(scan(planted), load_allowlist())
    assert [(h.path, h.word) for h in found] == [
        ("hfvg/qc/planted.py", "duck"), ("hfvg/qc/planted.py", "duck_clause"),
        ("hfvg/qc/planted_audio.py", "DUCK_MODES")]
    names = scan_names(["hfvg/qc/duck_helper.py", "api/animal_routes.py", "hfvg/compat/duck_shim.py",
                        "hfvg/qc/headcount.py", "web/lib/duck.ts"])
    assert [(h.path, h.word) for h in violations(names, load_allowlist())] == [
        ("api/animal_routes.py", "animal"), ("hfvg/qc/duck_helper.py", "duck_helper")]


def test_the_neutral_default_and_the_all_human_fixture_are_species_free():
    paths = tracked(*SPECIES_FREE)
    assert any(p.startswith("examples/neutral/") for p in paths)
    assert any(p.endswith("human_satire/show.json") for p in paths)
    found = scan(read_texts(paths))
    assert not found, "\n".join(map(str, found))
