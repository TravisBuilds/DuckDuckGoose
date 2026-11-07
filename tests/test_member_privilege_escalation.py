"""Test that members cannot access admin routes (privilege escalation blocker #2)."""

import pytest
import os


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test_priv.db")


def _client(monkeypatch, db_path):
    """Create test client with proper DATABASE_PATH."""
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


async def _init_db(db_path):
    """Initialize database for tests (correct init order)."""
    from hfvg.users import UserManager
    from hfvg.cost_tracking import CostTracker
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import init_studio_db
    
    await init_studio_db(db_path)
    
    # Budget ledger first (creates job_ledger)
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    # Then cost tracker (migrates job_ledger)
    tracker = CostTracker(db_path)
    await tracker.init_db()
    
    # Then users
    mgr = UserManager(db_path)
    await mgr.init_db()


@pytest.mark.asyncio
async def _member_login(client, db_path):
    """Create a member user and log in, returning cookies."""
    # Initialize database
    await _init_db(db_path)
    
    # Create admin session first
    admin_response = client.post("/api/login", json={"admin_secret": "a" * 32})
    assert admin_response.status_code == 200
    admin_cookies = admin_response.cookies
    
    # Create a member user
    create_response = client.post(
        "/api/users",
        json={
            "name": "Test Member",
            "email": "member@test.com",
            "role": "member",
            "password": "memberpass123"
        },
        cookies=admin_cookies
    )
    assert create_response.status_code == 200
    
    # Log out admin
    client.post("/api/logout", cookies=admin_cookies)
    
    # Log in as member
    member_response = client.post(
        "/api/login",
        json={"email": "member@test.com", "password": "memberpass123"}
    )
    assert member_response.status_code == 200
    assert member_response.json()["user"]["role"] == "member"
    return member_response.cookies


@pytest.mark.asyncio
async def test_member_cannot_access_dashboard(db_path, monkeypatch):
    """Test that member gets 403 on GET /api/dashboard."""
    client = _client(monkeypatch, db_path)
    member_cookies = await _member_login(client, db_path)
    
    response = client.get("/api/dashboard", cookies=member_cookies)
    assert response.status_code == 403
    assert "Admin access required" in response.text


@pytest.mark.asyncio
async def test_member_cannot_list_users(db_path, monkeypatch):
    """Test that member gets 403 on GET /api/users."""
    client = _client(monkeypatch, db_path)
    member_cookies = await _member_login(client, db_path)
    
    response = client.get("/api/users", cookies=member_cookies)
    assert response.status_code == 403
    assert "Admin access required" in response.text


@pytest.mark.asyncio
async def test_member_cannot_create_user(db_path, monkeypatch):
    """Test that member gets 403 on POST /api/users."""
    client = _client(monkeypatch, db_path)
    member_cookies = await _member_login(client, db_path)
    
    response = client.post(
        "/api/users",
        json={
            "name": "New User",
            "email": "new@test.com",
            "role": "member",
            "password": "pass123"
        },
        cookies=member_cookies
    )
    assert response.status_code == 403
    assert "Admin access required" in response.text


@pytest.mark.asyncio
async def test_member_cannot_view_user_details(db_path, monkeypatch):
    """Test that member gets 403 on GET /api/users/{user_id}."""
    client = _client(monkeypatch, db_path)
    member_cookies = await _member_login(client, db_path)
    
    response = client.get("/api/users/usr_test123", cookies=member_cookies)
    assert response.status_code == 403
    assert "Admin access required" in response.text


@pytest.mark.asyncio
async def test_admin_can_access_all_routes(db_path, monkeypatch):
    """Test that admin can access admin routes."""
    # Initialize database
    await _init_db(db_path)
    
    client = _client(monkeypatch, db_path)
    
    # Log in as admin
    admin_response = client.post("/api/login", json={"admin_secret": "a" * 32})
    assert admin_response.status_code == 200
    assert admin_response.json()["user"]["role"] == "admin"
    admin_cookies = admin_response.cookies
    
    # Dashboard - should succeed
    response = client.get("/api/dashboard", cookies=admin_cookies)
    assert response.status_code == 200
    
    # List users - should succeed
    response = client.get("/api/users", cookies=admin_cookies)
    assert response.status_code == 200
    
    # Create user - should succeed
    response = client.post(
        "/api/users",
        json={
            "name": "New User",
            "email": "admin_new@test.com",
            "role": "member",
            "password": "pass123"
        },
        cookies=admin_cookies
    )
    assert response.status_code == 200
