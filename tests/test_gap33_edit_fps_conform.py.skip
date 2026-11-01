"""Test GAP-33: edit fps conform."""
import pytest
from temporalio.exceptions import ApplicationError
from hfvg.edit_plan import edit_fps


def test_edit_fps_defaults_to_30():
    """edit_fps returns 30 (EDIT_FPS) when edit_plan is None or missing fps."""
    assert edit_fps(None) == 30
    assert edit_fps({}) == 30
    assert edit_fps({"order": ["A01"]}) == 30


def test_edit_fps_accepts_24():
    """edit_fps accepts 24 fps from edit plan."""
    assert edit_fps({"fps": 24}) == 24


def test_edit_fps_accepts_30():
    """edit_fps accepts explicit 30 fps from edit plan."""
    assert edit_fps({"fps": 30}) == 30


def test_edit_fps_refuses_invalid_int():
    """edit_fps raises ApplicationError for fps not in EDIT_FPS_ALLOWED."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": 60})
    assert exc_info.value.type == "EditPlanMismatch"
    assert "60" in str(exc_info.value)
    assert "not allowed" in str(exc_info.value)


def test_edit_fps_refuses_zero():
    """edit_fps raises ApplicationError for zero fps."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": 0})
    assert exc_info.value.type == "EditPlanMismatch"


def test_edit_fps_refuses_negative():
    """edit_fps raises ApplicationError for negative fps."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": -24})
    assert exc_info.value.type == "EditPlanMismatch"


def test_edit_fps_refuses_float():
    """edit_fps raises ApplicationError for float fps."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": 24.5})
    assert exc_info.value.type == "EditPlanMismatch"


def test_edit_fps_refuses_string():
    """edit_fps raises ApplicationError for string fps."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": "24"})
    assert exc_info.value.type == "EditPlanMismatch"


def test_edit_fps_refuses_bool():
    """edit_fps raises ApplicationError for bool fps."""
    with pytest.raises(ApplicationError) as exc_info:
        edit_fps({"fps": True})
    assert exc_info.value.type == "EditPlanMismatch"
