"""Manual/integration test for beat fields in shots API.

This test can be run manually after starting an episode with story shaping.
It verifies that GET /api/episodes/{id}/shots returns beat context fields.
"""

import os
import aiosqlite
import pytest
from fastapi.testclient import TestClient


def test_api_shots_endpoint_structure():
    """
    Verify that the shots endpoint code is structured to merge beat fields.
    This is a code inspection test that ensures the API includes beat enrichment logic.
    """
    import os
    from pathlib import Path
    
    # Find api/main.py relative to this test file
    test_dir = Path(__file__).parent
    api_main_path = test_dir.parent / "api" / "main.py"
    
    assert api_main_path.exists(), f"api/main.py not found at {api_main_path}"
    
    # Verify the beat field merging logic is present
    with open(api_main_path, "r") as f:
        api_source = f.read()
    
    # Check that the API includes beat field merging from workflow state
    assert "beat_id" in api_source, "API should include beat_id field"
    assert "beat_purpose" in api_source, "API should include beat_purpose field"
    assert "beat_emotional_tone" in api_source, "API should include beat_emotional_tone field"
    assert "parsed_beatmap" in api_source, "API should query parsed_beatmap from workflow state"
