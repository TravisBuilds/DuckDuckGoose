"""Test GAP-32: recompose shot_meta validation and headcount."""
import pytest
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


def test_recompose_meta_problem_none():
    """recompose_meta_problem accepts None."""
    assert EpisodeWorkflowV2.recompose_meta_problem(None) is None


def test_recompose_meta_problem_empty():
    """recompose_meta_problem accepts empty dict."""
    assert EpisodeWorkflowV2.recompose_meta_problem({}) is None


def test_recompose_meta_problem_allowed_keys():
    """recompose_meta_problem accepts allowed keys (count, characters, duck)."""
    assert EpisodeWorkflowV2.recompose_meta_problem({"count": 2}) is None
    assert EpisodeWorkflowV2.recompose_meta_problem({"characters": ["Alice"]}) is None
    assert EpisodeWorkflowV2.recompose_meta_problem({"duck": True}) is None
    assert EpisodeWorkflowV2.recompose_meta_problem({"count": 1, "duck": False}) is None


def test_recompose_meta_problem_rejects_disallowed_key():
    """recompose_meta_problem rejects keys not in (count, characters, duck)."""
    problem = EpisodeWorkflowV2.recompose_meta_problem({"location": "kitchen"})
    assert problem is not None
    assert "location" in problem
    assert "not allowed" in problem


def test_recompose_meta_problem_rejects_multiple_disallowed():
    """recompose_meta_problem rejects multiple disallowed keys."""
    problem = EpisodeWorkflowV2.recompose_meta_problem({
        "count": 2,
        "location": "kitchen",
        "time": "morning"
    })
    assert problem is not None
    assert "location" in problem
    assert "time" in problem


def test_recompose_meta_problem_rejects_non_dict():
    """recompose_meta_problem rejects non-dict."""
    problem = EpisodeWorkflowV2.recompose_meta_problem("invalid")
    assert problem is not None
    assert "object" in problem


def test_recompose_problem_checks_shot_meta():
    """_recompose_problem validates shot_meta via recompose_meta_problem."""
    workflow = EpisodeWorkflowV2()
    workflow.approved_g2_scene_stills = True
    workflow.passed_gates.append("G2_SCENE_STILLS")
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    
    # Valid shot_meta: no problem
    problem = workflow._recompose_problem("A01", "reason", "prompt", ["ref1"], {"count": 2})
    assert problem is None
    
    # Invalid shot_meta: problem returned
    problem = workflow._recompose_problem("A01", "reason", "prompt", ["ref1"], {"invalid": "key"})
    assert problem is not None
    assert "shot_meta" in problem
    assert "invalid" in problem


def test_recompose_still_plans_query():
    """recompose_still_plans returns shot metadata for headcount check."""
    workflow = EpisodeWorkflowV2()
    
    # Verify the method is registered as a workflow query
    from temporalio import workflow as wf
    assert hasattr(workflow.recompose_still_plans, "__temporal_query_definition"), \
        "recompose_still_plans must be decorated with @workflow.query"
    
    # Empty when no recomposes
    assert workflow.recompose_still_plans() == {}
    
    # Returns plan with metadata
    workflow.recompose_plans["A01"] = {
        "version": 3,
        "shot_meta": {"count": 2, "duck": True},
        "recompose": {"n": 1}
    }
    workflow.pending_recompose["A01"] = {
        "version": 3,
        "still_ready": True,
        "decision": None
    }
    
    plans = workflow.recompose_still_plans()
    assert "A01" in plans
    assert plans["A01"]["version"] == 3
    assert plans["A01"]["shot_meta"] == {"count": 2, "duck": True}
    assert plans["A01"]["still_ready"] is True
    assert plans["A01"]["finished"] is False
