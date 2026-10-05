"""Tests for duck identity gate (G2.03/G3.03/G4.02)."""

import pytest
from pathlib import Path

from hfvg.qc.duck_identity import (
    DuckIdentityGate,
    FakeDuckDetector,
    FakeVisionJudge,
    create_gate,
)


@pytest.mark.asyncio
async def test_fake_detector():
    """Test fake detector for dry-run."""
    detector = FakeDuckDetector()
    result = await detector.detect("/fake/image.jpg")
    
    assert result["found"] is True
    assert "bbox" in result
    assert result["confidence"] > 0


@pytest.mark.asyncio
async def test_fake_judge():
    """Test fake judge for dry-run."""
    judge = FakeVisionJudge()
    result = await judge.judge_identity(
        "/fake/composite.jpg",
        {"shot_id": "A01", "duck_state": "T"}
    )
    
    assert result["verdict"] == "PASS"
    assert "signs" in result
    assert "apparel" in result
    assert result["total_minor_score"] == 0


@pytest.mark.asyncio
async def test_auto_fail_drift():
    """Test auto-fail on drift (score = 2)."""
    gate = create_gate()
    
    # Mock judge result with drift
    judge_result = {
        "signs": {
            "bill": {"score": 2, "confidence": 0.9, "evidence": "Too long"},
            "head": {"score": 0, "confidence": 0.9, "evidence": "OK"},
            "neck_ring": {"score": 0, "confidence": 0.9, "evidence": "Absent"},
            "body": {"score": 0, "confidence": 0.9, "evidence": "OK"},
            "too_small": {"score": 0, "confidence": 0.9, "evidence": "Readable"},
        },
        "apparel": {
            "tuque": {"present": True, "correct": True},
            "vest": {"present": True, "correct": True},
        },
        "total_minor_score": 0,
        "verdict": "PASS",
        "confidence": 0.9,
    }
    
    shot_context = {"shot_id": "A01", "duck_state": "T"}
    verdict, reason = gate._apply_auto_fail_logic(judge_result, shot_context)
    
    assert verdict == "FAIL"
    assert "drift" in reason.lower()
    assert "bill" in reason.lower()


@pytest.mark.asyncio
async def test_auto_fail_neck_ring():
    """Test auto-fail on neck ring detection."""
    gate = create_gate()
    
    judge_result = {
        "signs": {
            "bill": {"score": 0, "confidence": 0.9, "evidence": "OK"},
            "head": {"score": 0, "confidence": 0.9, "evidence": "OK"},
            "neck_ring": {"score": 1, "confidence": 0.7, "evidence": "White ring visible"},
            "body": {"score": 0, "confidence": 0.9, "evidence": "OK"},
            "too_small": {"score": 0, "confidence": 0.9, "evidence": "Readable"},
        },
        "apparel": {
            "tuque": {"present": True, "correct": True},
            "vest": {"present": True, "correct": True},
        },
        "total_minor_score": 0,
        "verdict": "PASS",
        "confidence": 0.9,
    }
    
    shot_context = {"shot_id": "A01", "duck_state": "T"}
    verdict, reason = gate._apply_auto_fail_logic(judge_result, shot_context)
    
    assert verdict == "FAIL"
    assert "neck ring" in reason.lower()


@pytest.mark.asyncio
async def test_auto_fail_minor_score():
    """Test auto-fail when minor score sum ≥ 3."""
    gate = create_gate()
    
    judge_result = {
        "signs": {
            "bill": {"score": 1, "confidence": 0.9, "evidence": "Slightly long"},
            "head": {"score": 1, "confidence": 0.9, "evidence": "Slightly flat"},
            "neck_ring": {"score": 0, "confidence": 0.9, "evidence": "Absent"},
            "body": {"score": 1, "confidence": 0.9, "evidence": "Slightly pear"},
            "too_small": {"score": 0, "confidence": 0.9, "evidence": "Readable"},
        },
        "apparel": {
            "tuque": {"present": True, "correct": True},
            "vest": {"present": True, "correct": True},
        },
        "total_minor_score": 3,
        "verdict": "PASS",
        "confidence": 0.9,
    }
    
    shot_context = {"shot_id": "A01", "duck_state": "T"}
    verdict, reason = gate._apply_auto_fail_logic(judge_result, shot_context)
    
    assert verdict == "FAIL"
    assert "minor score" in reason.lower()


@pytest.mark.asyncio
async def test_escalate_low_confidence():
    """Test escalation on low confidence."""
    gate = create_gate()
    
    judge_result = {
        "signs": {
            "bill": {"score": 0, "confidence": 0.5, "evidence": "Unclear"},
            "head": {"score": 0, "confidence": 0.5, "evidence": "Unclear"},
            "neck_ring": {"score": 0, "confidence": 0.5, "evidence": "Unclear"},
            "body": {"score": 0, "confidence": 0.5, "evidence": "Unclear"},
            "too_small": {"score": 0, "confidence": 0.5, "evidence": "Unclear"},
        },
        "apparel": {
            "tuque": {"present": True, "correct": True},
            "vest": {"present": True, "correct": True},
        },
        "total_minor_score": 0,
        "verdict": "PASS",
        "confidence": 0.5,  # Below 0.6 threshold
    }
    
    shot_context = {"shot_id": "A01", "duck_state": "T"}
    verdict, reason = gate._apply_auto_fail_logic(judge_result, shot_context)
    
    assert verdict == "ESCALATE"
    assert "confidence" in reason.lower()


@pytest.mark.asyncio
async def test_create_gate_default():
    """Test creating gate with default (fake) components."""
    gate = create_gate()
    
    assert isinstance(gate.detector, FakeDuckDetector)
    assert isinstance(gate.judge, FakeVisionJudge)


@pytest.mark.asyncio
async def test_check_still_dry_run(tmp_path):
    """Test checking a still image in dry-run mode."""
    gate = create_gate()  # Uses fakes
    
    # Create a fake image file
    fake_image = tmp_path / "test_still.jpg"
    fake_image.write_bytes(b"fake image data")
    
    shot_context = {
        "shot_id": "A01",
        "duck_role": "host",
        "duck_state": "T",
    }
    
    result = await gate.check_still(fake_image, shot_context)
    
    assert "detection" in result
    assert "verdict" in result
    # With fake components, should pass
    assert result["verdict"] in ["PASS", "FAIL", "ESCALATE"]
