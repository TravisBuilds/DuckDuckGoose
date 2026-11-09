"""Tests for member role-based authorization.

Tests that members can access episode management APIs but NOT
the spending dashboard or user administration APIs.
"""

import sys
import pytest
import os

# Set test environment
os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["DRY_RUN"] = "true"


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    """Setup test database with users."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    from hfvg.budget import BudgetLedger
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    ledger = BudgetLedger(path)
    await ledger.init_db()
    
    # Create an admin user
    admin = await user_mgr.create_user(
        name="Admin User",
        email="admin@test.com",
        role="admin",
        password="admin123"
    )
    
    # Create a member user
    member = await user_mgr.create_user(
        name="Member User",
        email="member@test.com",
        role="member",
        password="member123"
    )
    
    # Create an episode for testing
    await init_studio_db(path)
    
    yield path, admin, member


@pytest.fixture(autouse=True)
def fresh_api_module():
    """Reload API module for each test."""
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


def get_client_and_cookies(role: str, db_tuple):
    """Get test client with authenticated session."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    _, admin, member = db_tuple
    email = admin["email"] if role == "admin" else member["email"]
    password = "admin123" if role == "admin" else "member123"
    
    response = client.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200, f"Login failed: {response.text}"
    
    return client, response.cookies


@pytest.mark.asyncio
async def test_member_can_list_episodes(db_path):
    """Members can list episodes."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    response = client.get("/api/episodes", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert "episodes" in data


@pytest.mark.asyncio
async def test_member_cannot_access_dashboard(db_path):
    """Members cannot access the spending dashboard."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    response = client.get("/api/dashboard", cookies=cookies)
    assert response.status_code == 403, response.text
    
    data = response.json()
    assert "admin" in data["detail"].lower()


@pytest.mark.asyncio
async def test_member_cannot_list_users(db_path):
    """Members cannot list users."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    response = client.get("/api/users", cookies=cookies)
    assert response.status_code == 403, response.text
    
    data = response.json()
    assert "admin" in data["detail"].lower()


@pytest.mark.asyncio
async def test_member_cannot_create_users(db_path):
    """Members cannot create users."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    response = client.post(
        "/api/users",
        json={
            "name": "Evil User",
            "email": "evil@test.com",
            "role": "admin",
            "password": "password123"
        },
        cookies=cookies
    )
    assert response.status_code == 403, response.text
    
    data = response.json()
    assert "admin" in data["detail"].lower()


@pytest.mark.asyncio
async def test_member_cannot_get_user_details(db_path):
    """Members cannot view other users' details."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    _, admin, member = db_path
    
    response = client.get(f"/api/users/{admin['user_id']}", cookies=cookies)
    assert response.status_code == 403, response.text
    
    data = response.json()
    assert "admin" in data["detail"].lower()


@pytest.mark.asyncio
async def test_member_can_change_own_password(db_path):
    """Members can change their own password."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    response = client.post(
        "/api/users/me/change-password",
        json={
            "current_password": "member123",
            "new_password": "newpassword456"
        },
        cookies=cookies
    )
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert data["success"] is True
    
    # Verify can login with new password
    response = client.post("/api/login", json={
        "email": "member@test.com",
        "password": "newpassword456"
    })
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_admin_can_access_dashboard(db_path):
    """Admins can access the spending dashboard."""
    client, cookies = get_client_and_cookies("admin", db_path)
    
    response = client.get("/api/dashboard", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert "user_stats" in data
    assert "spend_summary" in data


@pytest.mark.asyncio
async def test_admin_can_list_users(db_path):
    """Admins can list users."""
    client, cookies = get_client_and_cookies("admin", db_path)
    
    response = client.get("/api/users", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert "users" in data
    assert len(data["users"]) >= 2  # admin + member


@pytest.mark.asyncio
async def test_admin_can_create_users(db_path):
    """Admins can create users."""
    client, cookies = get_client_and_cookies("admin", db_path)
    
    response = client.post(
        "/api/users",
        json={
            "name": "New User",
            "email": "newuser@test.com",
            "role": "member",
            "password": "password123"
        },
        cookies=cookies
    )
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert data["success"] is True
    assert data["user"]["email"] == "newuser@test.com"


@pytest.mark.asyncio
async def test_member_can_start_episode(db_path):
    """Members can start episodes."""
    client, cookies = get_client_and_cookies("member", db_path)
    
    # Note: This will fail validation but should pass auth
    response = client.post(
        "/api/episodes",
        json={
            "episode_id": "ep99",
            "dry_run": True
        },
        cookies=cookies
    )
    
    # Should not get 403 Forbidden (auth error)
    # May get 400/422 (validation) or 503 (Temporal not running)
    assert response.status_code != 403, response.text


@pytest.mark.asyncio
async def test_unauthenticated_user_cannot_access_any_endpoint(db_path):
    """Unauthenticated users get 401 on all protected endpoints."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    endpoints = [
        ("GET", "/api/episodes"),
        ("GET", "/api/dashboard"),
        ("GET", "/api/users"),
    ]
    
    for method, path in endpoints:
        if method == "GET":
            response = client.get(path)
        else:
            response = client.post(path)
        
        assert response.status_code == 401, f"{method} {path} should require auth"


@pytest.mark.asyncio
async def test_wrong_password_fails_login(db_path):
    """Wrong password fails login."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    response = client.post("/api/login", json={
        "email": "member@test.com",
        "password": "wrongpassword"
    })
    
    assert response.status_code == 401, response.text
    data = response.json()
    assert "invalid" in data["detail"].lower() or "password" in data["detail"].lower()


@pytest.mark.asyncio
async def test_nonexistent_user_fails_login(db_path):
    """Nonexistent user fails login."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    response = client.post("/api/login", json={
        "email": "nonexistent@test.com",
        "password": "password123"
    })
    
    assert response.status_code == 401, response.text
