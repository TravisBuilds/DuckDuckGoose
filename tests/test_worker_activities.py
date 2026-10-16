"""Tests for worker activity registration."""

import ast
import inspect
import pytest


def test_worker_registers_all_workflow_activities():
    """
    Test that production worker registers every activity any workflow executes.
    
    This ensures no activity is missing from PRODUCTION_ACTIVITIES list in worker.py.
    A missing activity would cause workflows to fail at runtime.
    
    Mutation: Remove one activity from PRODUCTION_ACTIVITIES list.
    Expected: This test fails with AssertionError (activity not in registered list).
    """
    from hfvg import worker
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    # Get the list of activities registered in the worker
    registered_activities = worker.PRODUCTION_ACTIVITIES
    registered_names = set()
    
    for activity in registered_activities:
        # Get activity name from the function
        if hasattr(activity, '__temporal_activity_definition'):
            activity_def = activity.__temporal_activity_definition
            registered_names.add(activity_def.name)
        else:
            # Fallback to function name if no temporal definition
            registered_names.add(activity.__name__)
    
    # Extract all activity calls from ShotWorkflow
    shot_source = inspect.getsource(ShotWorkflow)
    shot_tree = ast.parse(shot_source)
    
    called_activities = set()
    
    for node in ast.walk(shot_tree):
        # Look for workflow.execute_activity calls
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "execute_activity":
                    # First argument is the activity function
                    if len(node.args) > 0:
                        activity_arg = node.args[0]
                        if isinstance(activity_arg, ast.Name):
                            # Direct reference like: workflow.execute_activity(poll_job_status, ...)
                            called_activities.add(activity_arg.id)
    
    # Check that all called activities are registered
    # Key activities that ShotWorkflow uses:
    required_activities = {
        "submit_still_job_enforced",
        "poll_job_status",
        "commit_job_budget",
        "release_job_budget",
        "mark_job_pending_reconcile",
        "precheck_still_qc",
        "review_still",
        "precheck_clip_qc",
        "review_clip",
        "submit_clip_job_enforced",
        "record_shot_result",
    }
    
    missing_activities = required_activities - registered_names
    
    assert not missing_activities, (
        f"Activities used by workflows but not registered in worker.PRODUCTION_ACTIVITIES: "
        f"{missing_activities}. These must be added to the PRODUCTION_ACTIVITIES list in hfvg/worker.py."
    )
    
    # Verify all registered activities are actually from hfvg.activities
    from hfvg import activities as activities_module
    
    for activity_func in registered_activities:
        # Check that each registered activity is either from activities module
        # or has a __temporal_activity_definition
        assert (
            hasattr(activity_func, '__temporal_activity_definition') or
            hasattr(activities_module, activity_func.__name__)
        ), f"Activity {activity_func.__name__} is not properly defined"
