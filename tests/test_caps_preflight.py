"""Tests for preflight validation of shots."""
import pytest
from hfvg import model_caps, preflight
from hfvg.activities.studio_generation import assemble_still_refs
from hfvg.config import max_refs_for


def test_still_over_cap_refused_with_creator_message():
    """T7: Grok shot with 6 refs gets error with creator message."""
    registry = model_caps.get_registry()
    
    shot = {
        "id": "A02",
        "start_prompt": "Test prompt",
        "ref_keys": ["ref1", "ref2", "ref3", "ref4", "ref5", "ref6"],
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "9:16",
    }
    route = {"still_model": "xai/grok-imagine-image-2.0"}
    
    violations = preflight.preflight_shot(shot, route, registry, phase="start")
    
    errors = [v for v in violations if v.severity == "error"]
    assert len(errors) == 1, f"Expected 1 error, got {len(errors)}"
    
    error = errors[0]
    assert error.field == "max_refs"
    assert "6 reference images" in error.message
    assert "at most 5" in error.message
    assert "Remove 1" in error.message
    assert "A02" in error.message
    
    # exactly at the cap (5) is allowed
    at_cap = dict(shot, ref_keys=["ref1", "ref2", "ref3", "ref4", "ref5"])
    assert not [v for v in preflight.preflight_shot(at_cap, route, registry, phase="start") if v.field == "max_refs"]
    # the anchor counts on a start still (no lead: never dropped): 5 planned + anchor = 6
    with_anchor = preflight.preflight_shot(at_cap, dict(route, anchor_ref="anchor"), registry, phase="start")
    assert [v.actual for v in with_anchor if v.field == "max_refs"] == [6], with_anchor

    # Check source info
    formatted = preflight.format_violation(error)
    assert "Limit source: docs.x.ai, checked 2026-" in formatted, formatted


def test_end_still_counts_after_anchor_drop():
    """T8: lead + anchor + 4 planned -> 5 after drop -> no violation; 6 -> error."""
    registry = model_caps.get_registry()
    cap = 5  # Grok cap
    
    # Case 1: lead + anchor + 4 planned -> 5 after anchor drop -> OK
    # Preflight should compute this using assemble_still_refs internally
    shot1 = {
        "id": "A02_end",
        "start_prompt": "Test",
        "ref_keys": ["r1", "r2", "r3", "r4"],  # 4 planned refs
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "9:16",
    }
    route = {"still_model": "xai/grok-imagine-image-2.0", "anchor_ref": "anchor_still"}
    
    # For end frame, preflight adds lead ref internally
    # lead + anchor + 4 planned = 6, but anchor drops -> 5 (OK)
    violations1 = preflight.preflight_shot(shot1, route, registry, phase="end")
    errors1 = [v for v in violations1 if v.severity == "error" and v.field == "max_refs"]
    assert len(errors1) == 0
    
    # Case 2: lead + anchor + 5 planned = 7, anchor drops -> 6 (still over)
    shot2 = {
        "id": "A02_end",
        "start_prompt": "Test",
        "ref_keys": ["r1", "r2", "r3", "r4", "r5"],  # 5 planned refs
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "9:16",
    }
    violations2 = preflight.preflight_shot(shot2, route, registry, phase="end")
    errors2 = [v for v in violations2 if v.severity == "error" and v.field == "max_refs"]
    assert len(errors2) == 1
    
    # Also verify the direct assembly logic
    refs1, anchor_used1 = assemble_still_refs(
        lead="start_still",
        anchor="anchor_still",
        planned=["r1", "r2", "r3", "r4"],
        cap=cap,
    )
    assert len(refs1) == 5
    assert not anchor_used1
    
    refs2, anchor_used2 = assemble_still_refs(
        lead="start_still",
        anchor="anchor_still",
        planned=["r1", "r2", "r3", "r4", "r5"],
        cap=cap,
    )
    assert len(refs2) == 6
    assert not anchor_used2


