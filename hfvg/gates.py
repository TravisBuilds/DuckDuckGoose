"""Gate policy loader for HARNESS-GATES.json."""

import json
import re
import os
from pathlib import Path
from typing import Any


OWNER_APPROVAL_POINTS_KEY = "owner_approval_points"
# Older policy files used a longer key; accepted with a DeprecationWarning (generic, no names).
LEGACY_OWNER_APPROVAL_POINTS_KEYS = ("project_owner_approval_points",)


def owner_approval_points(data: dict, policy_path: str | Path = "<policy>") -> list:
    """The policy's ``owner_approval_points`` (gate ids that need the owner's approval).

    A missing key fails loudly (ValueError naming the key and the file): an empty default would
    silently drop every owner approval point. A legacy alias is accepted with a DeprecationWarning."""
    if not isinstance(data, dict):
        raise ValueError(f"gate policy {policy_path} must be a JSON object")
    if OWNER_APPROVAL_POINTS_KEY in data:
        value = data[OWNER_APPROVAL_POINTS_KEY]
        key = OWNER_APPROVAL_POINTS_KEY
    else:
        legacy = next((k for k in LEGACY_OWNER_APPROVAL_POINTS_KEYS if k in data), None)
        if legacy is None:
            raise ValueError(
                f"gate policy {policy_path} is missing the required key '{OWNER_APPROVAL_POINTS_KEY}' "
                "(the list of gate ids that need the owner's approval, e.g. "
                '["G1.01", "G2.01", ...]); see docs/GATE-POLICY.md'
            )
        import warnings
        warnings.warn(
            f"gate policy {policy_path} uses the deprecated key '{legacy}'; rename it to "
            f"'{OWNER_APPROVAL_POINTS_KEY}'",
            DeprecationWarning,
            stacklevel=3,
        )
        value = data[legacy]
        key = legacy
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"gate policy {policy_path}: '{key}' must be a list of gate id strings")
    return list(value)


class GatePolicy:
    """
    Loads and validates HARNESS-GATES.json policy data.
    
    Expected structure (v1.1):
    - 71 total gates
    - 69 blocking gates (blocking = 'yes')
    - 15 project owner approval points
    - check_type distribution: code 31, judge 26, human 14
    """
    
    def __init__(self, policy_path: str | Path):
        """Load gate policy from JSON file."""
        self.policy_path = Path(policy_path)
        
        with open(self.policy_path) as f:
            self.data = json.load(f)
        
        self.schema_version = self.data.get("schema_version")
        self.series = self.data.get("series")
        self.harness = self.data.get("harness")
        self.gates = self.data.get("gates", [])
        
        self.owner_approval_points = owner_approval_points(self.data, self.policy_path)

        self.budgets = self.data.get("budgets", {})
        self.rules = self.data.get("rules", {})
        
        # Build gate index
        self.gates_by_id = {gate["gate_id"]: gate for gate in self.gates}
        
        # Validate structure
        self._validate()
    
    def _validate(self):
        """Validate policy structure matches v1.1 expectations."""
        # Count gates
        total_gates = len(self.gates)
        
        # Count blocking gates (blocking is string 'yes'/'no')
        blocking_gates = sum(1 for g in self.gates if g.get("blocking") == "yes")
        
        # Count check_type distribution
        check_type_counts = {"code": 0, "judge": 0, "human": 0}
        for gate in self.gates:
            check_type = gate.get("check_type", "")
            if check_type in check_type_counts:
                check_type_counts[check_type] += 1
        
        # Count owner approval points
        owner_approvals = len(self.owner_approval_points)
        
        # Validate expected counts for v1.1
        assert total_gates == 71, f"Expected 71 gates, got {total_gates}"
        assert blocking_gates == 69, f"Expected 69 blocking gates, got {blocking_gates}"
        assert owner_approvals == 15, f"Expected 15 owner approval points, got {owner_approvals}"
        assert check_type_counts["code"] == 31, f"Expected 31 code gates, got {check_type_counts['code']}"
        assert check_type_counts["judge"] == 26, f"Expected 26 judge gates, got {check_type_counts['judge']}"
        assert check_type_counts["human"] == 14, f"Expected 14 human gates, got {check_type_counts['human']}"
        
        # Validate gate IDs are unique
        gate_ids = [g["gate_id"] for g in self.gates]
        assert len(gate_ids) == len(set(gate_ids)), "Gate IDs must be unique"
    
    def get_gate(self, gate_id: str) -> dict[str, Any] | None:
        """Get gate by ID."""
        return self.gates_by_id.get(gate_id)
    
    def is_blocking(self, gate_id: str) -> bool:
        """Check if gate is blocking (string comparison)."""
        gate = self.get_gate(gate_id)
        return gate and gate.get("blocking") == "yes"
    
    def requires_owner_approval(self, gate_id: str) -> bool:
        """Check if gate requires project owner approval."""
        return gate_id in self.owner_approval_points
    
    def get_gates_for_step(self, step: str) -> list[dict[str, Any]]:
        """Get all gates for a specific step."""
        return [g for g in self.gates if g.get("step") == step or g.get("step") == "all"]
    
    def get_budget(self, episode: str = "ep04") -> dict[str, Any]:
        """Get budget configuration for an episode."""
        return self.budgets.get(episode, {})
    
    def get_higgsfield_budget(self, episode: str = "ep04") -> dict[str, Any]:
        """Get Higgsfield budget configuration."""
        budget = self.get_budget(episode)
        return budget.get("higgsfield", {})
    
    def get_elevenlabs_budget(self, episode: str = "ep04") -> dict[str, Any]:
        """Get ElevenLabs budget configuration."""
        budget = self.get_budget(episode)
        return budget.get("elevenlabs", {})
    
    def get_usd_line_caps(self, episode: str = "ep04") -> dict[str, str]:
        """USD line caps (decimal strings) for an episode from the policy's `usd` section, if any."""
        usd = self.budgets.get("usd", {})
        return dict(usd.get(f"{episode}_lines_app_usd", {}))

    def get_unit_prices(self) -> dict[str, float]:
        """Get unit prices for all services."""
        return self.budgets.get("unit_prices", {})


