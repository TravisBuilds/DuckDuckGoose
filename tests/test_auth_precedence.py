"""Tests for auth precedence with malformed Authorization headers and sessions.

Tests issue: when a request carries a malformed or non-session Authorization header
(e.g. reverse-proxy Basic auth) plus a valid member session, admin routes must
return 403 for members (not 401), and 401 only with no valid session.
"""

import os
import sys
import pytest

# Set test tokens BEFORE any other imports
os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"

from fastapi.testclient import TestClient


@pytest.fixture
async def client(tmp_path, monkeypatch):
    """Test client with fresh app state and initialized database."""
    # Setup database path
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize database
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    from hfvg.budget import BudgetLedger
    from hfvg.cost_tracking import CostTracker
    
    await init_studio_db(db_path)
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    cost_tracker = CostTracker(db_path)
    await cost_tracker.init_db()
    
    # Clear any cached module
    if 'api.main' in sys.modules:
        del sys.modules['api.main']
    
    # Import after setting env vars
    from api.main import app
    return TestClient(app)


@pytest.fixture
async def member_session(client):
    """Create a member session cookie."""
    # Login as admin first to create the session infrastructure
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    admin_cookies = response.cookies
    
    # Create a member user via the API
    response = client.post(
        "/api/dashboard/users",
        json={
            "name": "Test Member",
            "email": "member@example.com",
            "role": "member",
            "password": "member_password_123"
        },
        cookies=admin_cookies
    )
    assert response.status_code == 200
    
    # Now login as the member using password
    # We need to create a member session token manually since login endpoint
    # might only support admin secret. Let's create it via the DB.
    import aiosqlite
    from api.main import DATABASE_PATH, create_session
    
    # Create member session token
    member_token = await create_session(user_id="member_test", role="member")
    
    return {"studio_admin_token": member_token}


@pytest.mark.asyncio
async def test_basic_auth_with_valid_admin_session_succeeds(client):
    """Basic Authorization header is ignored when valid admin session exists."""
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    
    # First login to get admin session
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    admin_cookies = response.cookies
    
    # Now make request with both Basic auth (malformed) AND valid admin session
    # Use dashboard endpoint which is an admin-only GET route
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Basic some_base64_encoded_credentials"},
        cookies=admin_cookies
    )
    
    # Should succeed because admin session is valid (ignore malformed Basic header)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_basic_auth_with_valid_member_session_returns_403(client, tmp_path, monkeypatch):
    """Basic Authorization header is ignored; valid member session returns 403 on admin route."""
    # Setup database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    from api.main import create_session
    
    await init_studio_db(db_path)
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    # Create member session
    member_token = await create_session(user_id="member_test", role="member")
    
    # Make request with both Basic auth (malformed) AND valid member session
    # Use dashboard endpoint which is an admin-only GET route
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Basic some_base64_encoded_credentials"},
        cookies={"studio_admin_token": member_token}
    )
    
    # Should return 403 (authenticated but not authorized), NOT 401
    # The Basic header is ignored, and the member session is processed
    assert response.status_code == 403, f"Expected 403 for member on admin route, got {response.status_code}: {response.text}"
    detail = response.json().get("detail", "")
    assert "admin" in detail.lower() or "required" in detail.lower()


@pytest.mark.asyncio
async def test_basic_auth_with_no_session_returns_401(client):
    """Basic Authorization header with no session returns 401."""
    # Make request with only Basic auth (no session cookie)
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Basic some_base64_encoded_credentials"}
    )
    
    # Should return 401 (unauthenticated) because no valid credential
    assert response.status_code == 401
    detail = response.json().get("detail", "")
    assert "required" in detail.lower() or "authentication" in detail.lower()


@pytest.mark.asyncio
async def test_invalid_bearer_with_valid_admin_session_succeeds(client):
    """Invalid Bearer token is rejected, but valid admin session succeeds."""
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    
    # First login to get admin session
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    admin_cookies = response.cookies
    
    # Now make request with both invalid Bearer token AND valid admin session
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Bearer invalid_token_here_wrong"},
        cookies=admin_cookies
    )
    
    # With the fix, this should succeed because admin session is valid
    # The invalid Bearer token should not override the valid session
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_invalid_bearer_with_valid_member_session_returns_403(client, tmp_path, monkeypatch):
    """Invalid Bearer token with valid member session returns 403 (not 401)."""
    # Setup database
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    from api.main import create_session
    
    await init_studio_db(db_path)
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    # Create member session
    member_token = await create_session(user_id="member_test", role="member")
    
    # Make request with both invalid Bearer token AND valid member session
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Bearer invalid_token_here_wrong"},
        cookies={"studio_admin_token": member_token}
    )
    
    # Should return 403 (authenticated but not authorized), NOT 401
    # The member session should take precedence over the invalid Bearer token
    assert response.status_code == 403, f"Expected 403 for member with invalid bearer, got {response.status_code}: {response.text}"


@pytest.mark.asyncio
async def test_invalid_bearer_with_no_session_returns_403(client):
    """Invalid Bearer token with no session returns 403."""
    # Make request with only invalid Bearer token (no session cookie)
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": "Bearer invalid_token_here_wrong"}
    )
    
    # Should return 403 (invalid secret)
    assert response.status_code == 403
    detail = response.json().get("detail", "")
    assert "invalid" in detail.lower() or "secret" in detail.lower()


@pytest.mark.asyncio
async def test_valid_bearer_always_succeeds(client):
    """Valid Bearer token always succeeds regardless of session."""
    admin_secret = "test_admin_secret_at_least_32_chars_long_here"
    
    # Make request with valid Bearer token (no session)
    response = client.get(
        "/api/dashboard",
        headers={"Authorization": f"Bearer {admin_secret}"}
    )
    
    # Should succeed
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_auth_returns_401(client):
    """No authentication at all returns 401."""
    # Make request with no auth headers and no cookies
    response = client.get("/api/dashboard")
    
    # Should return 401
    assert response.status_code == 401
    detail = response.json().get("detail", "")
    assert "required" in detail.lower() or "authentication" in detail.lower()
