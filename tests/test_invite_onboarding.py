"""Tests for invite-based user onboarding.

Tests single-use, expiring password reset tokens for:
- User invites (create user without password)
- Password reset links
- Token security (hashed storage, single-use, expiry)
"""

import sys
import pytest
import os
from datetime import datetime, timedelta, timezone

os.environ["ADMIN_SECRET"] = "test_admin_secret_at_least_32_chars_long_here"
os.environ["DRY_RUN"] = "true"


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    """Setup test database."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:3000")
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    # Create an admin user
    admin = await user_mgr.create_user(
        name="Admin User",
        email="admin@test.com",
        role="admin",
        password="admin123"
    )
    
    yield path, admin, user_mgr


@pytest.fixture(autouse=True)
def fresh_api_module():
    """Reload API module for each test."""
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


@pytest.mark.asyncio
async def test_create_user_without_password_generates_token(db_path):
    """Creating a user without password generates a reset token."""
    _, admin, user_mgr = db_path
    
    # Create user without password
    new_user = await user_mgr.create_user(
        name="Invited User",
        email="invited@test.com",
        role="member",
        password=None  # No password
    )
    
    assert new_user["user_id"].startswith("usr_")
    assert new_user["email"] == "invited@test.com"
    
    # User should not be able to login yet (no password)
    auth = await user_mgr.authenticate_password("invited@test.com", "anypassword")
    assert auth is None
    
    # Generate token for the user
    token = await user_mgr.create_password_reset_token(new_user["user_id"])
    assert token
    assert len(token) > 40  # URL-safe base64, 32 bytes = ~43 chars


@pytest.mark.asyncio
async def test_set_password_with_valid_token(db_path):
    """User can set password with valid token."""
    _, admin, user_mgr = db_path
    
    # Create user without password
    new_user = await user_mgr.create_user(
        name="Token User",
        email="token@test.com",
        role="member",
        password=None
    )
    
    # Generate token
    token = await user_mgr.create_password_reset_token(new_user["user_id"])
    
    # Set password with token
    success = await user_mgr.set_password_with_token(token, "newpassword123")
    assert success is True
    
    # User can now login with new password
    auth = await user_mgr.authenticate_password("token@test.com", "newpassword123")
    assert auth is not None
    assert auth["user_id"] == new_user["user_id"]


@pytest.mark.asyncio
async def test_token_is_single_use(db_path):
    """Token can only be used once."""
    _, admin, user_mgr = db_path
    
    new_user = await user_mgr.create_user(
        name="Single Use",
        email="singleuse@test.com",
        role="member",
        password=None
    )
    
    token = await user_mgr.create_password_reset_token(new_user["user_id"])
    
    # First use succeeds
    success = await user_mgr.set_password_with_token(token, "password1")
    assert success is True
    
    # Second use fails
    success = await user_mgr.set_password_with_token(token, "password2")
    assert success is False
    
    # Password is still the first one
    auth = await user_mgr.authenticate_password("singleuse@test.com", "password1")
    assert auth is not None
    
    auth = await user_mgr.authenticate_password("singleuse@test.com", "password2")
    assert auth is None


@pytest.mark.asyncio
async def test_token_expires_after_72_hours(db_path):
    """Token expires after the specified time."""
    _, admin, user_mgr = db_path
    
    new_user = await user_mgr.create_user(
        name="Expiry Test",
        email="expiry@test.com",
        role="member",
        password=None
    )
    
    # Create token with very short expiry
    token = await user_mgr.create_password_reset_token(new_user["user_id"], expiry_hours=0)
    
    # Token should be expired immediately (0 hour expiry)
    import asyncio
    await asyncio.sleep(0.1)  # Small delay to ensure time has passed
    
    success = await user_mgr.set_password_with_token(token, "password123")
    assert success is False


@pytest.mark.asyncio
async def test_tokens_are_stored_hashed(db_path):
    """Tokens are stored hashed, not in plaintext."""
    path, admin, user_mgr = db_path
    
    new_user = await user_mgr.create_user(
        name="Hash Test",
        email="hash@test.com",
        role="member",
        password=None
    )
    
    token = await user_mgr.create_password_reset_token(new_user["user_id"])
    
    # Check database storage
    import aiosqlite
    import hashlib
    
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT token_hash FROM password_reset_tokens WHERE user_id = ?",
            (new_user["user_id"],)
        ) as cursor:
            row = await cursor.fetchone()
            stored_hash = row["token_hash"]
    
    # Stored hash should not equal the raw token
    assert stored_hash != token
    
    # Stored hash should equal SHA256(token)
    expected_hash = hashlib.sha256(token.encode()).hexdigest()
    assert stored_hash == expected_hash


@pytest.mark.asyncio
async def test_new_token_invalidates_old_tokens(db_path):
    """Creating a new token invalidates previous unused tokens."""
    _, admin, user_mgr = db_path
    
    new_user = await user_mgr.create_user(
        name="Multi Token",
        email="multitoken@test.com",
        role="member",
        password=None
    )
    
    # Create first token
    token1 = await user_mgr.create_password_reset_token(new_user["user_id"])
    
    # Create second token (should invalidate first)
    token2 = await user_mgr.create_password_reset_token(new_user["user_id"])
    
    # First token should no longer work
    success = await user_mgr.set_password_with_token(token1, "password1")
    assert success is False
    
    # Second token should work
    success = await user_mgr.set_password_with_token(token2, "password2")
    assert success is True


@pytest.mark.asyncio
async def test_api_create_user_with_invite(db_path):
    """API endpoint creates user and returns invite URL."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    # Login as admin
    _, admin, _ = db_path
    response = client.post("/api/login", json={
        "email": "admin@test.com",
        "password": "admin123"
    })
    assert response.status_code == 200
    cookies = response.cookies
    
    # Create user without password
    response = client.post(
        "/api/users",
        json={
            "name": "API Invite User",
            "email": "apiinvite@test.com",
            "role": "member"
            # No password field
        },
        cookies=cookies
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["success"] is True
    assert "invite_url" in data
    assert "/set-password?token=" in data["invite_url"]
    assert "message" in data
    
    # Extract token from URL
    import urllib.parse
    parsed = urllib.parse.urlparse(data["invite_url"])
    params = urllib.parse.parse_qs(parsed.query)
    token = params["token"][0]
    
    # Use token to set password
    response = client.post(
        "/api/auth/set-password",
        json={
            "token": token,
            "new_password": "userpassword123"
        }
    )
    
    assert response.status_code == 200
    result = response.json()
    assert result["success"] is True


@pytest.mark.asyncio
async def test_api_admin_can_reset_user_password(db_path):
    """Admin can generate password reset link for existing user."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    path, admin, user_mgr = db_path
    
    # Create a user with a password
    user = await user_mgr.create_user(
        name="Reset Target",
        email="resettarget@test.com",
        role="member",
        password="oldpassword123"
    )
    
    client = TestClient(app)
    
    # Login as admin
    response = client.post("/api/login", json={
        "email": "admin@test.com",
        "password": "admin123"
    })
    cookies = response.cookies
    
    # Generate reset link
    response = client.post(
        f"/api/users/{user['user_id']}/reset-password",
        cookies=cookies
    )
    
    assert response.status_code == 200
    data = response.json()
    
    assert data["success"] is True
    assert "reset_url" in data
    assert data["expires_hours"] == 72
    
    # Extract token
    import urllib.parse
    parsed = urllib.parse.urlparse(data["reset_url"])
    params = urllib.parse.parse_qs(parsed.query)
    token = params["token"][0]
    
    # User can set new password with token
    response = client.post(
        "/api/auth/set-password",
        json={
            "token": token,
            "new_password": "newpassword456"
        }
    )
    
    assert response.status_code == 200
    
    # Old password no longer works
    auth = await user_mgr.authenticate_password("resettarget@test.com", "oldpassword123")
    assert auth is None
    
    # New password works
    auth = await user_mgr.authenticate_password("resettarget@test.com", "newpassword456")
    assert auth is not None


@pytest.mark.asyncio
async def test_member_cannot_reset_other_users_password(db_path):
    """Members cannot generate reset links for other users."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    path, admin, user_mgr = db_path
    
    # Create member user
    member = await user_mgr.create_user(
        name="Member User",
        email="member@test.com",
        role="member",
        password="member123"
    )
    
    client = TestClient(app)
    
    # Login as member
    response = client.post("/api/login", json={
        "email": "member@test.com",
        "password": "member123"
    })
    cookies = response.cookies
    
    # Try to generate reset link for admin
    response = client.post(
        f"/api/users/{admin['user_id']}/reset-password",
        cookies=cookies
    )
    
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invalid_token_fails(db_path):
    """Invalid token fails gracefully."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    response = client.post(
        "/api/auth/set-password",
        json={
            "token": "invalid_token_that_does_not_exist",
            "new_password": "password123"
        }
    )
    
    # Endpoint is public (no auth required), should return 400 for invalid token
    assert response.status_code == 400, f"Expected 400 but got {response.status_code}: {response.text}"
    data = response.json()
    assert "invalid" in data["detail"].lower() or "expired" in data["detail"].lower() or "used" in data["detail"].lower()


@pytest.mark.asyncio
async def test_cli_invite_mode(db_path):
    """CLI can create user with invite link."""
    import subprocess
    from pathlib import Path
    
    path, _, _ = db_path
    script_path = Path(__file__).parent.parent / "scripts" / "manage_user.py"
    
    result = subprocess.run(
        ["python3", str(script_path), "create",
         "--name", "CLI Invite",
         "--email", "cliinvite@test.com",
         "--role", "admin",
         "--invite"],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_PATH": path}
    )
    
    assert result.returncode == 0, result.stderr
    # With --invite, output should be ONLY the invite URL
    assert "/set-password?token=" in result.stdout
    # Should NOT contain user details or "User created" message
    assert "User created" not in result.stdout
    assert "API Token" not in result.stdout


@pytest.mark.asyncio
async def test_cli_reset_with_invite_mode(db_path):
    """CLI can generate reset link for existing user."""
    import subprocess
    from pathlib import Path
    
    path, admin, user_mgr = db_path
    
    # Create a user
    await user_mgr.create_user(
        name="CLI Reset",
        email="clireset@test.com",
        role="member",
        password="oldpass123"
    )
    
    script_path = Path(__file__).parent.parent / "scripts" / "manage_user.py"
    
    result = subprocess.run(
        ["python3", str(script_path), "reset-password",
         "--email", "clireset@test.com",
         "--invite"],
        capture_output=True,
        text=True,
        env={**os.environ, "DATABASE_PATH": path}
    )
    
    assert result.returncode == 0, result.stderr
    assert "Password reset link generated" in result.stdout
    assert "Reset Link" in result.stdout
    assert "/set-password?token=" in result.stdout