def test_prompt_limit_kling_refused_unknown_grok_allowed():
    """T9: Kling 2501 chars -> error; grok 10000 -> allowed (unknown)."""
    registry = model_caps.get_registry()
    
    # Kling: 2501 chars should be refused (limit 2500)
    long_prompt = "a" * 2501
    shot_kling = {
        "id": "A01",
        "clip_prompt": long_prompt,
        "duration": 5,
        "start_frame": "frame.jpg",
    }
    route_kling = {"clip_model": "kling-video/v3.0/pro/image-to-video"}
    violations_kling = preflight.preflight_shot(shot_kling, route_kling, registry, phase="clip")
    
    errors_kling = [v for v in violations_kling if v.severity == "error" and v.field == "prompt_max_chars"]
    assert len(errors_kling) == 1
    assert "2501" in errors_kling[0].message
    assert "2500" in errors_kling[0].message
    assert "Shorten it by 1" in errors_kling[0].message
    
    # Grok: 10000 chars should be allowed (unknown limit)
    very_long_prompt = "a" * 10000
    shot_grok = {
        "id": "A02",
        "start_prompt": very_long_prompt,
        "ref_keys": [],
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "9:16",
    }
    route_grok = {"still_model": "xai/grok-imagine-image-2.0"}
    violations_grok = preflight.preflight_shot(shot_grok, route_grok, registry, phase="start")
    
    errors_grok = [v for v in violations_grok if v.field == "prompt_max_chars"]
    assert len(errors_grok) == 0  # Unknown limit doesn't refuse


def test_duration_rules():
    """T10: Kling 2/16/4.5 -> errors; 3,15 -> ok; Seedance 3 -> error, 30 -> ok, 31 -> error."""
    registry = model_caps.get_registry()
    
    # Kling: duration must be 3-15 integer seconds
    # Test duration 2 (too low)
    shot_kling_low = {
        "id": "A01",
        "clip_prompt": "test",
        "duration": 2,
        "start_frame": "frame.jpg",
    }
    route_kling = {"clip_model": "kling-video/v3.0/pro/image-to-video"}
    violations = preflight.preflight_shot(shot_kling_low, route_kling, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration"]
    assert len(errors) == 1
    assert "3" in errors[0].message and "15" in errors[0].message
    
    # Test duration 16 (too high)
    shot_kling_high = shot_kling_low.copy()
    shot_kling_high["duration"] = 16
    violations = preflight.preflight_shot(shot_kling_high, route_kling, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration"]
    assert len(errors) == 1
    
    # 4.5 s: Kling takes whole seconds and the harness sends int(duration) -> a warning naming the
    # duration actually sent, not an error (submit_clip_job_enforced truncates for Kling)
    shot_kling_float = shot_kling_low.copy()
    shot_kling_float["duration"] = 4.5
    violations = preflight.preflight_shot(shot_kling_float, route_kling, registry, phase="clip")
    found = [v for v in violations if v.field == "duration"]
    assert [v.severity for v in found] == ["warning"], found
    assert "whole seconds" in found[0].message and "sent as 4 s" in found[0].message
    # 3.5 s is sent as 3 s (ok); 2.9 s is sent as 2 s -> refused
    shot_kling_float["duration"] = 2.9
    found = [v for v in preflight.preflight_shot(shot_kling_float, route_kling, registry, phase="clip")
             if v.field == "duration" and v.severity == "error"]
    assert len(found) == 1 and "sent as 2 s" in found[0].message, found
    
    # Test durations 3 and 15 (OK)
    for dur in [3, 15]:
        shot_ok = shot_kling_low.copy()
        shot_ok["duration"] = dur
        violations = preflight.preflight_shot(shot_ok, route_kling, registry, phase="clip")
        errors = [v for v in violations if v.field == "duration"]
        assert len(errors) == 0
    
    # Seedance: duration must be 4-30 integer seconds
    route_seedance = {"clip_model": "bytedance/seedance-2.5/image-to-video"}
    
    # Test 3 (too low)
    shot_seedance = shot_kling_low.copy()
    shot_seedance["duration"] = 3
    violations = preflight.preflight_shot(shot_seedance, route_seedance, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration"]
    assert len(errors) == 1
    
    # Seedance refuses fractional seconds (the submit path raises, it does not truncate)
    shot_seedance["duration"] = 4.5
    violations = preflight.preflight_shot(shot_seedance, route_seedance, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration" and v.severity == "error"]
    assert len(errors) == 1 and "whole number" in errors[0].message, violations

    # Test 30 (OK)
    shot_seedance["duration"] = 30
    violations = preflight.preflight_shot(shot_seedance, route_seedance, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration"]
    assert len(errors) == 0
    
    # Test 31 (too high)
    shot_seedance["duration"] = 31
    violations = preflight.preflight_shot(shot_seedance, route_seedance, registry, phase="clip")
    errors = [v for v in violations if v.field == "duration"]
    assert len(errors) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
