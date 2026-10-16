"""Test that default budget line caps fit within episode cap."""

import pytest


def test_default_line_caps_fit_within_episode_cap():
    """
    Test: Default line caps total less than episode cap.
    
    Ensures DEFAULT_LINE_CAPS_USD_MICROS values sum to < $60 episode cap
    and < $48 stop (80% of cap).
    
    This prevents initialization failures when policy lacks line definitions.
    """
    from hfvg.budget import (
        DEFAULT_LINE_CAPS_USD_MICROS,
        get_budget_settings,
        REVISION_LINE,
    )
    from hfvg.pricing import micros_to_usd_str
    
    settings = get_budget_settings()
    
    # Sum standard lines (excludes L7_revision which is outside episode cap)
    standard_lines_total = sum(DEFAULT_LINE_CAPS_USD_MICROS.values())
    
    # Total must be under episode cap
    assert standard_lines_total < settings.episode_cap_usd_micros, (
        f"Default line caps total {micros_to_usd_str(standard_lines_total)} USD "
        f"exceeds episode cap {micros_to_usd_str(settings.episode_cap_usd_micros)} USD. "
        f"Lines: {DEFAULT_LINE_CAPS_USD_MICROS}"
    )
    
    # Total should be under episode stop (80% threshold)
    assert standard_lines_total < settings.episode_stop_usd_micros, (
        f"Default line caps total {micros_to_usd_str(standard_lines_total)} USD "
        f"exceeds episode stop {micros_to_usd_str(settings.episode_stop_usd_micros)} USD. "
        f"This will trigger stop immediately. Lines: {DEFAULT_LINE_CAPS_USD_MICROS}"
    )
    
    # Verify revision line is NOT in standard lines (it's separate)
    assert REVISION_LINE not in DEFAULT_LINE_CAPS_USD_MICROS, (
        f"{REVISION_LINE} should not be in DEFAULT_LINE_CAPS_USD_MICROS "
        "(revision reserve is outside episode cap)"
    )
    
    # Log the actual values for visibility
    print(f"Default line caps total: {micros_to_usd_str(standard_lines_total)} USD")
    print(f"Episode cap: {micros_to_usd_str(settings.episode_cap_usd_micros)} USD")
    print(f"Episode stop (80%): {micros_to_usd_str(settings.episode_stop_usd_micros)} USD")
    print(f"Buffer remaining: {micros_to_usd_str(settings.episode_cap_usd_micros - standard_lines_total)} USD")


def test_required_budget_lines_constant():
    """Test: REQUIRED_BUDGET_LINES constant exists and matches code usage."""
    from hfvg.budget import REQUIRED_BUDGET_LINES, DEFAULT_LINE_CAPS_USD_MICROS, REVISION_LINE
    
    # Required lines should include all standard lines plus revision
    assert "L1_refs" in REQUIRED_BUDGET_LINES
    assert "L2_drafts" in REQUIRED_BUDGET_LINES
    assert "L3_final_stills" in REQUIRED_BUDGET_LINES
    assert "L4_video" in REQUIRED_BUDGET_LINES
    assert REVISION_LINE in REQUIRED_BUDGET_LINES
    
    # All default lines should be in required lines (except revision which is handled separately)
    for line_name in DEFAULT_LINE_CAPS_USD_MICROS.keys():
        assert line_name in REQUIRED_BUDGET_LINES, (
            f"Line {line_name} in DEFAULT_LINE_CAPS_USD_MICROS but not in REQUIRED_BUDGET_LINES"
        )
