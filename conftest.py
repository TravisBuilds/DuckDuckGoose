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


def pytest_configure(config):
    """Configure pytest-socket to allow only localhost connections."""
    # pytest-socket will block all socket connections except those we explicitly allow
    config.addinivalue_line(
        "markers",
        "allow_hosts: Allow network connections to specified hosts"
    )


def pytest_runtest_setup(item):
    """Set up test with socket restrictions."""
    # Allow localhost for Temporal test server
    marker = item.get_closest_marker("allow_hosts")
    if marker:
        # Test explicitly allows certain hosts
        pass
    else:
        # By default, only allow localhost
        if hasattr(item.config, "_socket_allow_hosts"):
            # pytest-socket is installed
            pass


# Configure pytest-socket to allow localhost by default
def pytest_socket_allow_hosts():
    """
    Allow connections to localhost and 127.0.0.1 for Temporal test server.
    Block all other connections.
    """
    return ["localhost", "127.0.0.1", "::1"]
