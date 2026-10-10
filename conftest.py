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
    
    # Set required secrets for API initialization (32+ chars)
    if "ADMIN_SECRET" not in os.environ:
        os.environ["ADMIN_SECRET"] = "test_admin_secret_minimum_32_characters_long_for_security"
    
    # Set Higgsfield API key for provider initialization
    if "HIGGSFIELD_API_KEY" not in os.environ:
        os.environ["HIGGSFIELD_API_KEY"] = "test_key_id:test_key_secret"
    
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
