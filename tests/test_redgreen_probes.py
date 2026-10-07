"""
Probe tests to verify red-green classifier correctly identifies failure types.

These tests are designed to fail in specific ways to test the JUnit XML classifier.
"""

import pytest


def test_probe_control_passes():
    """Control: should always pass."""
    assert True


def test_probe_assertion_fails():
    """Should pass normally, fail with AssertionError when mutated."""
    assert True, "This should pass"


def test_probe_attribute_error():
    """Should pass normally, fail with AttributeError when mutated (wrong reason)."""
    obj = object()  # Normal: has attributes
    _ = getattr(obj, "__class__", None)  # This works


def test_probe_runtime_chained_from_assert():
    """Should pass normally, fail with RuntimeError when mutated (wrong reason)."""
    assert True, "assertion passes"
