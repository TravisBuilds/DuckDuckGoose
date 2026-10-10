"""Test that all workflow activities are registered in the production worker."""

import ast
import inspect
from pathlib import Path

import pytest

from hfvg import activities
from hfvg.worker import PRODUCTION_ACTIVITIES
from hfvg.workflows.shot import ShotWorkflow


def get_activity_names_from_list(activity_list):
    """Extract activity function names from PRODUCTION_ACTIVITIES list."""
    names = set()
    for activity_func in activity_list:
        if hasattr(activity_func, "__name__"):
            names.add(activity_func.__name__)
    return names


def get_workflow_activity_calls(workflow_class):
    """
    Extract all activity names called by workflow.execute_activity() in a workflow class.
    
    Parses the workflow source to find all execute_activity calls.
    """
    source = inspect.getsource(workflow_class)
    tree = ast.parse(source)
    
    activity_names = set()
    
    class ActivityCallVisitor(ast.NodeVisitor):
        def visit_Call(self, node):
            # Look for workflow.execute_activity calls
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "execute_activity":
                    # First argument is the activity function
                    if len(node.args) > 0:
                        arg = node.args[0]
                        if isinstance(arg, ast.Name):
                            activity_names.add(arg.id)
            self.generic_visit(node)
    
    visitor = ActivityCallVisitor()
    visitor.visit(tree)
    
    return activity_names


def test_shot_workflow_activities_registered():
    """
    Test that all activities called by ShotWorkflow are registered in PRODUCTION_ACTIVITIES.
    
    This ensures that production workers can execute all activities that workflows need.
    Mutation: remove one activity registration from PRODUCTION_ACTIVITIES.
    """
    # Get registered activity names
    registered = get_activity_names_from_list(PRODUCTION_ACTIVITIES)
    
    # Get activity names called by ShotWorkflow
    workflow_activities = get_workflow_activity_calls(ShotWorkflow)
    
    # Assert all workflow activities are registered
    missing = workflow_activities - registered
    
    assert not missing, (
        f"ShotWorkflow calls activities that are not registered in PRODUCTION_ACTIVITIES: {missing}. "
        f"Add these to PRODUCTION_ACTIVITIES in hfvg/worker.py"
    )
    
    # Verify specific critical activities for R1
    critical_activities = {
        "poll_job_status",
        "commit_job_budget",
        "release_job_budget",
        "mark_job_pending_reconcile",
    }
    
    missing_critical = critical_activities - registered
    assert not missing_critical, (
        f"Critical polling activities not registered: {missing_critical}"
    )


def test_all_imported_activities_are_registered():
    """
    Test that key activities from hfvg.activities are registered.
    
    This is a backup check to ensure we don't accidentally drop important activities.
    """
    registered = get_activity_names_from_list(PRODUCTION_ACTIVITIES)
    
    # List of activities that MUST be registered for production
    required_activities = [
        "submit_still_job_enforced",
        "submit_clip_job_enforced",
        "poll_job_status",
        "commit_job_budget",
        "release_job_budget",
        "mark_job_pending_reconcile",
        "precheck_still_qc",
        "precheck_clip_qc",
        "review_still",
        "review_clip",
        "record_shot_result",
    ]
    
    for activity_name in required_activities:
        assert activity_name in registered, (
            f"Required activity '{activity_name}' not registered in PRODUCTION_ACTIVITIES"
        )
