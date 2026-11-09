"""pytest configuration and global fixtures."""

import pytest
import sys


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Reset login rate limiter between tests to prevent cross-test contamination."""
    # Clear rate limiter state before each test
    if 'api.main' in sys.modules:
        api_main = sys.modules['api.main']
        if hasattr(api_main, '_LOGIN_ATTEMPTS'):
            api_main._LOGIN_ATTEMPTS.clear()
    
    yield
    
    # Clear again after test
    if 'api.main' in sys.modules:
        api_main = sys.modules['api.main']
        if hasattr(api_main, '_LOGIN_ATTEMPTS'):
            api_main._LOGIN_ATTEMPTS.clear()
