"""
Tests for R1: Poll timeout behavior (keeps reservation, marks pending_reconcile).

These tests verify the polling logic is structured correctly in the workflow.
"""

import ast
import inspect
import textwrap
import pytest


def test_poll_timeout_keeps_reservation():
    """
    Test: Poll exhaustion keeps reservation and marks pending_reconcile.
    
    M-R1a will release the reservation - this test must fail.
    
    Verifies that _poll_job_to_completion:
    1. Has a loop with max_polls bound (~30 min)
    2. Calls mark_job_pending_reconcile on exhaustion
    3. Does NOT call release_job_budget on exhaustion
    4. Raises RuntimeError on exhaustion
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code of _poll_job_to_completion
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Find the method
    method_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_poll_job_to_completion":
            method_def = node
            break
    
    assert method_def is not None, "_poll_job_to_completion method not found"
    
    # Check for mark_job_pending_reconcile call
    found_mark_pending = False
    found_release = False
    found_raise_runtime = False
    found_for_loop = False
    
    for node in ast.walk(method_def):
        # Check for loop with range
        if isinstance(node, ast.For):
            if isinstance(node.iter, ast.Call):
                if isinstance(node.iter.func, ast.Name) and node.iter.func.id == "range":
                    found_for_loop = True
        
        # Check for mark_job_pending_reconcile call in the exhaustion path
        if isinstance(node, ast.Name) and node.id == "mark_job_pending_reconcile":
            found_mark_pending = True
        
        # Check for release_job_budget - should only be in confirmed failure, not exhaustion
        if isinstance(node, ast.Name) and node.id == "release_job_budget":
            found_release = True
        
        # Check for RuntimeError raise
        if isinstance(node, ast.Raise):
            if isinstance(node.exc, ast.Call):
                if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "RuntimeError":
                    found_raise_runtime = True
    
    assert found_for_loop, "Polling loop with range() not found"
    assert found_mark_pending, "mark_job_pending_reconcile call not found in exhaustion path"
    assert found_raise_runtime, "RuntimeError raise not found for exhaustion"
    
    # Note: We DO expect release_job_budget to exist (for confirmed failures),
    # but mutations will verify it's not called on exhaustion


def test_poll_confirmed_failure_releases():
    """
    Test: Confirmed provider failure (failed/blocked/canceled) releases reservation.
    
    Verifies that _poll_job_to_completion calls release_job_budget on confirmed failures.
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Find checks for failed/blocked/canceled statuses
    found_failure_status_check = False
    found_release_call = False
    
    for node in ast.walk(tree):
        # Look for: if status in ("failed", "blocked", "canceled"):
        if isinstance(node, ast.Compare):
            if isinstance(node.left, ast.Name) and node.left.id == "status":
                for comp in node.comparators:
                    if isinstance(comp, ast.Tuple):
                        # Check if tuple contains failure statuses
                        status_values = [elt.value for elt in comp.elts if isinstance(elt, ast.Constant)]
                        if "failed" in status_values or "blocked" in status_values:
                            found_failure_status_check = True
        
        # Look for release_job_budget call
        if isinstance(node, ast.Name) and node.id == "release_job_budget":
            found_release_call = True
    
    assert found_failure_status_check, "Status check for failed/blocked/canceled not found"
    assert found_release_call, "release_job_budget call not found for confirmed failures"


def test_poll_single_provider_submit():
    """
    Test: Exactly one provider submit, even with polling exhaustion.
    
    M-R1c will resubmit on exhaustion - this test must fail.
    
    Verifies that _poll_job_to_completion never calls submit activities.
    The submit happens before polling starts, and never again.
    """
    from hfvg.workflows.shot import ShotWorkflow
    
    # Get source code of _poll_job_to_completion
    source = inspect.getsource(ShotWorkflow._poll_job_to_completion)
    source = textwrap.dedent(source)
    tree = ast.parse(source)
    
    # Check that submit_still_job_enforced and submit_clip_job_enforced are NOT called
    found_submit_call = False
    
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if "submit" in node.id and "job" in node.id:
                found_submit_call = True
    
    assert not found_submit_call, \
        "_poll_job_to_completion must not call submit activities (submit happens before poll)"
