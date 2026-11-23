"""The neutral gate policy (examples/neutral/HARNESS-GATES.json) a show without its own policy gets, the
lookup's last fallback to it (hfvg.gates.policy_search_paths), the first series' policy kept with the
series' data, and a show.json naming a gate policy in its own folder (hfvg.show_profile)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from hfvg import gates, show_profile
from hfvg.gates import GatePolicy, load_policy, policy_search_paths

REPO_ROOT = Path(__file__).resolve().parent.parent
NEUTRAL = REPO_ROOT / "examples" / "neutral" / "HARNESS-GATES.json"
DEMO = REPO_ROOT / "examples" / "demo" / "HARNESS-GATES.json"
SERIES = REPO_ROOT / "examples" / "first-series" / "HARNESS-GATES.json"
SHOW_WORDS = re.compile(r"\b(ducks?|duckl\w*|quack\w*|sheep|crows?|moose|antlers?|animals?|species|wings?|feathers?|"
                        r"fleece|beaks?|hoof|hooves|paws?|fur|furry|plush|tuque|mallard|waddl\w*|bram|nettle|"
                        r"resort)\b", re.IGNORECASE)


@pytest.fixture(autouse=True)
def _no_policy_env(monkeypatch):
    for var in ("HFVG_PROJECT", "HFVG_GATE_POLICY", "HFVG_RUN_DIR", "HFVG_SHOW_PROFILE_PATH"):
        monkeypatch.delenv(var, raising=False)


def _strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _shape(policy: GatePolicy) -> list[tuple]:
    return [(g["gate_id"], g["step"], g["blocking"], g.get("approver"), g["check_type"]) for g in policy.gates]


def test_the_neutral_policy_has_the_same_gates_in_general_words():
    neutral, demo = GatePolicy(NEUTRAL), GatePolicy(DEMO)
    assert neutral.series == "Neutral default"
    assert _shape(neutral) == _shape(demo), "same ids, steps, blocking, approvers and check types"
    assert neutral.owner_approval_points == demo.owner_approval_points
    hits = sorted({m.group(0).lower() for s in _strings(json.loads(NEUTRAL.read_text())) for m in
                   SHOW_WORDS.finditer(s)})
    assert hits == [], f"the neutral policy names a show's subjects: {hits}"
    texts = [(g["name"] + " " + g["check"]).lower() for g in neutral.gates]
    assert any("locked character identity" in t for t in texts) and any("figures in frame" in t for t in texts)


def test_the_first_series_policy_is_the_demo_policy():
    assert SERIES.read_bytes() == DEMO.read_bytes(), (
        "examples/demo stays the default project's file (HFVG_PROJECT=demo); the series' copy must match it")


def test_the_lookup_order_is_unchanged_and_neutral_comes_last(tmp_path, monkeypatch):
    run = tmp_path / "run"
    (run / "policy").mkdir(parents=True)
    monkeypatch.setenv("HFVG_GATE_POLICY", str(tmp_path / "explicit.json"))
    monkeypatch.setenv("HFVG_RUN_DIR", str(run))
    sources = [s for s, _ in policy_search_paths()]
    assert sources == ["HFVG_GATE_POLICY", "HFVG_RUN_DIR", "HFVG_RUN_DIR", "examples"], (
        "demo ships a policy: no neutral fallback")
    assert [s for s, _ in policy_search_paths("neutral")] == ["examples"]


def _fake_repo(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "repo"
    (root / "hfvg").mkdir(parents=True)
    for name in ("demo", "neutral"):
        (root / "examples" / name).mkdir(parents=True)
    shutil.copy(DEMO, root / "examples" / "demo" / "HARNESS-GATES.json")
    shutil.copy(NEUTRAL, root / "examples" / "neutral" / "HARNESS-GATES.json")
    monkeypatch.setattr(gates, "__file__", str(root / "hfvg" / "gates.py"))
    return root


def test_a_project_folder_without_a_policy_gets_the_neutral_one(tmp_path, monkeypatch):
    root = _fake_repo(tmp_path, monkeypatch)
    (root / "examples" / "cooking").mkdir()
    (root / "examples" / "cooking" / "show.json").write_text('{"schema": "hfvg.show/1"}')
    assert load_policy("cooking").policy_path == root / "examples" / "neutral" / "HARNESS-GATES.json"
    monkeypatch.setenv("HFVG_PROJECT", "cooking")
    assert load_policy().series == "Neutral default"
    assert [s for s, _ in policy_search_paths()] == ["examples", "neutral"]


def test_an_unknown_project_still_fails_and_a_private_run_policy_still_wins(tmp_path, monkeypatch):
    root = _fake_repo(tmp_path, monkeypatch)
    with pytest.raises(FileNotFoundError):
        load_policy("missing")
    monkeypatch.setenv("HFVG_PROJECT", "missing")
    with pytest.raises(FileNotFoundError):
        load_policy()
    (root / "examples" / "cooking").mkdir()
    run = tmp_path / "run"
    run.mkdir()
    data = json.loads(NEUTRAL.read_text())
    data["series"] = "Private run policy"
    (run / "HARNESS-GATES.json").write_text(json.dumps(data))
    monkeypatch.setenv("HFVG_PROJECT", "cooking")
    monkeypatch.setenv("HFVG_RUN_DIR", str(run))
    assert load_policy().series == "Private run policy"


# ------------------------------------------------------------------------------------------ a show's own policy


@pytest.fixture
def show(tmp_path, monkeypatch):
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path / "eps"))
    for var in ("PROMPT_LINT_KIT_PATH", "SHOTS_JSON_PATH"):
        monkeypatch.delenv(var, raising=False)
    folder = tmp_path / "eps" / "ep73cook"
    folder.mkdir(parents=True)
    return folder


def _write_show(folder: Path, **gates_section) -> None:
    (folder / "show.json").write_text(json.dumps({"schema": "hfvg.show/1", "name": "Cooking fixture",
                                                  "gates": gates_section}))


def test_a_show_without_a_policy_file_gets_the_neutral_policy(show):
    _write_show(show)
    profile = show_profile.resolve_show_profile("ep73cook")
    assert show_profile.load_gate_policy(profile).series == "Neutral default"
    assert show_profile.gate_policy_summary(profile) == {"from": "neutral", "series": "Neutral default", "errors": []}
    assert show_profile.show_profile_problems("ep73cook") == []


@pytest.mark.parametrize("name", ["gates.json", "policy/gates.json"])
def test_a_show_names_a_policy_in_its_own_folder(show, name):
    data = json.loads(NEUTRAL.read_text())
    data["series"] = "Cooking fixture gates"
    (show / name).parent.mkdir(exist_ok=True)
    (show / name).write_text(json.dumps(data))
    _write_show(show, policy_file=name)
    profile = show_profile.resolve_show_profile("ep73cook")
    assert show_profile.load_gate_policy(profile).policy_path == show / name
    assert show_profile.gate_policy_summary(profile) == {"from": "show", "series": "Cooking fixture gates",
                                                         "errors": []}
    assert show_profile.show_profile_problems("ep73cook") == []


def test_a_policy_file_that_is_missing_or_invalid_is_a_preflight_problem(show):
    _write_show(show, policy_file="gates.json")
    [problem] = show_profile.show_profile_problems("ep73cook")
    assert problem.endswith("gates.policy_file 'gates.json' is not in the show's folder")
    (show / "gates.json").write_text(json.dumps({"series": "x", "gates": []}))
    [problem] = show_profile.show_profile_problems("ep73cook")
    assert "gates.policy_file 'gates.json' does not load" in problem
    summary = show_profile.gate_policy_summary(show_profile.resolve_show_profile("ep73cook"))
    assert summary["from"] == "show" and summary["errors"] == [problem.split(": ", 1)[1]]


@pytest.mark.parametrize("name", ["../gates.json", "policy/../../gates.json", "/etc/gates.json"])
def test_a_policy_file_outside_the_show_folder_is_refused(show, name):
    _write_show(show, policy_file=name)
    with pytest.raises(show_profile.ShowProfileError, match="gates.policy_file must be a file name"):
        show_profile.resolve_show_profile("ep73cook")


def test_an_episode_of_the_first_series_keeps_the_deployments_policy(show):
    (show / "prompt_kit.json").write_text("{}")
    profile = show_profile.resolve_show_profile("ep73cook")
    assert profile.source == "legacy-adapter"
    assert show_profile.load_gate_policy(profile).policy_path == load_policy().policy_path == DEMO
    assert show_profile.gate_policy_summary(profile)["from"] == "deployment"
