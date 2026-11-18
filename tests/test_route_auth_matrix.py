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
    """Setup test database with users and a test episode."""
    import os
    path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DRAFT_LLM_FAKE", "1")  # Use fake LLM for draft-from-idea
    
    # Create data directory
    data_dir = tmp_path / "data" / "episodes" / "ep01" / "outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    from hfvg.studio_db import init_studio_db, create_episode
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
    
    # Create a test episode in the database
    await create_episode(path, "ep01")
    
    # Initialize budget for the episode
    await ledger.init_episode_budget("ep01")
    
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
    ("GET", "/api/episodes/ep01", {"none": 401, "member": [200, 503], "admin": [200, 503]}, False, None),  # 503 if Temporal unavailable
    ("POST", "/api/episodes/ep01/set-live", {"none": 401, "member": [200, 503], "admin": [200, 503]}, True, {"confirmation": "ENABLE_LIVE_MODE"}),
    ("POST", "/api/episodes/ep01/set-dry", {"none": 401, "member": [200, 503], "admin": [200, 503]}, True, {}),
    ("GET", "/api/episodes/ep01/story-shape", {"none": 401, "member": [200, 503], "admin": [200, 503]}, False, None),
    ("PUT", "/api/episodes/ep01/story-shape", {"none": 401, "member": [200, 503], "admin": [200, 503]}, True, {"shot_1": {"description": "Test shot"}}),
    ("GET", "/api/episodes/ep01/budget", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/ep01/shots", {"none": 401, "member": [200, 503], "admin": [200, 503]}, False, None),
    ("GET", "/api/episodes/ep01/gates", {"none": 401, "member": [200, 503], "admin": [200, 503]}, False, None),
    ("GET", "/api/episodes/ep01/audit", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/ep01/files", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/me", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/models", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/ep01/credits", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/ep01/stalls", {"none": 401, "member": 200, "admin": 200}, False, None),
    ("GET", "/api/episodes/ep01/shot-states", {"none": 401, "member": [200, 503], "admin": [200, 503]}, False, None),  # 503 if Temporal unavailable
    ("POST", "/api/episodes/ep01/reopen", {"none": 401, "member": [200, 503, 409], "admin": [200, 503, 409]}, True, {"reason": "Test reopen"}),  # 409 if not completed
    ("POST", "/api/episodes/ep01/shots/shot001/retry", {"none": 401, "member": [200, 503, 409], "admin": [200, 503, 409]}, True, {}),  # 409 if not at valid gate
    ("POST", "/api/episodes/ep01/shots/shot001/approve", {"none": 401, "member": [200, 503, 409], "admin": [200, 503, 409]}, True, {}),  # 409 if not at G2
    ("POST", "/api/episodes/ep01/shots/shot001/reject", {"none": 401, "member": [200, 503, 409], "admin": [200, 503, 409]}, True, {"reason": "Test rejection"}),  # Form data, handled by test; 409 if not at G2
    ("POST", "/api/episodes/ep01/shots/shot001/accept-clip", {"none": 401, "member": [200, 503, 409], "admin": [200, 503, 409]}, True, {"reason": "Test acceptance"}),  # 409 if clip not needs_review
    ("POST", "/api/users/me/change-password", {"none": 401, "member": 200, "admin": 200}, True, {}),  # Test function sets role-specific password
    ("POST", "/api/episodes/draft-from-idea", {"none": 401, "member": 200, "admin": 200}, True, {"idea": "A test video", "style": "test"}),  # DRAFT_LLM_FAKE=1 returns canned data
    ("POST", "/api/voices/pick", {"none": 401, "member": 200, "admin": 200}, True, {"description": "warm calm narrator"}),  # DRY_RUN=true picks a placeholder voice
    
    # Admin-only routes (financial controls)
    ("POST", "/api/episodes/ep01/raise-cap", {"none": 401, "member": 403, "admin": [200, 503, 404, 400]}, True, {"new_cap": 1000, "reason": "test"}),
    ("POST", "/api/episodes/ep01/resume-balance", {"none": 401, "member": 403, "admin": [200, 503, 404]}, False, None),
    
    # Admin-only routes (user management)
    ("GET", "/api/users", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/users", {"none": 401, "member": 403, "admin": [200, 400]}, True, {"name": "Test", "email": "test@example.com", "role": "member"}),
    ("GET", "/api/users/usr_test", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    ("POST", "/api/users/usr_test/reset-password", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    
    # Admin-only routes (budget/financial)
    ("GET", "/api/budget/balance", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("GET", "/api/budget/all-balances", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/budget/balance", {"none": 401, "member": 403, "admin": [200, 400, 422]}, True, {"amount_usd_micros": 1000000}),
    ("POST", "/api/budget/ep01/lines/sync", {"none": 401, "member": 403, "admin": [200, 404, 400, 500, 422]}, False, None),
    
    # Admin-only routes (dashboard/analytics)
    ("GET", "/api/dashboard", {"none": 401, "member": 403, "admin": [200, 503]}, False, None),
    ("PUT", "/api/dashboard/episodes/ep01/attribution", {"none": 401, "member": 403, "admin": [200, 404]}, True, {"user_id": "usr_test"}),
    ("DELETE", "/api/dashboard/episodes/ep01/attribution", {"none": 401, "member": 403, "admin": [200, 404]}, False, None),
    ("GET", "/api/alerts/status", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/alerts/test", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/balance/check", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/costs/sync", {"none": 401, "member": 403, "admin": 200}, False, None),
    
    # Admin-only routes (admin operations)
    ("GET", "/api/stalls", {"none": 401, "member": 403, "admin": 200}, False, None),
    ("POST", "/api/episodes/ep01/approve-g108", {"none": 401, "member": 403, "admin": [200, 404, 503]}, False, None),
    ("POST", "/api/episodes/ep01/set-ref-versions", {"none": 401, "member": 403, "admin": [200, 404, 422]}, True, {}),
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
            # Special case: change-password needs role-specific current password
            if path == "/api/users/me/change-password" and role in ["member", "admin"]:
                current_password = "member123" if role == "member" else "admin123"
                kwargs["json"] = {"current_password": current_password, "new_password": "newpass123"}
            # Special case: reject uses Form data (not JSON)
            elif "reject" in path and "reason" in body:
                kwargs["data"] = body
            else:
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
            # Multiple acceptable statuses (e.g., 200 or 503 if Temporal unavailable)
            assert response.status_code in expected_status, (
                f"{role.upper()} {method} {path}: expected one of {expected_status}, got {response.status_code}. "
                f"Response: {response.text}"
            )
        else:
            assert response.status_code == expected_status, (
                f"{role.upper()} {method} {path}: expected {expected_status}, got {response.status_code}. "
                f"Response: {response.text}"
            )


# Universe routes (api/world_routes.py). Roles: "none" (no login), "other" (a member with nothing in
# the Universe: the 404 a missing Universe gets), "editor" (a member whose episode is in it: may
# see, may not change), "owner" (the member who made it) and "admin". Each role acts on a Universe of
# its own, so one role's change never meets another's.
SEE = {"none": 401, "other": 404, "editor": 200, "owner": 200, "admin": 200}
CHANGE = {"none": 401, "other": 404, "editor": 403, "owner": 200, "admin": 200}
ANY_MEMBER = {"none": 401, "other": 200, "editor": 200, "owner": 200, "admin": 200}
UNIVERSE_ROUTE_CASES = [
    ("GET", "/api/worlds", ANY_MEMBER),
    ("POST", "/api/worlds", ANY_MEMBER),
    ("GET", "/api/worlds/{series_id}", SEE),
    ("POST", "/api/worlds/{series_id}/profile", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/with-picture", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}/versions/1/images", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}/versions/1/images/{image_id}/primary", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}/versions/1/images/{image_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/cast/{character_id}/voice", CHANGE),
    ("POST", "/api/worlds/{series_id}/narrator-voice", CHANGE),
    ("POST", "/api/worlds/{series_id}/props", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/with-photo", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/{prop_id}", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/{prop_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/{prop_id}/photos", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/{prop_id}/photos/{photo_id}/primary", CHANGE),
    ("POST", "/api/worlds/{series_id}/props/{prop_id}/photos/{photo_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/notes", CHANGE),
    ("POST", "/api/worlds/{series_id}/notes/{note_id}", CHANGE),
    ("POST", "/api/worlds/{series_id}/notes/{note_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/photos", CHANGE),
    ("POST", "/api/worlds/{series_id}/photos/{reference_id}/delete", CHANGE),
    ("POST", "/api/worlds/{series_id}/board/estimate", CHANGE),
    ("POST", "/api/worlds/{series_id}/board", CHANGE),
    ("POST", "/api/worlds/{series_id}/board/{board_id}/approve", CHANGE),
    ("POST", "/api/worlds/{series_id}/bible", CHANGE),
    ("GET", "/api/worlds/images/{reference_id}", SEE),  # the cover: shown to whoever may see the Universe
    ("GET", "/api/worlds/images/{reference_id}/thumb", SEE),
    ("GET", "/api/episodes/{episode_id}/universe", SEE),  # the editor's episode: hidden from everyone else
    ("POST", "/api/episodes/draft-from-idea?world", SEE),
]
UNIVERSE_JSON = {
    "/api/worlds": {"name": "Tide Hollow"},
    "/api/worlds/{series_id}/profile": {"logline": "A bay of humming lanterns"},
    "/api/worlds/{series_id}/cast": {"name": "Moss"},
    "/api/worlds/{series_id}/cast/{character_id}": {"name": "Wren Two"},
    "/api/worlds/{series_id}/cast/{character_id}/voice": {"voice_id": "fake-voice-1", "name": "Calm"},
    "/api/worlds/{series_id}/narrator-voice": {"voice_id": "fake-voice-1", "name": "Calm"},
    "/api/worlds/{series_id}/props": {"kind": "prop", "name": "Oar"},
    "/api/worlds/{series_id}/props/{prop_id}": {"name": "Long pier"},
    "/api/worlds/{series_id}/notes": {"topic": "tone", "body": "Quiet and warm"},
    "/api/worlds/{series_id}/notes/{note_id}": {"body": "Quieter"},
    "/api/worlds/{series_id}/board/estimate": {"feedback": "warmer light"},
    "/api/worlds/{series_id}/board": {"feedback": "warmer light", "approved_total_usd_micros": 10**9},
}


async def _universe_for(role_episode: str, owner_id: str, editor_id: str, approved: bool) -> dict:
    """A Universe owned by the member with a character (an upload), a place (a photo), a note, a
    reference photo as its cover and a mood board (approved for the bible route), and an episode of
    the editor in it."""
    import os
    from hfvg import cast_store, prop_store, world_store
    from hfvg.estimates import still_call_usd_micros
    from hfvg.studio_db import create_episode
    from tests.cast_helpers import attestation, image_bytes

    sid = world_store.create_universe(f"Bay {role_episode}", owner_id, actor=owner_id)["series"]["series_id"]
    library = ("series", sid)
    wren = cast_store.create_character_with_upload(library, "Wren", "lead", "a lantern keeper", image_bytes(seed=1),
                                                   filename="w.png", attestation=attestation(), actor=owner_id)
    pier = prop_store.create_item_with_upload(library, "place", "Pier", "wet planks", image_bytes(seed=2),
                                              filename="p.png", attestation=attestation(), actor=owner_id)
    note = world_store.add_note(sid, "premise", "Lanterns that hum", owner_id)
    ref = world_store.add_reference_photo(sid, image_bytes(seed=3), filename="r.png", attestation=attestation(),
                                          actor=owner_id)
    world_store.update_profile(sid, cover_image_id=ref["image_id"], actor=owner_id)
    plan = world_store.plan_board(sid)
    total = world_store.BOARD_IMAGES * still_call_usd_micros()
    board = world_store.create_board(sid, plan, estimate_usd_micros=total, approved_total_usd_micros=total,
                                     dry_run=True, images=world_store.placeholder_board_images(sid, plan["photo_files"]),
                                     placeholder=True, requested_by=owner_id)
    if approved:
        world_store.approve_board(sid, board["board_id"], owner_id)
    await create_episode(os.environ["DATABASE_PATH"], role_episode, user_id=editor_id)
    cast_store.link_episode(role_episode, sid, "admin")
    return {"series_id": sid, "character_id": wren["character_id"],
            "image_id": wren["versions"][0]["images"][0]["image_id"], "prop_id": pier["prop_id"],
            "photo_id": pier["photos"][0]["image_id"], "note_id": note["note_id"], "reference_id": ref["image_id"],
            "board_id": board["board_id"], "episode_id": role_episode}


def _universe_request(client, cookies, method: str, path: str, ids: dict):
    import io
    from tests.cast_helpers import image_bytes
    kwargs = {"cookies": cookies} if cookies else {}
    if path == "/api/episodes/draft-from-idea?world":
        return client.post("/api/episodes/draft-from-idea", json={
            "idea": "A keeper lights the last lantern", "archetype_id": "cozy-hangout", "target_duration": 12,
            "world_id": ids["series_id"]}, **kwargs)
    url = path.format(**ids)
    if method == "GET":
        return client.get(url, **kwargs)
    files = {"image_file": ("new.png", io.BytesIO(image_bytes(seed=77)), "image/png")}
    if path.endswith("/with-picture"):
        return client.post(url, data={"rights": "original", "name": "Moss"}, files=files, **kwargs)
    if path.endswith("/with-photo"):
        return client.post(url, data={"rights": "original", "kind": "prop", "name": "Oar"}, files=files, **kwargs)
    if path.endswith("/images") or path.endswith("/photos"):
        return client.post(url, data={"rights": "original"}, files=files, **kwargs)
    return client.post(url, json=UNIVERSE_JSON.get(path, {}), **kwargs)


@pytest.mark.parametrize("method,path,expected_per_role", UNIVERSE_ROUTE_CASES)
async def test_universe_route_authorization_matrix(db_path, monkeypatch, tmp_path, method, path, expected_per_role):
    """Every Universe route for no login, another member, an episode editor, the owner and an admin
    (DRY_RUN and DRAFT_LLM_FAKE: no provider is asked)."""
    from fastapi.testclient import TestClient
    from hfvg.users import UserManager

    path_db, _admin, member = db_path
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    users = UserManager(path_db)
    await users.create_user(name="Other Member", email="other@test.com", role="member", password="other123")
    editor = await users.create_user(name="Episode Editor", email="editor@test.com", role="member",
                                     password="editor123")
    logins = {"other": ("other@test.com", "other123"), "editor": ("editor@test.com", "editor123"),
              "owner": ("member@test.com", "member123"), "admin": ("admin@test.com", "admin123")}
    from api.main import app
    for n, (role, expected) in enumerate(expected_per_role.items()):
        ids = await _universe_for(f"ep{70 + n}mtx", member["user_id"], editor["user_id"], path.endswith("/bible"))
        client = TestClient(app)
        cookies = {}
        if role in logins:
            email, password = logins[role]
            response = client.post("/api/login", json={"email": email, "password": password})
            assert response.status_code == 200, response.text
            cookies = response.cookies
        response = _universe_request(client, cookies, method, path, ids)
        assert response.status_code == expected, (
            f"{role.upper()} {method} {path}: expected {expected}, got {response.status_code}. Response: {response.text}")
        if role == "other" and expected == 404 and "/images/" not in path:
            missing = (f"No workflow found for episode {ids['episode_id']}" if "{episode_id}" in path
                       else f"Universe {ids['series_id']} not found")
            assert response.json()["detail"] == missing


def test_upload_endpoints_auth(db_path, tmp_path):
    """Test upload endpoints with multipart form data for all roles, asserting exact success codes."""
    from fastapi.testclient import TestClient
    from api.main import app
    import io
    
    # Create a valid minimal beatmap that passes validation
    beatmap_content = b"""# Test Episode

## shot_1

**Duration:** 5 seconds

A robot discovers a glowing book.

**Visual Style:** Animated, colorful

**Camera:** Wide shot
"""
    
    for role in ["none", "member", "admin", "upload_token"]:
        if role == "upload_token":
            # Test with UPLOAD_TOKEN bearer auth
            import os
            upload_token = os.getenv("UPLOAD_TOKEN")
            if not upload_token:
                continue  # Skip if UPLOAD_TOKEN not configured
            client = TestClient(app)
            kwargs = {"headers": {"Authorization": f"Bearer {upload_token}"}}
        else:
            client, cookies = get_client_and_login(role, db_path)
            kwargs = {"cookies": cookies} if cookies else {}
        
        # Test /api/episodes/upload/validate
        files = {
            "beatmap_file": ("BEATMAP.md", io.BytesIO(beatmap_content), "text/markdown"),
        }
        data = {"episode_id": "ep99"}
        validate_response = client.post("/api/episodes/upload/validate", files=files, data=data, **kwargs)
        
        if role == "none":
            assert validate_response.status_code == 401, (
                f"Unauthenticated upload/validate expected 401, got {validate_response.status_code}"
            )
        else:
            # Member, admin, and UPLOAD_TOKEN should get exactly 200 with valid beatmap
            assert validate_response.status_code == 200, (
                f"{role.upper()} upload/validate expected 200, got {validate_response.status_code}. "
                f"Response: {validate_response.text}"
            )
        
        # Test /api/episodes/upload
        files = {
            "beatmap_file": ("BEATMAP.md", io.BytesIO(beatmap_content), "text/markdown"),
        }
        data = {"episode_id": "ep99"}
        upload_response = client.post("/api/episodes/upload", files=files, data=data, **kwargs)
        
        if role == "none":
            assert upload_response.status_code == 401, (
                f"Unauthenticated upload expected 401, got {upload_response.status_code}"
            )
        else:
            # Member, admin, and UPLOAD_TOKEN should get exactly 200 on first upload
            # (503 acceptable if Temporal not available in test environment)
            assert upload_response.status_code in [200, 503], (
                f"{role.upper()} upload expected 200 or 503, got {upload_response.status_code}. "
                f"Response: {upload_response.text}"
            )


def test_wizard_upload_endpoints_auth(db_path):
    """The create wizard's uploads (prompt kit, shots + refs, audio plan): members and admins, never anonymous."""
    import io
    import json

    client, cookies = get_client_and_login("member", db_path)
    response = client.post("/api/episodes/draft-from-idea", cookies=cookies, json={
        "idea": "A paper kite rides the wind over a quiet harbor", "archetype_id": "cozy-hangout",
        "target_duration": 12})
    assert response.status_code == 200, response.text
    draft = response.json()

    def files(*fields):
        return {field: (name, io.BytesIO(json.dumps(draft[key]).encode()), "application/json")
                for field, name, key in fields}

    uploads = [
        ("/api/episodes/ep01/upload-prompt-kit", [("prompt_kit_file", "prompt_kit.json", "prompt_kit_json")]),
        ("/api/episodes/ep01/upload-shots", [("shots_file", "shots.json", "shots_json"),
                                             ("refs_file", "refs.json", "refs_json")]),
        ("/api/episodes/ep01/upload-audio-plan", [("audio_plan_file", "audio_plan.json", "audio_plan_json")]),
    ]
    for role, expected in (("none", 401), ("member", 200), ("admin", 200)):
        client, cookies = get_client_and_login(role, db_path)
        kwargs = {"cookies": cookies} if cookies else {}
        for path, fields in uploads:
            response = client.post(path, files=files(*fields), **kwargs)
            assert response.status_code == expected, (
                f"{role.upper()} POST {path}: expected {expected}, got {response.status_code}. "
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
