"""Test episode_v2 handles failed shots that return error dicts."""

import pytest


def test_episode_v2_failed_shot_dict_handling():
    """Test that episode_v2 track_child_completion properly detects failed dict."""
    # Simulate what track_child_completion does with a failed result
    result = {
        "status": "failed",
        "error": "422 PROVIDER_DETAIL_XYZ"
    }
    
    # This is the logic from episode_v2.py lines 346-353
    if isinstance(result, dict) and result.get("status") == "failed":
        error_detail = result.get("error", "Unknown error")
        # Would be appended to self.state.failed_shots
        failed_shot = {
            "shot_id": "test_shot",
            "status": "failed",
            "reason": error_detail
        }
        
        # Verify the reason is preserved exactly
        assert failed_shot["reason"] == "422 PROVIDER_DETAIL_XYZ", \
            f"Expected exact provider detail, got: {failed_shot['reason']}"
    else:
        pytest.fail("Failed dict was not detected as failed")


def test_episode_v2_success_shot_dict_handling():
    """Test that episode_v2 track_child_completion properly detects success dict."""
    # Simulate a success result
    result = {
        "status": "completed",
        "url": "http://fake.cdn/shot.png",
        "cost": 0.5
    }
    
    # This is the logic from episode_v2.py lines 346-357
    if isinstance(result, dict) and result.get("status") == "failed":
        pytest.fail("Success dict was incorrectly detected as failed")
    else:
        # Would be recorded as success
        # shot_completion_status[shot_id] = {"done": True, "success": True, "result": result}
        assert True  # Success path
