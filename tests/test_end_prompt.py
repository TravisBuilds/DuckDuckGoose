"""Tests for end_prompt support in shots.json."""

import pytest
import json
from pathlib import Path

from hfvg.shot_data import parse_shots, ShotDataError


def test_end_prompt_optional():
    """Test: end_prompt is optional in shots.json."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["alice"],
                "wet": False,
                "model": "kling",
                "duration": 5,
                "end_frame": False,
                "prompt": "A woman walking",
                "ref_keys": [],
            }
        ]
    }
    
    plans = parse_shots(data)
    assert "A01" in plans
    assert plans["A01"]["prompt"] == "A woman walking"
    assert plans["A01"]["end_prompt"] is None


def test_end_prompt_string():
    """Test: end_prompt is parsed when provided."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["alice"],
                "wet": False,
                "model": "kling",
                "duration": 5,
                "end_frame": True,
                "prompt": "A woman walking starts",
                "end_prompt": "A woman walking ends",
                "ref_keys": [],
            }
        ]
    }
    
    plans = parse_shots(data)
    assert plans["A01"]["prompt"] == "A woman walking starts"
    assert plans["A01"]["end_prompt"] == "A woman walking ends"


def test_end_prompt_must_be_string():
    """Test: end_prompt must be a string if provided."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["alice"],
                "wet": False,
                "model": "kling",
                "duration": 5,
                "end_frame": True,
                "prompt": "A woman walking",
                "end_prompt": 123,  # Invalid: number instead of string
                "ref_keys": [],
            }
        ]
    }
    
    with pytest.raises(ShotDataError, match="end_prompt must be string"):
        parse_shots(data)


def test_end_prompt_cannot_be_empty():
    """Test: end_prompt cannot be empty string."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["alice"],
                "wet": False,
                "model": "kling",
                "duration": 5,
                "end_frame": True,
                "prompt": "A woman walking",
                "end_prompt": "   ",  # Invalid: empty/whitespace only
                "ref_keys": [],
            }
        ]
    }
    
    with pytest.raises(ShotDataError, match="empty end_prompt"):
        parse_shots(data)


def test_end_prompt_used_for_end_frame():
    """Test: _end still uses end_prompt when provided, falls back to prompt."""
    # This would need to be tested in the workflow/activity tests
    # where we verify that when end_frame=True and end_prompt is set,
    # the _end still generation uses end_prompt instead of prompt
    pass
