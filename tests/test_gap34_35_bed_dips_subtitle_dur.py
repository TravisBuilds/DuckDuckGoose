"""Test GAP-34: bed_dips and GAP-35: subtitle_dur."""
import pytest
import numpy as np
from hfvg.audio_mix import (
    validate_bed_dips, dip_envelope, normalize_plan, AudioInputError,
    SR, SUBTITLE_SPEECH_SLACK_S
)


def test_validate_bed_dips_empty():
    """validate_bed_dips accepts empty or None."""
    assert validate_bed_dips(None) == []
    assert validate_bed_dips([]) == []


def test_validate_bed_dips_single():
    """validate_bed_dips accepts a valid dip."""
    dips = validate_bed_dips([{"start": 10.0, "end": 12.0, "gain_db": -12.0}])
    assert len(dips) == 1
    assert dips[0]["start"] == 10.0
    assert dips[0]["end"] == 12.0
    assert dips[0]["gain_db"] == -12.0
    assert dips[0]["ramp_s"] == 0.15  # default


def test_validate_bed_dips_with_ramp_and_note():
    """validate_bed_dips accepts ramp_s and note."""
    dips = validate_bed_dips([{
        "start": 5.0, "end": 8.0, "gain_db": -18.0, "ramp_s": 0.3, "note": "burst"
    }])
    assert dips[0]["ramp_s"] == 0.3
    assert dips[0]["note"] == "burst"


def test_validate_bed_dips_multiple():
    """validate_bed_dips accepts multiple dips."""
    dips = validate_bed_dips([
        {"start": 1.0, "end": 2.0, "gain_db": -10.0},
        {"start": 5.0, "end": 6.0, "gain_db": -15.0},
    ])
    assert len(dips) == 2


def test_validate_bed_dips_rejects_empty_window():
    """validate_bed_dips rejects when end <= start."""
    with pytest.raises(AudioInputError) as exc_info:
        validate_bed_dips([{"start": 10.0, "end": 10.0, "gain_db": -12.0}])
    assert "window" in str(exc_info.value).lower()
    assert "empty" in str(exc_info.value).lower()


def test_validate_bed_dips_rejects_reversed_window():
    """validate_bed_dips rejects when end < start."""
    with pytest.raises(AudioInputError) as exc_info:
        validate_bed_dips([{"start": 10.0, "end": 8.0, "gain_db": -12.0}])
    assert "empty" in str(exc_info.value).lower()


def test_validate_bed_dips_rejects_invalid_gain():
    """validate_bed_dips rejects gain_db outside [-30, -0.5]."""
    with pytest.raises(AudioInputError) as exc_info:
        validate_bed_dips([{"start": 1.0, "end": 2.0, "gain_db": 0.0}])
    assert "gain_db" in str(exc_info.value)


def test_validate_bed_dips_rejects_non_dict():
    """validate_bed_dips rejects non-dict items."""
    with pytest.raises(AudioInputError) as exc_info:
        validate_bed_dips([None])
    assert "object" in str(exc_info.value)


def test_dip_envelope_no_dips():
    """dip_envelope returns all 1.0 when no dips."""
    env = dip_envelope(100, [])
    assert len(env) == 100
    assert np.all(env == 1.0)


def test_dip_envelope_single_dip():
    """dip_envelope creates a dip in the specified window."""
    n = int(20 * SR)  # 20 seconds
    dips = [{"start": 5.0, "end": 10.0, "gain_db": -12.0, "ramp_s": 0.5}]
    env = dip_envelope(n, dips)
    
    # Before dip: should be 1.0
    assert env[int(4 * SR)] == pytest.approx(1.0, abs=0.01)
    
    # During dip: should be reduced (10^(-12/20) ≈ 0.251)
    mid = int(7.5 * SR)
    expected = 10.0 ** (-12.0 / 20.0)
    assert env[mid] == pytest.approx(expected, abs=0.01)
    
    # After dip: should be 1.0
    assert env[int(15 * SR)] == pytest.approx(1.0, abs=0.01)


def test_dip_envelope_overlapping_takes_deepest():
    """dip_envelope with overlapping dips takes the deepest (most negative) gain."""
    n = int(10 * SR)
    dips = [
        {"start": 2.0, "end": 6.0, "gain_db": -6.0, "ramp_s": 0.1},
        {"start": 4.0, "end": 8.0, "gain_db": -12.0, "ramp_s": 0.1},  # Deeper
    ]
    env = dip_envelope(n, dips)
    
    # In overlap (5.0s): should use the deeper -12dB
    mid = int(5.0 * SR)
    expected = 10.0 ** (-12.0 / 20.0)
    assert env[mid] == pytest.approx(expected, abs=0.01)


def test_normalize_plan_with_subtitle_dur():
    """normalize_plan accepts subtitle_dur on VO lines."""
    plan = normalize_plan({
        "vo": {
            "lines": [
                {"id": "L01", "text": "Hello", "at": 1.0, "subtitle_dur": 2.5}
            ]
        }
    }, "/tmp")
    
    assert plan["vo"]["lines"][0]["subtitle_dur"] == 2.5


def test_normalize_plan_without_subtitle_dur():
    """normalize_plan accepts lines without subtitle_dur (None)."""
    plan = normalize_plan({
        "vo": {
            "lines": [
                {"id": "L01", "text": "Hello", "at": 1.0}
            ]
        }
    }, "/tmp")
    
    assert plan["vo"]["lines"][0]["subtitle_dur"] is None


def test_normalize_plan_with_bed_dips():
    """normalize_plan validates and includes bed_dips."""
    plan = normalize_plan({
        "vo": {"lines": []},
        "bed_dips": [
            {"start": 5.0, "end": 7.0, "gain_db": -10.0}
        ]
    }, "/tmp")
    
    assert "bed_dips" in plan
    assert len(plan["bed_dips"]) == 1
    assert plan["bed_dips"][0]["start"] == 5.0


def test_normalize_plan_without_bed_dips():
    """normalize_plan accepts plans without bed_dips (empty list)."""
    plan = normalize_plan({
        "vo": {"lines": []}
    }, "/tmp")
    
    assert plan["bed_dips"] == []


def test_subtitle_speech_slack_constant():
    """SUBTITLE_SPEECH_SLACK_S is defined."""
    assert SUBTITLE_SPEECH_SLACK_S == 0.1
