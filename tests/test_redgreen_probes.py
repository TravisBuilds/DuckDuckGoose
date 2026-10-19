"""
Probe tests to verify red-green classifier correctly identifies failure types.

These tests are designed to fail in specific ways to test the JUnit XML classifier.
"""

import pytest
import httpx
import respx


def test_probe_positive_control():
    """Positive control: should pass normally, fail with AssertionError when mutated."""
    value = 42
    assert value == 42, "Value should be 42"


class TestHelper:
    """Helper class for P1 test."""
    
    def working_method(self):
        """This method will be deleted by the P1 mutation."""
        return "success"


def test_probe_p1_attribute_error():
    """Should pass normally, fail with AttributeError when mutated (wrong reason)."""
    helper = TestHelper()
    result = helper.working_method()
    assert result == "success", "Method should return success"


@respx.mock
def test_probe_p2_respx_unmocked():
    """Should pass normally, fail with respx unmocked request when mutated (wrong reason)."""
    # Mock the endpoint
    respx.get("http://example.com/api/data").mock(return_value=httpx.Response(200, json={"status": "ok"}))
    
    # Make the request
    response = httpx.get("http://example.com/api/data")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_probe_p3_runtime_during_assert():
    """Should pass normally, fail with RuntimeError during AssertionError handling (wrong reason)."""
    value = 100
    # This will be mutated to raise RuntimeError during except
    assert value == 100, "Value should be 100"
