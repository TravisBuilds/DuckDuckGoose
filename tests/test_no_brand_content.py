"""G-BRAND-1: the public repo carries no private project / brand / character content.

Every tracked file (content and path, text and binary) is scanned for a case-insensitive
substring denylist. Only this file is exempt (it has to spell the terms out). History is not
rewritten; this guards the tree from now on.
"""

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SELF = "tests/test_no_brand_content.py"

# Lower-case substrings. Never shrink this list to make a test pass: remove the content instead.
BRAND_DENYLIST = (
    # owner / brand / project
    "travis",
    "quacked",
    "concierge",
    "mid-mountain",
    "mid mountain",
    "mid_mountain",
    "midmountain",
    # story characters, story text and private asset paths
    "mochi",
    "rowan",
    "hazel",
    "birch",
    "willow",
    "twig",
    "yuzu",
    "capybara",
    "snow-monkeys",
    "snow monkeys",
    "ep04-moose",
    "duck-ab02",
    # private ElevenLabs voice id of the project narrator
    "4sirbxwrtrlmpv80mjkq",
)

REQUIRED_TERMS = {"travis", "quacked", "concierge", "mid-mountain", "mochi", "rowan", "hazel",
                  "birch", "willow", "twig"}


def _tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=False)
    assert out.returncode == 0, f"git ls-files failed: {out.stderr.decode(errors='replace')}"
    files = [f for f in out.stdout.decode("utf-8", errors="surrogateescape").split("\0") if f]
    assert len(files) > 100, f"git ls-files returned only {len(files)} files"
    return files


def find_violations(files, read=lambda p: (REPO_ROOT / p).read_bytes()) -> list[str]:
    """Return "path: term (where)" for every denylisted term in a path or in file content."""
    found = []
    for rel in files:
        if rel == SELF:
            continue
        lowered_path = rel.lower()
        for term in BRAND_DENYLIST:
            if term in lowered_path:
                found.append(f"{rel}: {term!r} (file name)")
        try:
            data = read(rel)
        except FileNotFoundError:
            continue  # deleted in the working tree but still in the index
        lowered = data.lower()
        for term in BRAND_DENYLIST:
            idx = lowered.find(term.encode())
            if idx >= 0:
                line = lowered.count(b"\n", 0, idx) + 1
                found.append(f"{rel}:{line}: {term!r}")
    return found


def test_denylist_covers_the_required_terms():
    assert REQUIRED_TERMS <= set(BRAND_DENYLIST), REQUIRED_TERMS - set(BRAND_DENYLIST)
    assert all(t == t.lower() and t for t in BRAND_DENYLIST)


def test_matcher_flags_content_and_file_names_case_insensitively():
    fake = {"docs/a.md": b"Hello\nthe Mid-Mountain REST\n", "img/Rowan.png": b"\x89PNG\x00",
            "ok.txt": b"nothing to see"}
    got = find_violations(list(fake), read=lambda p: fake[p])
    assert "docs/a.md:2: 'mid-mountain'" in got, got
    assert "img/Rowan.png: 'rowan' (file name)" in got, got
    assert not [g for g in got if g.startswith("ok.txt")], got
    assert find_violations([SELF], read=lambda p: b"travis") == []


def test_no_brand_terms_in_tracked_files():
    violations = find_violations(_tracked_files())
    assert not violations, (
        f"{len(violations)} private brand/story term(s) in tracked files:\n" + "\n".join(violations[:50])
    )


def test_private_example_and_fixture_dirs_are_gone():
    assert not (REPO_ROOT / "examples" / "mid-mountain-rest").exists()
    assert not (REPO_ROOT / "fixtures" / "ep05").exists()
    assert (REPO_ROOT / "examples" / "demo" / "HARNESS-GATES.json").is_file()
    for name in ("BEATMAP.md", "CONTINUITY.md", "VO-AUDIO.md"):
        assert (REPO_ROOT / "fixtures" / "demo" / name).is_file(), name


def test_hfvg_project_defaults_to_demo_and_is_read_from_env(tmp_path, monkeypatch):
    import shutil

    from hfvg import gates

    monkeypatch.delenv("HFVG_PROJECT", raising=False)
    assert gates.load_policy().policy_path == REPO_ROOT / "examples" / "demo" / "HARNESS-GATES.json"

    # A second project next to demo: the env var must select it (not a hardcoded name).
    fake_root = tmp_path / "repo"
    (fake_root / "hfvg").mkdir(parents=True)
    for name in ("demo", "other"):  # both exist, so a hardcoded project resolves (to the wrong file)
        (fake_root / "examples" / name).mkdir(parents=True)
        shutil.copy(REPO_ROOT / "examples" / "demo" / "HARNESS-GATES.json",
                    fake_root / "examples" / name / "HARNESS-GATES.json")
    monkeypatch.setattr(gates, "__file__", str(fake_root / "hfvg" / "gates.py"))
    monkeypatch.setenv("HFVG_PROJECT", "other")
    assert gates.load_policy().policy_path == fake_root / "examples" / "other" / "HARNESS-GATES.json"
    monkeypatch.setenv("HFVG_PROJECT", "missing")
    err = None
    try:
        gates.load_policy()
    except Exception as e:  # noqa: BLE001 - the type is asserted below
        err = e
    assert isinstance(err, FileNotFoundError), f"an unknown HFVG_PROJECT must fail, not fall back silently: {err!r}"


@pytest.mark.parametrize("bad", ["../demo", "demo/../../etc", "", ".hidden", "a b"])
def test_hfvg_project_rejects_path_like_names(monkeypatch, bad):
    from hfvg.gates import load_policy

    monkeypatch.setenv("HFVG_PROJECT", bad)
    err = None
    try:
        load_policy()
    except Exception as e:  # noqa: BLE001 - the type is asserted below
        err = e
    assert isinstance(err, ValueError) and "HFVG_PROJECT" in str(err), repr(err)