_PROJECT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


POLICY_FILE_NAME = "HARNESS-GATES.json"


def policy_search_paths(project: str | None = None) -> list[tuple[str, Path]]:
    """Where load_policy looks for the gate policy, in order: [(source, path)].

    1. ``HFVG_GATE_POLICY``: explicit file (the active project only; it MUST exist, see load_policy)
    2. ``HFVG_RUN_DIR``: ``<run dir>/HARNESS-GATES.json`` then ``<run dir>/policy/HARNESS-GATES.json``
       (the active project only)
    3. ``examples/<project>/HARNESS-GATES.json`` in the repo (the public fallback)

    The active project is ``HFVG_PROJECT`` (default ``demo``): an explicit ``load_policy("other")``
    for a different project only uses the examples fallback, so a private run policy never leaks
    into another project's lookup."""
    active = os.getenv("HFVG_PROJECT", "demo")
    if project is None:
        project = active
    if not _PROJECT_RE.fullmatch(project or ""):
        raise ValueError(f"invalid HFVG_PROJECT / project name {project!r} (letters, digits, '.', '_', '-')")
    paths: list[tuple[str, Path]] = []
    if project == active:
        env_path = os.getenv("HFVG_GATE_POLICY", "").strip()
        if env_path:
            paths.append(("HFVG_GATE_POLICY", Path(env_path)))
        run_dir = os.getenv("HFVG_RUN_DIR", "").strip()
        if run_dir:
            paths.append(("HFVG_RUN_DIR", Path(run_dir) / POLICY_FILE_NAME))
            paths.append(("HFVG_RUN_DIR", Path(run_dir) / "policy" / POLICY_FILE_NAME))
    paths.append(("examples", Path(__file__).parent.parent / "examples" / project / POLICY_FILE_NAME))
    return paths


def load_policy(project: str | None = None) -> GatePolicy:
    """
    Load the gate policy (HARNESS-GATES.json) for a project.

    Search order: see policy_search_paths (HFVG_GATE_POLICY, then HFVG_RUN_DIR, then
    examples/<project>/). Documented in docs/GATE-POLICY.md.

    Args:
        project: Project name. If None, reads from HFVG_PROJECT env var (default: 'demo')

    Raises:
        FileNotFoundError: HFVG_GATE_POLICY is set but the file does not exist (never a silent
            fallback to the public example), or no policy exists in any location.
        ValueError: invalid project name, unreadable JSON or a policy that fails validation.
    """
    paths = policy_search_paths(project)
    for source, policy_path in paths:
        if source == "HFVG_GATE_POLICY" and not policy_path.is_file():
            raise FileNotFoundError(
                f"HFVG_GATE_POLICY points to {policy_path}, which does not exist; refusing to fall back "
                "to another policy (unset HFVG_GATE_POLICY to use the run dir / examples lookup)"
            )
        if policy_path.is_file():
            try:
                return GatePolicy(policy_path)
            except json.JSONDecodeError as e:
                raise ValueError(f"gate policy {policy_path} ({source}) is not valid JSON: {e}") from None
            except AssertionError as e:
                raise ValueError(f"gate policy {policy_path} ({source}) failed validation: {e}") from None
    raise FileNotFoundError(
        "HARNESS-GATES.json not found. Searched:\n"
        + "\n".join(f"  - {p} ({src})" for src, p in paths)
        + "\nSet HFVG_GATE_POLICY to the policy file or HFVG_RUN_DIR to the run directory."
    )
