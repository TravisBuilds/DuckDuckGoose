"""
Test draft-from-idea returns 503 when OpenAI API key is not configured.
Separate test file to avoid monkeypatch conflicts with the main matrix test.
"""

import pytest


@pytest.fixture
async def db_path_no_fake(tmp_path, monkeypatch):
    """Setup test database WITHOUT DRAFT_LLM_FAKE."""
    import os
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DRY_RUN", "true")
    # Explicitly unset DRAFT_LLM_FAKE to test real OpenAI config check
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    
    # Create data directory
    data_dir = tmp_path / "data" / "episodes"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    # Create admin and member users
    admin = await user_mgr.create_user(
        name="Admin User",
        email="admin@test.com",
        role="admin",
        password="admin123"
    )
    
    member = await user_mgr.create_user(
        name="Member User",
        email="member@test.com",
        role="member",
        password="member123"
    )
    
    yield path, admin, member


@pytest.fixture(autouse=True)
def fresh_api_module():
    """Reload API module for each test."""
    import sys
    sys.modules.pop("api.main", None)


def get_client_and_login(role: str, db_path):
    """Get client and login as specified role."""
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    if role == "admin":
        response = client.post("/api/login", json={"email": "admin@test.com", "password": "admin123"})
        assert response.status_code == 200
        cookies = {"studio_admin_token": response.cookies.get("studio_admin_token")}
        return client, cookies
    elif role == "member":
        response = client.post("/api/login", json={"email": "member@test.com", "password": "member123"})
        assert response.status_code == 200
        cookies = {"studio_admin_token": response.cookies.get("studio_admin_token")}
        return client, cookies
    else:
        # Unauthenticated
        return client, None


def test_draft_from_idea_503_without_openai_key(db_path_no_fake, monkeypatch):
    """Test that draft-from-idea returns exactly 503 when OPENAI_API_KEY is not configured.
    
    Hermetic: explicitly unsets env var and resets config attribute to ensure
    test passes regardless of ambient state or test order.
    """
    import os
    
    # Ensure OPENAI_API_KEY is unset in environment (hermetic regardless of ambient state)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DRAFT_LLM_FAKE", raising=False)
    
    # Reset config class attribute to force reload from (now-unset) environment
    # This makes the test hermetic when run after tests that set OPENAI_API_KEY
    from hfvg import config as config_module
    config_module.config.OPENAI_API_KEY = None
    
    for role in ["none", "member", "admin"]:
        client, cookies = get_client_and_login(role, db_path_no_fake)
        
        kwargs = {"json": {"idea": "A test video", "style": "test"}}
        if cookies:
            kwargs["cookies"] = cookies
        
        response = client.post("/api/episodes/draft-from-idea", **kwargs)
        
        if role == "none":
            assert response.status_code == 401, (
                f"NONE draft-from-idea expected 401, got {response.status_code}. Response: {response.text}"
            )
        else:
            # Member and admin should get exactly 503 when OpenAI not configured
            assert response.status_code == 503, (
                f"{role.upper()} draft-from-idea expected 503 (OpenAI not configured), got {response.status_code}. Response: {response.text}"
            )
            assert "not configured" in response.json()["detail"].lower()
