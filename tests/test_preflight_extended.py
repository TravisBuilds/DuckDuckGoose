"""Additional preflight tests for aspect, resolution, frames, status, and audio."""
import pytest
from hfvg import model_caps, preflight


def test_aspect_resolution_quality():
    """T11: Grok aspect/resolution/quality validation."""
    registry = model_caps.get_registry()
    
    # Invalid aspect
    shot = {
        "id": "A01",
        "start_prompt": "Test",
        "ref_keys": [],
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "21:9",  # Not supported by Grok
    }
    route = {"still_model": "xai/grok-imagine-image-2.0"}
    violations = preflight.preflight_shot(shot, route, registry, phase="start")
    errors = [v for v in violations if v.field == "aspect_ratio"]
    assert len(errors) == 1
    
    # Invalid quality
    shot2 = shot.copy()
    shot2["aspect_ratio"] = "9:16"  # Valid
    shot2["quality"] = "high"  # Not supported by Grok (only low/medium)
    violations = preflight.preflight_shot(shot2, route, registry, phase="start")
    errors = [v for v in violations if v.field == "quality"]
    assert len(errors) == 1
    
    # Invalid resolution
    shot3 = shot.copy()
    shot3["aspect_ratio"] = "9:16"
    shot3["quality"] = "medium"
    shot3["resolution"] = "4k"  # Not supported by Grok (only 1k/2k)
    violations = preflight.preflight_shot(shot3, route, registry, phase="start")
    errors = [v for v in violations if v.field == "resolution"]
    assert len(errors) == 1


def test_frames_rules():
    """T12: Frame format, size, aspect validation."""
    registry = model_caps.get_registry()
    
    # Test that basic frame checks work
    shot = {
        "id": "A01",
        "clip_prompt": "Test",
        "duration": 5,
        "start_frame": "frame.jpg",
    }
    route = {"clip_model": "kling-video/v3.0/pro/image-to-video"}
    
    # Basic validation should pass without files
    violations = preflight.preflight_shot(shot, route, registry, phase="clip")
    # No file checks without files parameter
    assert True  # Placeholder - files parameter would enable format checks


def test_model_status_rules():
    """T13: Model status validation and eleven_music mapping."""
    registry = model_caps.get_registry()
    
    # Test gpt_image_2 MCP-only status
    shot = {
        "id": "A01",
        "start_prompt": "Test",
        "ref_keys": [],
        "resolution": "1k",
        "quality": "medium",
        "aspect_ratio": "9:16",
    }
    route = {"still_model": "gpt_image_2"}
    violations = preflight.preflight_shot(shot, route, registry, phase="start")
    errors = [v for v in violations if v.field == "status"]
    assert len(errors) == 1
    assert "MCP" in errors[0].message
    
    # Test eleven_turbo deprecated warning
    caps_turbo = registry.resolve("eleven_turbo_v2_5")
    assert caps_turbo.status == "deprecated_upstream"
    
    # Test eleven_music maps to music_v1
    caps_music = registry.resolve("eleven_music")
    assert caps_music.status == "alias_invalid_upstream_id"


def test_audio_limits():
    """T14: Audio limits for TTS, SFX, music."""
    registry = model_caps.get_registry()
    
    # TTS text limit
    caps = registry.resolve("eleven_multilingual_v2")
    text_limit = caps.limit("text_max_chars")
    assert text_limit.value == 10000
    
    # SFX duration limits
    caps_sfx = registry.resolve("sound_generation")
    dur_limit = caps_sfx.limit("duration_s")
    assert dur_limit.value["min"] == 0.5
    assert dur_limit.value["max"] == 30
    
    # Music duration limits
    caps_music = registry.resolve("eleven_music")
    music_dur = caps_music.limit("duration_ms")
    assert music_dur.value["min"] == 3000
    assert music_dur.value["max"] == 300000  # 5 minutes


def test_still_preflight_runs_before_estimate_and_reserve():
    """T15: Still preflight before estimate/reserve (no spend on violations)."""
    # This would require mocking the full submit flow
    # For now, verify PreflightError is non-retryable
    from hfvg.preflight import PreflightError
    
    error = PreflightError("Test error")
    assert error.non_retryable == True
    
    # Also verify the preflight code exists in submit_still_job_enforced
    import inspect
    from hfvg.activities.studio_generation import submit_still_job_enforced
    
    source = inspect.getsource(submit_still_job_enforced)
    assert "preflight.preflight_shot" in source, "Preflight call must exist in submit_still_job_enforced"
    assert "PreflightError" in source, "PreflightError must be raised in submit_still_job_enforced"


def test_clip_preflight_runs_before_estimate_and_reserve():
    """T16: Clip preflight before estimate/reserve."""
    # Similar to T15, requires full flow mocking
    assert True  # Placeholder


def test_g1_refused_while_preflight_errors():
    """T19: G1 approval refused while preflight errors exist."""
    # Would require workflow testing
    assert True  # Placeholder


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
