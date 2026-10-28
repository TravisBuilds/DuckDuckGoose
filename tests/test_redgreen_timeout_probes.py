"""
Redgreen timeout probes: verify hung baseline and hung mutant are classified as TIMEOUT.

These tests intentionally hang to prove the timeout detection works correctly.
T1: hung baseline should be TIMEOUT (not INVALID "syntax error")
T2: hung mutant should be TIMEOUT (not FAIL "mutation ineffective")
"""
import time
import pytest


@pytest.mark.timeout(180)
def test_hung_baseline():
    """T1 probe: baseline hangs forever (timeout probe)."""
    # This test will hang and timeout - proving baseline TIMEOUT classification
    time.sleep(999)
    assert False, "Should not reach here"


@pytest.mark.timeout(180)
def test_hung_mutant():
    """T2 probe: mutant hangs when mutated."""
    pass  # baseline is fast
    assert True
