"""
Pytest configuration for Studio tests.

Network guard: Block all real network calls except localhost (for Temporal test server).
"""

import pytest
import os


@pytest.fixture(scope="session", autouse=True)
def configure_test_environment():
    """Configure test environment variables."""
    # Force DRY_RUN mode for tests
    os.environ["DRY_RUN"] = "true"
    os.environ["DATABASE_PATH"] = ":memory:"
    
    yield
    
    # Cleanup
    if "DATABASE_PATH" in os.environ and os.environ["DATABASE_PATH"] == ":memory:":
        del os.environ["DATABASE_PATH"]


@pytest.fixture(scope="session", autouse=True)
def socket_allow_hosts():
    """
    Allow connections to localhost for Temporal test server.
    Block all other connections (pytest-socket).
    """
    return ["localhost", "127.0.0.1", "::1"]
