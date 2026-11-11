"""Episode inputs, outputs and dry-run files resolve only through hfvg/paths.py (EPISODES_DIR /
DATA_DIR). A hardcoded data/episodes or data/dry_run_outputs path in hfvg/ or api/ resolves
against the process cwd (/app for the hosted worker, /app/api for the API), not the data volume.

Comments and docstrings count too: they name the configurable location (<EPISODES_DIR>/...).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = "hfvg/paths.py"
SCANNED_DIRS = ("hfvg", "api")

HARDCODED_PATH = (
    re.compile(r"\bdata/(?:episodes|dry_run_outputs)\b"),
    # the same dirs joined from parts: Path("data") / "episodes", os.path.join("./data", "dry_run_outputs")
    re.compile(r"""["'](?:\./)?data["']\s*\)?\s*[/,]\s*["'](?:episodes|dry_run_outputs)\b"""),
)
ROOT_ENV_READ = re.compile(r"""(?:getenv|environ\.get|environ\[)\(?\s*["'](?:EPISODES_DIR|DATA_DIR)["']""")


def _modules() -> list[str]:
    files = sorted(
        p.relative_to(REPO_ROOT).as_posix()
        for d in SCANNED_DIRS for p in (REPO_ROOT / d).rglob("*.py")
        if "tests" not in p.relative_to(REPO_ROOT).parts and not p.name.startswith("test_")
    )
    assert "api/main.py" in files and len(files) > 50, f"scan found only {len(files)} modules"
    return [f for f in files if f != HELPER]


def find_violations(files, patterns, read=lambda rel: (REPO_ROOT / rel).read_text(encoding="utf-8")) -> list[str]:
    found = []
    for rel in files:
        for n, line in enumerate(read(rel).splitlines(), 1):
            if any(p.search(line) for p in patterns):
                found.append(f"{rel}:{n}: {line.strip()}")
    return found


def test_no_hardcoded_episode_or_dry_run_paths_outside_the_helper():
    bad = {
        "a.py": 'base_path = Path(f"data/episodes/{episode_id}")',
        "b.py": 'output_dir = Path("data/episodes") / episode_id / "outputs" / shot_id',
        "c.py": 'scratch_dir = Path(f"data/dry_run_outputs/{workflow_id}/refs")',
        "d.py": 'Path(os.getenv("EPISODES_DIR", "./data/episodes")) / episode_id',
        "e.py": 'root = Path("data") / "episodes" / episode_id',
        "f.py": 'os.path.join("./data", "dry_run_outputs")',
        "g.py": '# outputs go to data/episodes/{episode}/outputs/{shot}/',
    }
    fine = {"ok.py": 'p = episode_dir(ep) / "shots.json"\nq = Path(__file__).parent / "data" / "model_caps.json"'}
    samples = {**bad, **fine}
    flagged = find_violations(samples, HARDCODED_PATH, read=samples.__getitem__)
    assert sorted(v.split(":")[0] for v in flagged) == sorted(bad), flagged

    violations = find_violations(_modules(), HARDCODED_PATH)
    assert violations == [], (
        "hardcoded data/episodes or data/dry_run_outputs paths (resolve them with hfvg.paths: "
        "episode_dir / episodes_dir / outputs_dir / dry_run_outputs_dir):\n" + "\n".join(violations))


def test_episodes_and_data_dir_env_vars_are_read_only_by_the_helper():
    samples = {"a.py": 'os.getenv("EPISODES_DIR", "")', "b.py": 'os.environ.get("DATA_DIR")',
               "c.py": 'os.environ["EPISODES_DIR"]', "ok.py": 'os.getenv("HFVG_EPISODES_DIR", "")'}
    flagged = find_violations(samples, (ROOT_ENV_READ,), read=samples.__getitem__)
    assert sorted(v.split(":")[0] for v in flagged) == ["a.py", "b.py", "c.py"], flagged

    violations = find_violations(_modules(), (ROOT_ENV_READ,))
    assert violations == [], (
        "EPISODES_DIR / DATA_DIR read outside hfvg/paths.py (call hfvg.paths so the fallbacks stay "
        "in one place):\n" + "\n".join(violations))
