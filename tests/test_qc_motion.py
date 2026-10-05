"""Tests for G4.10 motion check."""

import pytest
from hfvg.qc.motion import create_manifest, MotionCheckResult


def test_create_manifest():
    """Test manifest creation for stillgate.py."""
    clips = [
        {"id": "A01", "seg_s": 3.75},
        {"id": "A02", "seg_s": 4.75},
        {"id": "A03", "seg_s": 3.5},
    ]
    dissolves = {"A02": 1.0}
    
    manifest = create_manifest(clips, dissolves)
    
    assert "clips" in manifest
    assert "dissolves" in manifest
    assert len(manifest["clips"]) == 3
    assert manifest["dissolves"]["A02"] == 1.0


def test_motion_check_result():
    """Test MotionCheckResult class."""
    # PASS result
    pass_result = MotionCheckResult("A01", 0.0, 3.75, 0.45, 12.5, "PASS")
    assert pass_result.is_pass()
    assert not pass_result.is_fail()
    assert not pass_result.is_watch()
    
    # FAIL result (still-only)
    fail_result = MotionCheckResult("O03", 0.0, 2.92, 0.083, 1.0, "FAIL")
    assert fail_result.is_fail()
    assert not fail_result.is_pass()
    
    # WATCH result
    watch_result = MotionCheckResult("B02", 0.0, 3.5, 0.20, 5.0, "WATCH")
    assert watch_result.is_watch()
    assert not fail_result.is_pass()


def test_motion_check_thresholds():
    """Test motion check threshold logic (from stillgate.py spec)."""
    # FAIL: peak < 3.0 AND mean < 0.15
    # WATCH: peak < 6.0 OR mean < 0.25
    
    # Test cases from spec
    fail_case = MotionCheckResult("O03", 0.0, 2.92, 0.083, 1.0, "FAIL")
    assert fail_case.mean_fd < 0.15
    assert fail_case.peak_fd < 3.0
    assert fail_case.is_fail()
    
    watch_case = MotionCheckResult("B02", 0.0, 3.5, 0.20, 5.0, "WATCH")
    assert watch_case.peak_fd < 6.0 or watch_case.mean_fd < 0.25
    assert watch_case.is_watch()
    
    pass_case = MotionCheckResult("A01", 0.0, 3.75, 0.45, 12.5, "PASS")
    assert pass_case.peak_fd >= 6.0
    assert pass_case.mean_fd >= 0.25
    assert pass_case.is_pass()
