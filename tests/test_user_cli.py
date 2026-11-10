"""Tests for user management CLI."""

import pytest
import subprocess
import os
from pathlib import Path


@pytest.fixture
async def db_path(tmp_path):
    """Setup test database."""
    path = str(tmp_path / "test.db")
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    yield path


def run_cli(*args, env=None, input_data=None):
    """Run CLI command and return result."""
    script_path = Path(__file__).parent.parent / "scripts" / "manage_user.py"
    
    env_vars = os.environ.copy()
    if env:
        env_vars.update(env)
    
    result = subprocess.run(
        ["python3", str(script_path)] + list(args),
        capture_output=True,
        text=True,
        env=env_vars,
        input=input_data
    )
    
    return result


@pytest.mark.asyncio
async def test_cli_create_user_with_stdin_password(db_path):
    """Create user with password from stdin."""
    result = run_cli(
        "create",
        "--name", "Test User",
        "--email", "test@example.com",
        "--role", "member",
        env={"DATABASE_PATH": db_path},
        input_data="password123\n"
    )
    
    assert result.returncode == 0, result.stderr
    assert "User created successfully" in result.stdout
    assert "User ID:" in result.stdout
    assert "API Token:" in result.stdout
    
    # Verify user was created
    from hfvg.users import UserManager
    user_mgr = UserManager(db_path)
    
    user = await user_mgr.authenticate_password("test@example.com", "password123")
    assert user is not None
    assert user["name"] == "Test User"
    assert user["role"] == "member"


@pytest.mark.asyncio
async def test_cli_create_user_with_env_password(db_path):
    """Create user with password from environment variable."""
    result = run_cli(
        "create",
        "--name", "Env User",
        "--email", "env@example.com",
        "--role", "admin",
        env={
            "DATABASE_PATH": db_path,
            "USER_PASSWORD": "envpassword123"
        }
    )
    
    assert result.returncode == 0, result.stderr
    assert "User created successfully" in result.stdout
    
    # Verify user was created
    from hfvg.users import UserManager
    user_mgr = UserManager(db_path)
    
    user = await user_mgr.authenticate_password("env@example.com", "envpassword123")
    assert user is not None
    assert user["role"] == "admin"


@pytest.mark.asyncio
async def test_cli_create_duplicate_user_fails(db_path):
    """Creating duplicate user fails."""
    # Create first user
    result = run_cli(
        "create",
        "--name", "First User",
        "--email", "duplicate@example.com",
        "--role", "member",
        env={
            "DATABASE_PATH": db_path,
            "USER_PASSWORD": "password123"
        }
    )
    assert result.returncode == 0, result.stderr
    
    # Try to create duplicate
    result = run_cli(
        "create",
        "--name", "Second User",
        "--email", "duplicate@example.com",
        "--role", "member",
        env={
            "DATABASE_PATH": db_path,
            "USER_PASSWORD": "password456"
        }
    )
    
    assert result.returncode != 0
    assert "already exists" in result.stderr.lower()


@pytest.mark.asyncio
async def test_cli_list_users(db_path):
    """List users."""
    # Create some users first
    from hfvg.users import UserManager
    user_mgr = UserManager(db_path)
    
    await user_mgr.create_user("Alice", "alice@example.com", "admin", "pass123")
    await user_mgr.create_user("Bob", "bob@example.com", "member", "pass456")
    
    result = run_cli(
        "list",
        env={"DATABASE_PATH": db_path}
    )
    
    assert result.returncode == 0, result.stderr
    assert "Alice" in result.stdout
    assert "Bob" in result.stdout
    assert "alice@example.com" in result.stdout
    assert "bob@example.com" in result.stdout
    assert "admin" in result.stdout
    assert "member" in result.stdout


@pytest.mark.asyncio
async def test_cli_get_user(db_path):
    """Get user details."""
    # Create a user first
    from hfvg.users import UserManager
    user_mgr = UserManager(db_path)
    
    await user_mgr.create_user("Charlie", "charlie@example.com", "member", "pass789")
    
    result = run_cli(
        "get",
        "--email", "charlie@example.com",
        env={"DATABASE_PATH": db_path}
    )
    
    assert result.returncode == 0, result.stderr
    assert "User Details:" in result.stdout
    assert "Charlie" in result.stdout
    assert "charlie@example.com" in result.stdout
    assert "member" in result.stdout


@pytest.mark.asyncio
async def test_cli_reset_password(db_path):
    """Reset user password."""
    # Create a user first
    from hfvg.users import UserManager
    user_mgr = UserManager(db_path)
    
    await user_mgr.create_user("David", "david@example.com", "member", "oldpass")
    
    # Reset password
    result = run_cli(
        "reset-password",
        "--email", "david@example.com",
        env={"DATABASE_PATH": db_path},
        input_data="newpass123\n"
    )
    
    assert result.returncode == 0, result.stderr
    assert "Password reset successfully" in result.stdout
    
    # Verify new password works
    user = await user_mgr.authenticate_password("david@example.com", "newpass123")
    assert user is not None
    
    # Verify old password doesn't work
    user = await user_mgr.authenticate_password("david@example.com", "oldpass")
    assert user is None


@pytest.mark.asyncio
async def test_cli_reset_password_nonexistent_user_fails(db_path):
    """Resetting password for nonexistent user fails."""
    result = run_cli(
        "reset-password",
        "--email", "nonexistent@example.com",
        env={
            "DATABASE_PATH": db_path,
            "USER_PASSWORD": "password123"
        }
    )
    
    assert result.returncode != 0
    assert "not found" in result.stderr.lower()


@pytest.mark.asyncio
async def test_cli_no_password_provided_fails(db_path):
    """Creating user without password fails."""
    # Run in a TTY-like environment (no stdin pipe, no env var)
    result = run_cli(
        "create",
        "--name", "No Pass User",
        "--email", "nopass@example.com",
        "--role", "member",
        env={"DATABASE_PATH": db_path}
        # No input_data, no USER_PASSWORD
    )
    
    assert result.returncode != 0
    assert "password required" in result.stderr.lower() or "provide via" in result.stderr.lower()
