"""
CI guard: Ensure no mutations leaked into product code.
"""

import pytest
from pathlib import Path


def test_no_mutated_markers():
    """Fail if 'MUTATED' appears in product code."""
    repo_root = Path(__file__).parent.parent
    product_dirs = ["hfvg", "api"]
    
    violations = []
    for dir_name in product_dirs:
        dir_path = repo_root / dir_name
        if not dir_path.exists():
            continue
        
        for py_file in dir_path.rglob("*.py"):
            content = py_file.read_text()
            for line_num, line in enumerate(content.splitlines(), 1):
                if "MUTATED" in line:
                    violations.append(f"{py_file.relative_to(repo_root)}:{line_num}: {line.strip()}")
    
    assert not violations, f"Found MUTATED markers in product code:\n" + "\n".join(violations)


def test_episode_workflow_gates_not_commented():
    """Ensure all EpisodeWorkflowV2 wait_condition calls are active."""
    repo_root = Path(__file__).parent.parent
    workflow_file = repo_root / "hfvg" / "workflows" / "episode_v2.py"
    
    if not workflow_file.exists():
        pytest.skip("episode_v2.py not found")
    
    content = workflow_file.read_text()
    lines = content.splitlines()
    
    violations = []
    for line_num, line in enumerate(lines, 1):
        # Check for commented-out wait_condition
        if "#" in line and "await workflow.wait_condition" in line:
            # Verify it's actually commented out
            stripped = line.strip()
            if stripped.startswith("#") and "wait_condition" in stripped:
                violations.append(f"Line {line_num}: {line.strip()}")
    
    assert not violations, f"Found commented wait_condition in EpisodeWorkflowV2:\n" + "\n".join(violations)


def test_all_gates_have_wait_condition():
    """Ensure current_gate assignments are followed by wait_condition."""
    repo_root = Path(__file__).parent.parent
    workflow_file = repo_root / "hfvg" / "workflows" / "episode_v2.py"
    
    if not workflow_file.exists():
        pytest.skip("episode_v2.py not found")
    
    content = workflow_file.read_text()
    lines = content.splitlines()
    
    violations = []
    for line_num, line in enumerate(lines, 1):
        if "self.state.current_gate = " in line and '"' in line:
            # Extract gate name
            gate_name = line.split('"')[1]
            
            # Check next few lines for wait_condition
            found_wait = False
            for offset in range(1, 5):
                if line_num + offset <= len(lines):
                    next_line = lines[line_num + offset - 1]
                    if "await workflow.wait_condition" in next_line and not next_line.strip().startswith("#"):
                        found_wait = True
                        break
                    # Stop if we hit another gate or passed_gates
                    if "current_gate = " in next_line or "passed_gates.append" in next_line:
                        break
            
            if not found_wait:
                violations.append(f"Gate {gate_name} at line {line_num} has no active wait_condition")
    
    assert not violations, f"Gates missing wait_condition:\n" + "\n".join(violations)
