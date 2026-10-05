"""Tests for playbook rule validators."""

import pytest

from hfvg.playbook import PlaybookValidator, PlaybookViolation


def test_credit_cap_enforcement():
    """Test credit cap stops at 80% threshold."""
    validator = PlaybookValidator(credit_target=1200.0, credit_cap=1500.0)
    
    # 70% should pass
    validator.validate_credit_cap("round1", 1050.0)
    validator.record_spend("round1", 1050.0)
    
    # 85% should fail
    with pytest.raises(PlaybookViolation) as exc:
        validator.validate_credit_cap("round2", 1275.0)
    
    assert "80%" in str(exc.value)


def test_draft_first_enforcement():
    """Test draft-first tier validation."""
    validator = PlaybookValidator()
    
    # Final without draft approval should fail
    shot = {
        "shot_id": "A01",
        "tier": "final",
        "draft_approved": False
    }
    
    errors = validator.validate_draft_first(shot)
    assert len(errors) == 1
    assert "draft" in errors[0].lower()
    assert "approval" in errors[0].lower()
    
    # Draft without QC should fail
    shot = {
        "shot_id": "A02",
        "tier": "draft",
        "qc_passed": False
    }
    
    errors = validator.validate_draft_first(shot)
    assert len(errors) == 1
    assert "qc" in errors[0].lower()


def test_same_resort_rule():
    """Test ONE-RESORT RULE enforcement."""
    validator = PlaybookValidator()
    world_refs = ["img_resort_lobby", "img_resort_pool"]
    
    # Room plate without world refs should fail
    shot = {
        "shot_id": "R01",
        "type": "room_plate",
        "refs": ["img_random"],
        "tags": []
    }
    
    errors = validator.validate_same_resort(shot, world_refs)
    assert len(errors) == 1
    assert "resort" in errors[0].lower()
    
    # Room plate with world ref should pass
    shot = {
        "shot_id": "R02",
        "type": "room_plate",
        "refs": ["img_resort_lobby", "img_props"],
        "tags": []
    }
    
    errors = validator.validate_same_resort(shot, world_refs)
    assert len(errors) == 0
    
    # Pre-resort scene exempt
    shot = {
        "shot_id": "O01",
        "type": "room_plate",
        "refs": ["img_wild_spring"],
        "tags": ["pre-resort"]
    }
    
    errors = validator.validate_same_resort(shot, world_refs)
    assert len(errors) == 0


def test_duck_role_validation():
    """Test duck-as-host rule."""
    validator = PlaybookValidator()
    
    # Guest-only shot without "no duck" negative should fail
    shot = {
        "shot_id": "A01",
        "duck_role": "absent",
        "prompt": "Four penguins standing together",
        "tags": ["guest-only"]
    }
    
    errors = validator.validate_duck_role(shot)
    assert len(errors) == 1
    assert "no duck" in errors[0].lower()
    
    # Guest-only shot with "no duck" should pass
    shot = {
        "shot_id": "A02",
        "duck_role": "absent",
        "prompt": "Four penguins standing together, no duck in frame",
        "tags": ["guest-only"]
    }
    
    errors = validator.validate_duck_role(shot)
    assert len(errors) == 0
    
    # Duck-in-pack should fail
    shot = {
        "shot_id": "A03",
        "prompt": "Group selfie",
        "tags": ["duck-in-pack"]
    }
    
    errors = validator.validate_duck_role(shot)
    assert len(errors) == 1
    assert "pack" in errors[0].lower()


def test_animal_poses_validation():
    """Test NO UPRIGHT/CARRYING rule."""
    validator = PlaybookValidator()
    
    # Upright guest animal should fail
    shot = {
        "shot_id": "A01",
        "prompt": "Guest monkey standing upright holding a bag",
        "tags": ["guest"]
    }
    
    errors = validator.validate_animal_poses(shot)
    assert len(errors) >= 1
    assert any("upright" in e.lower() or "holding" in e.lower() for e in errors)
    
    # Quadrupedal pose should pass
    shot = {
        "shot_id": "A02",
        "prompt": "Guest monkey on all fours, bundle tied on back",
        "tags": ["guest"]
    }
    
    errors = validator.validate_animal_poses(shot)
    assert len(errors) == 0


def test_moderation_routing():
    """Test moderation-safe routing recommendations."""
    validator = PlaybookValidator()
    
    # Wet content → Kling
    shot = {
        "shot_id": "C01",
        "tags": ["wet", "pool"]
    }
    
    routing = validator.validate_moderation_routing(shot)
    assert routing["model"] == "kling_3.0"
    assert "wet" in routing["reason"].lower() or "kling" in routing["reason"].lower()
    
    # Multi-reference → Seedance
    shot = {
        "shot_id": "F02",
        "tags": [],
        "refs": ["ref1", "ref2", "ref3"]
    }
    
    routing = validator.validate_moderation_routing(shot)
    assert routing["model"] == "seedance_2.5"


def test_validate_shot_all_rules():
    """Test running all validations on a shot."""
    validator = PlaybookValidator()
    world_refs = ["img_resort_lobby"]
    
    # Shot with multiple violations
    shot = {
        "shot_id": "A01",
        "tier": "final",
        "draft_approved": False,  # Violation 1
        "type": "room_plate",
        "refs": ["img_random"],  # Violation 2 (no world ref)
        "tags": ["guest-only"],
        "duck_role": "absent",
        "prompt": "Guest monkey standing upright"  # Violation 3 & 4
    }
    
    errors = validator.validate_shot(shot, world_refs)
    assert len(errors) >= 3  # Multiple violations detected
