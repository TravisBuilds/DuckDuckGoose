"""Round 7 B2/B3: gate policy location (HFVG_GATE_POLICY / HFVG_RUN_DIR before the examples
fallback) and the required ``owner_approval_points`` key."""

import json
import warnings
from pathlib import Path

import pytest

from hfvg import gates

REPO = Path(__file__).resolve().parent.parent
EXAMPLE = REPO / "examples" / "demo" / "HARNESS-GATES.json"


def _policy(path: Path, **changes) -> Path:
    data = json.loads(EXAMPLE.read_text())
    for k, v in changes.items():
        if v is None:
            data.pop(k, None)
        else:
            data[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("HFVG_GATE_POLICY", "HFVG_RUN_DIR", "HFVG_PROJECT"):
        monkeypatch.delenv(k, raising=False)


def test_examples_fallback_when_nothing_configured():
    assert gates.load_policy().policy_path.resolve() == EXAMPLE.resolve()


def test_hfvg_gate_policy_wins_over_run_dir_and_examples(tmp_path, monkeypatch):
    explicit = _policy(tmp_path / "explicit.json", series="explicit-policy")
    run_dir = tmp_path / "run"
    _policy(run_dir / "HARNESS-GATES.json", series="run-dir-policy")
    monkeypatch.setenv("HFVG_GATE_POLICY", str(explicit))
    monkeypatch.setenv("HFVG_RUN_DIR", str(run_dir))
    p = gates.load_policy()
    assert p.policy_path == explicit
    assert p.series == "explicit-policy"


def test_run_dir_before_examples_and_policy_subdir(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    sub = _policy(run_dir / "policy" / "HARNESS-GATES.json", series="run-subdir-policy")
    monkeypatch.setenv("HFVG_RUN_DIR", str(run_dir))
    assert gates.load_policy().policy_path == sub
    top = _policy(run_dir / "HARNESS-GATES.json", series="run-top-policy")
    assert gates.load_policy().policy_path == top
    assert gates.load_policy().series == "run-top-policy"


def test_missing_explicit_policy_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("HFVG_GATE_POLICY", str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError, match="HFVG_GATE_POLICY points to"):
        gates.load_policy()


def test_run_policy_is_not_used_for_another_project(tmp_path, monkeypatch):
    explicit = _policy(tmp_path / "explicit.json")
    monkeypatch.setenv("HFVG_GATE_POLICY", str(explicit))
    assert gates.load_policy("demo").policy_path == explicit
    with pytest.raises(FileNotFoundError, match="not found"):
        gates.load_policy("other-project")


def test_invalid_json_and_project_name_are_explicit(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("HFVG_GATE_POLICY", str(bad))
    with pytest.raises(ValueError, match="not valid JSON"):
        gates.load_policy()
    with pytest.raises(ValueError, match="invalid HFVG_PROJECT"):
        gates.load_policy("../etc")


def test_missing_owner_approval_points_fails_loudly(tmp_path, monkeypatch):
    path = _policy(tmp_path / "p.json", owner_approval_points=None)
    monkeypatch.setenv("HFVG_GATE_POLICY", str(path))
    try:
        gates.load_policy()
        msg = None
    except ValueError as e:
        msg = str(e)
    assert msg is not None, "a policy without owner_approval_points must not load"
    assert "missing the required key 'owner_approval_points'" in msg
    assert str(path) in msg and "docs/GATE-POLICY.md" in msg


def test_owner_approval_points_must_be_list_of_strings(tmp_path):
    with pytest.raises(ValueError, match="must be a list of gate id strings"):
        gates.owner_approval_points({"owner_approval_points": "G1.01"}, "x.json")
    with pytest.raises(ValueError, match="must be a list of gate id strings"):
        gates.owner_approval_points({"owner_approval_points": [1]}, "x.json")


def test_legacy_alias_accepted_with_deprecation_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        points = gates.owner_approval_points({"project_owner_approval_points": ["G1.01"]}, "x.json")
    assert points == ["G1.01"]
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert dep and "rename it to 'owner_approval_points'" in str(dep[0].message)
    # the canonical key wins without a warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert gates.owner_approval_points(
            {"owner_approval_points": ["G2.01"], "project_owner_approval_points": ["G1.01"]}) == ["G2.01"]


def test_legacy_alias_is_generic():
    """Public repo: the alias list holds key names only, never a person / project name."""
    assert all(k.endswith("_owner_approval_points") and k.split("_")[0] == "project"
               for k in gates.LEGACY_OWNER_APPROVAL_POINTS_KEYS)


def test_gate_policy_doc_documents_lookup_and_key():
    doc = (REPO / "docs" / "GATE-POLICY.md").read_text()
    for needle in ("HFVG_GATE_POLICY", "HFVG_RUN_DIR", "owner_approval_points", "examples/"):
        assert needle in doc, needle
