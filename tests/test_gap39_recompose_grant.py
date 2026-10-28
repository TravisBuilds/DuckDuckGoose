"""Test GAP-39: reviewer-granted extra recompose."""
import pytest
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2


def test_recompose_allowance_default():
    """recompose_allowance returns MAX_RECOMPOSES_PER_SHOT when no grants."""
    workflow = EpisodeWorkflowV2()
    assert workflow.recompose_allowance("A01") == 2  # MAX_RECOMPOSES_PER_SHOT


def test_recompose_allowance_with_one_grant():
    """recompose_allowance adds granted count."""
    workflow = EpisodeWorkflowV2()
    workflow.recompose_grants["A01"] = 1
    assert workflow.recompose_allowance("A01") == 3  # 2 + 1


def test_recompose_allowance_with_max_grants():
    """recompose_allowance adds all grants up to MAX_RECOMPOSE_GRANTS_PER_SHOT."""
    workflow = EpisodeWorkflowV2()
    workflow.recompose_grants["A01"] = 2  # MAX_RECOMPOSE_GRANTS_PER_SHOT
    assert workflow.recompose_allowance("A01") == 4  # 2 + 2


def test_grant_recompose_problem_not_at_final_cut():
    """_grant_recompose_problem rejects when not at G_FINAL_CUT."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G2_SCENE_STILLS"
    workflow.launched_shot_plans["A01"] = {}
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem == "only while parked at G_FINAL_CUT"


def test_grant_recompose_problem_final_cut_passed():
    """_grant_recompose_problem rejects when G_FINAL_CUT already passed."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.passed_gates.append("G_FINAL_CUT")
    workflow.launched_shot_plans["A01"] = {}
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem == "only while parked at G_FINAL_CUT"


def test_grant_recompose_problem_already_approved():
    """_grant_recompose_problem rejects when final cut already approved."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.approved_g_final_cut = True
    workflow.launched_shot_plans["A01"] = {}
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem == "final cut already approved"


def test_grant_recompose_problem_no_reason():
    """_grant_recompose_problem rejects when reason is empty."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    problem = workflow._grant_recompose_problem("A01", "")
    assert problem == "a reason is required"


def test_grant_recompose_problem_unknown_shot():
    """_grant_recompose_problem rejects for a shot that wasn't launched."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    problem = workflow._grant_recompose_problem("A99", "test reason")
    assert problem == "unknown shot (no still launched in this episode)"


def test_grant_recompose_problem_allowance_not_used():
    """_grant_recompose_problem rejects when shot still has recomposes left."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    workflow.recompose_counts["A01"] = 1  # Used 1 out of 2
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem == "the shot still has a recompose left; no grant needed"


def test_grant_recompose_problem_max_grants_reached():
    """_grant_recompose_problem rejects when MAX_RECOMPOSE_GRANTS_PER_SHOT reached."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    workflow.recompose_counts["A01"] = 4  # Used all including grants
    workflow.recompose_grants["A01"] = 2  # MAX_RECOMPOSE_GRANTS_PER_SHOT
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem == "max recompose grants per shot (2) reached"


def test_grant_recompose_problem_allows_when_valid():
    """_grant_recompose_problem returns None when grant is valid."""
    workflow = EpisodeWorkflowV2()
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    workflow.recompose_counts["A01"] = 2  # Used all allowance
    problem = workflow._grant_recompose_problem("A01", "test reason")
    assert problem is None


def test_recompose_problem_shows_grant_hint():
    """_recompose_problem hints at grant_recompose when allowance is used."""
    workflow = EpisodeWorkflowV2()
    workflow.approved_g2_scene_stills = True
    workflow.passed_gates.append("G2_SCENE_STILLS")
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    workflow.recompose_counts["A01"] = 2  # Used all
    problem = workflow._recompose_problem("A01", "reason", "prompt", ["ref1"])
    assert "max recomposes per shot (2) reached" in problem
    assert "grant_recompose" in problem


def test_recompose_problem_no_hint_after_grant():
    """_recompose_problem doesn't hint at grant when grant was already used."""
    workflow = EpisodeWorkflowV2()
    workflow.approved_g2_scene_stills = True
    workflow.passed_gates.append("G2_SCENE_STILLS")
    workflow.state.current_gate = "G_FINAL_CUT"
    workflow.launched_shot_plans["A01"] = {}
    workflow.recompose_counts["A01"] = 3  # Used all including 1 grant
    workflow.recompose_grants["A01"] = 1
    problem = workflow._recompose_problem("A01", "reason", "prompt", ["ref1"])
    assert "max recomposes per shot (3) reached" in problem
    assert "grant_recompose" not in problem  # No hint since allowance > base
