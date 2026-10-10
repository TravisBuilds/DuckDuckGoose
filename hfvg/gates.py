"""Gate policy loader for HARNESS-GATES.json (Mid-Mountain Rest HARNESS-HANDBOOK v1.1)."""

import json
from pathlib import Path
from typing import Any


class GatePolicy:
    """
    Loads and validates HARNESS-GATES.json policy data.
    
    Expected structure (v1.1):
    - 71 total gates
    - 69 blocking gates (blocking = 'yes')
    - 15 Travis approval points
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
        self.travis_approval_points = self.data.get("travis_approval_points", [])
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
        
        # Count Travis approval points
        travis_approvals = len(self.travis_approval_points)
        
        # Validate expected counts for v1.1
        assert total_gates == 71, f"Expected 71 gates, got {total_gates}"
        assert blocking_gates == 69, f"Expected 69 blocking gates, got {blocking_gates}"
        assert travis_approvals == 15, f"Expected 15 Travis approval points, got {travis_approvals}"
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
    
    def requires_travis_approval(self, gate_id: str) -> bool:
        """Check if gate requires Travis approval."""
        return gate_id in self.travis_approval_points
    
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


def load_policy(project: str = "mid-mountain-rest") -> GatePolicy:
    """
    Load gate policy for a project.
    
    Args:
        project: Project name (default: mid-mountain-rest)
    
    Returns:
        GatePolicy instance
    """
    policy_path = Path(__file__).parent.parent / "examples" / project / "HARNESS-GATES.json"
    return GatePolicy(policy_path)
