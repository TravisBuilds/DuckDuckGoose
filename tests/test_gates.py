"""Tests for gate policy loader."""

import pytest

from hfvg.gates import load_policy


def test_load_policy():
    """Test loading HARNESS-GATES.json v1.1."""
    policy = load_policy("mid-mountain-rest")
    
    # Validate structure
    assert policy.schema_version == "1.1"
    assert len(policy.gates) == 71
    
    # Count blocking gates (string 'yes')
    blocking_count = sum(1 for g in policy.gates if g.get("blocking") == "yes")
    assert blocking_count == 69
    
    # Count Travis approval points
    assert len(policy.travis_approval_points) == 15
    
    # Count check_type distribution
    check_types = {"code": 0, "judge": 0, "human": 0}
    for gate in policy.gates:
        ct = gate.get("check_type")
        if ct in check_types:
            check_types[ct] += 1
    
    assert check_types["code"] == 31
    assert check_types["judge"] == 26
    assert check_types["human"] == 14


def test_gate_lookup():
    """Test gate lookup methods."""
    policy = load_policy("mid-mountain-rest")
    
    # Test get_gate
    g410 = policy.get_gate("G4.10")
    assert g410 is not None
    assert g410["check_type"] == "code"
    
    # Test is_blocking
    assert policy.is_blocking("G4.10") is True
    assert policy.is_blocking("G4.06") is False  # Non-blocking gate
    assert policy.is_blocking("G6.04") is False  # Non-blocking gate
    
    # Test requires_travis_approval
    assert policy.requires_travis_approval("G1.01") is True
    assert policy.requires_travis_approval("G1.03") is True
    assert policy.requires_travis_approval("G4.10") is False


def test_budgets():
    """Test budget configuration loading."""
    policy = load_policy("mid-mountain-rest")
    
    # Test Higgsfield budget
    hf_budget = policy.get_higgsfield_budget("ep04")
    assert hf_budget["target"] == 1000
    assert hf_budget["hard_cap"] == 1250
    assert hf_budget["stop_fraction"] == 0.8
    assert hf_budget["unit"] == "Higgsfield app credits"
    
    # Test lines
    lines = hf_budget["lines"]
    assert lines["L1_refs"] == 120
    assert lines["L2_drafts"] == 100
    assert lines["L3_final_stills"] == 230
    assert lines["L4_video"] == 300
    assert lines["L5_finalize"] == 0
    assert lines["L6_revision_reserve"] == 250
    
    # Lines should sum to 1000
    total = sum(lines.values())
    assert total == 1000
    
    # Test ElevenLabs budget
    el_budget = policy.get_elevenlabs_budget("ep04")
    assert el_budget["target"] == 3500
    assert el_budget["hard_cap"] == 4000
    assert el_budget["stop_fraction"] == 0.8
    
    # Test unit prices
    prices = policy.get_unit_prices()
    assert prices["kling3_pro_per_s"] == 1.5
    assert prices["seedance25_720p_per_s"] == 7.0
    assert prices["gpt_image_2_2k_high"] == 6.5


def test_step_filtering():
    """Test filtering gates by step."""
    policy = load_policy("mid-mountain-rest")
    
    # Get gates for step 4 (mute)
    mute_gates = policy.get_gates_for_step("4-mute")
    assert len(mute_gates) > 0
    
    # G4.10 should be in mute gates
    gate_ids = [g["gate_id"] for g in mute_gates]
    assert "G4.10" in gate_ids
    
    # Get cross-cutting gates (step "all")
    all_gates = policy.get_gates_for_step("all")
    assert len(all_gates) > 0
