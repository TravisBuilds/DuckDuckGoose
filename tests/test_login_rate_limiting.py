"""Tests for login rate limiting."""

import sys
import pytest
import os

os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["DRY_RUN"] = "true"


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    """Setup test database."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    # Create a test user
    await user_mgr.create_user(
        name="Test User",
        email="test@example.com",
        role="member",
        password="correct_password"
    )
    
    yield path


@pytest.fixture(autouse=True)
def fresh_api_module():
    """Reload API module for each test."""
    # Clear the login attempts dict between tests
    if 'api.main' in sys.modules:
        api_main = sys.modules['api.main']
        if hasattr(api_main, '_LOGIN_ATTEMPTS'):
            api_main._LOGIN_ATTEMPTS.clear()
    
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


@pytest.mark.asyncio
async def test_successful_login_not_rate_limited(db_path):
    """Successful logins are not rate limited."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    # Multiple successful logins should work
    for _ in range(10):
        response = client.post("/api/login", json={
            "email": "test@example.com",
            "password": "correct_password"
        })
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_failed_login_triggers_rate_limit(db_path):
    """Failed logins trigger rate limiting after MAX_LOGIN_ATTEMPTS."""
    from fastapi.testclient import TestClient
    from api.main import app, MAX_LOGIN_ATTEMPTS
    
    client = TestClient(app)
    
    # Make MAX_LOGIN_ATTEMPTS failed attempts
    for i in range(MAX_LOGIN_ATTEMPTS):
        response = client.post("/api/login", json={
            "email": "test@example.com",
            "password": "wrong_password"
        })
        assert response.status_code == 401, f"Attempt {i+1} should fail with 401"
    
    # Next attempt should be rate limited
    response = client.post("/api/login", json={
        "email": "test@example.com",
        "password": "wrong_password"
    })
    assert response.status_code == 429, "Should be rate limited"
    assert "too many" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_rate_limit_is_per_email(db_path):
    """Rate limiting is per email address."""
    from fastapi.testclient import TestClient
    from api.main import app, MAX_LOGIN_ATTEMPTS
    from hfvg.users import UserManager
    
    # Create another user
    user_mgr = UserManager(db_path)
    await user_mgr.create_user(
        name="User2",
        email="user2@example.com",
        role="member",
        password="password2"
    )
    
    client = TestClient(app)
    
    # Exhaust attempts for first user
    for _ in range(MAX_LOGIN_ATTEMPTS):
        response = client.post("/api/login", json={
            "email": "test@example.com",
            "password": "wrong"
        })
        assert response.status_code == 401
    
    # First user should be locked
    response = client.post("/api/login", json={
        "email": "test@example.com",
        "password": "wrong"
    })
    assert response.status_code == 429
    
    # Second user should still be able to login
    response = client.post("/api/login", json={
        "email": "user2@example.com",
        "password": "password2"
    })
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_successful_login_clears_failed_attempts(db_path):
    """Successful login clears the failed attempt counter."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    # Make a few failed attempts
    for _ in range(3):
        response = client.post("/api/login", json={
            "email": "test@example.com",
            "password": "wrong"
        })
        assert response.status_code == 401
    
    # Successful login should clear counter
    response = client.post("/api/login", json={
        "email": "test@example.com",
        "password": "correct_password"
    })
    assert response.status_code == 200
    
    # Should be able to make more failed attempts before being locked
    for _ in range(3):
        response = client.post("/api/login", json={
            "email": "test@example.com",
            "password": "wrong"
        })
        assert response.status_code == 401  # Not 429


@pytest.mark.asyncio
async def test_admin_secret_not_rate_limited(db_path):
    """Admin secret login is not rate limited."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    
    # Admin secret logins should never be rate limited
    for _ in range(10):
        response = client.post("/api/login", json={
            "admin_secret": admin_secret
        })
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_case_insensitive_rate_limiting(db_path):
    """Rate limiting is case-insensitive for emails."""
    from fastapi.testclient import TestClient
    from api.main import app, MAX_LOGIN_ATTEMPTS
    
    client = TestClient(app)
    
    # Make failed attempts with different casing
    attempts = [
        "test@example.com",
        "Test@Example.com",
        "TEST@EXAMPLE.COM",
        "TeSt@ExAmPlE.cOm",
    ]
    
    for i, email in enumerate(attempts * (MAX_LOGIN_ATTEMPTS // len(attempts) + 1)):
        if i >= MAX_LOGIN_ATTEMPTS:
            break
        response = client.post("/api/login", json={
            "email": email,
            "password": "wrong"
        })
        assert response.status_code == 401
    
    # All variants should now be locked
    for email in attempts:
        response = client.post("/api/login", json={
            "email": email,
            "password": "wrong"
        })
        assert response.status_code == 429
