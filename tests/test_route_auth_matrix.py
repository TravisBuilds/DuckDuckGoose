"""
Comprehensive parametrized tests for the complete route authorization matrix.

Tests every route against:
- Unauthenticated (none): should get 401
- Member: should get 200/appropriate success (if allowed) or 403 (if admin-only)
- Admin: should get 200/appropriate success (always allowed)
"""

import pytest


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    """Setup test database with users."""
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    
    from hfvg.studio_db import init_studio_db
    from hfvg.users import UserManager
    from hfvg.budget import BudgetLedger
    from hfvg.cost_tracking import CostTracker
    
    await init_studio_db(path)
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    ledger = BudgetLedger(path)
    await ledger.init_db()
    
    cost_tracker = CostTracker(path)
    await cost_tracker.init_db()
    
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
    
    # Create a test episode
    await init_studio_db(path)
    
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
        email, password = "admin@test.com", "admin123"
    elif role == "member":
        email, password = "member@test.com", "member123"
    else:
        return client, {}
    
    response = client.post("/api/login", json={"email": email, "password": password})
    assert response.status_code == 200, f"Login failed: {response.text}"
    
    return client, response.cookies


# Route test cases: (method, path, expected_status_per_role, requires_body, body)
# expected_status_per_role: {"none": 401, "member": 200|403, "admin": 200}
ROUTE_TEST_CASES = [
    # Public routes (200 for all)
    ("GET", "/", {"none": 200, "member": 200, "admin": 200}, False, None),
    ("GET", "/gates", {"none": 200, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/health", {"none": 200, "member": 200, "admin": 200}, False, None),
    
    # Member-allowed routes
    ("GET", "/api/episodes", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/test-ep", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/set-live", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/set-dry", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("GET", "/api/episodes/test-ep/story-shape", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("PUT", "/api/episodes/test-ep/story-shape", {"none": 401, "member": [200, 404, 422], "admin": [200, 404, 422]}, True, {}),
    ("GET", "/api/episodes/test-ep/budget", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("GET", "/api/episodes/test-ep/shots", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("GET", "/api/episodes/test-ep/gates", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("GET", "/api/episodes/test-ep/audit", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("GET", "/api/episodes/test-ep/files", {"none": 401, "member": [200, 404], "admin": [200, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/reopen", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/shots/shot1/retry", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/shots/shot1/approve", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/shots/shot1/reject", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/shots/shot1/accept-clip", {"none": 401, "member": [200, 503, 404], "admin": [200, 503, 404]}, False, None),
    ("POST", "/api/users/me/change-password", {"none": 401, "member": [200, 400], "admin": [200, 400]}, True, {"current_password": "test", "new_password": "newpass123"}),
    
    # Admin-only routes (financial controls)
    ("POST", "/api/episodes/test-ep/raise-cap", {"none": 401, "member": 403, "admin": [200, 503, 404]}, True, {"new_cap": 1000, "reason": "test"}),
    ("POST", "/api/episodes/test-ep/resume-balance", {"none": 401, "member": 403, "admin": [200, 503, 404]}, False, None),
    
    # Admin-only routes (user management)
    ("GET", "/api/users", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/users", {"none": 401, "member": 403, "admin": [200, 400]}, True, {"name": "Test", "email": "test@example.com", "role": "member"}),
    ("GET", "/api/users/usr_test", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    ("POST", "/api/users/usr_test/reset-password", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    
    # Admin-only routes (budget/financial)
    ("GET", "/api/budget/balance", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("GET", "/api/budget/all-balances", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/budget/balance", {"none": 401, "member": 403, "admin": [200, 400]}, True, {"amount_usd_micros": 1000000}),
    ("POST", "/api/budget/test-ep/lines/sync", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    
    # Admin-only routes (dashboard/analytics)
    ("GET", "/api/dashboard", {"none": 401, "member": 403, "admin": [200, 503]}, False, None),
    
    # Admin-only routes (admin operations)
    ("GET", "/api/stalls", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/episodes/test-ep/approve-g108", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    ("POST", "/api/episodes/test-ep/set-ref-versions", {"none": 401, "member": 403, "admin": [200, 404, 422]}, True, {}),
    ("POST", "/api/lint", {"none": 401, "member": 403, "admin": [200, 400, 422]}, True, {}),
]


@pytest.mark.parametrize("method,path,expected_per_role,requires_body,body", ROUTE_TEST_CASES)
def test_route_authorization_matrix(db_path, method, path, expected_per_role, requires_body, body):
    """
    Test route authorization for all roles: none, member, admin.
    
    Verifies that:
    - Unauthenticated requests get 401
    - Members get 200 (if allowed) or 403 (if admin-only)
    - Admins always get 200 (or appropriate success status)
    """
    for role in ["none", "member", "admin"]:
        client, cookies = get_client_and_login(role, db_path)
        
        # Make the request
        kwargs = {"cookies": cookies} if cookies else {}
        if requires_body and body is not None:
            kwargs["json"] = body
        
        if method == "GET":
            response = client.get(path, **kwargs)
        elif method == "POST":
            response = client.post(path, **kwargs)
        elif method == "PUT":
            response = client.put(path, **kwargs)
        elif method == "DELETE":
            response = client.delete(path, **kwargs)
        else:
            pytest.fail(f"Unsupported method: {method}")
        
        # Check expected status
        expected_status = expected_per_role[role]
        if isinstance(expected_status, list):
            # Multiple acceptable statuses (e.g., 200 or 404 if resource doesn't exist)
            assert response.status_code in expected_status, (
                f"{role.upper()} {method} {path}: expected one of {expected_status}, got {response.status_code}. "
                f"Response: {response.text}"
            )
        else:
            assert response.status_code == expected_status, (
                f"{role.upper()} {method} {path}: expected {expected_status}, got {response.status_code}. "
                f"Response: {response.text}"
            )


def test_admin_secret_bearer_token_works(db_path, monkeypatch):
    """Admin secret Bearer token should work for admin-only routes."""
    from fastapi.testclient import TestClient
    from api.main import app
    import secrets
    
    admin_secret = secrets.token_urlsafe(32)
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    # Reload app with new secret
    import sys
    sys.modules.pop("api.main", None)
    from api.main import app as new_app
    
    client = TestClient(new_app)
    
    # Test admin-only route with Bearer token
    response = client.get("/api/users", headers={"Authorization": f"Bearer {admin_secret}"})
    assert response.status_code == 200
    
    # Test member route with Bearer token (should also work - admin has all access)
    response = client.get("/api/episodes", headers={"Authorization": f"Bearer {admin_secret}"})
    assert response.status_code == 200
