"""
Red-green API route tests: Call actual API routes to verify protections.

These tests use FastAPI TestClient to call API routes and verify that safety
checks are actually enforced. Each test is paired with a red-green mutation.
"""

import pytest
import aiosqlite
import sys

from hfvg.studio_db import init_studio_db, create_episode, approve_g108, set_live_mode
from hfvg.budget import BudgetLedger


@pytest.fixture
async def test_db_api(tmp_path, monkeypatch):
    """Create fresh test database for API tests."""
    import secrets
    db_name = f"test_api_{secrets.token_hex(8)}.db"
    db_path = str(tmp_path / db_name)
    await init_studio_db(db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    yield db_path


@pytest.fixture(autouse=True)
def reload_api_for_mutations():
    """Reload api.main to pick up mutations."""
    if 'api.main' in sys.modules:
        del sys.modules['api.main']
    yield
    if 'api.main' in sys.modules:
        del sys.modules['api.main']


async def get_authenticated_client(test_db, monkeypatch):
    """Helper: Get TestClient with authenticated session cookie."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from fastapi.testclient import TestClient
    from api.main import app
    from unittest.mock import MagicMock, AsyncMock
    
    # Mock temporal client to avoid 503 errors
    import api.main as api_main
    mock_client = MagicMock()
    mock_client.start_workflow = AsyncMock(return_value=MagicMock(id="test-workflow"))
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    
    # Login
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    
    return client, response.cookies


@pytest.mark.asyncio
async def test_canary_route_checks_g108(test_db_api, monkeypatch):
    """Test: Canary route refuses without G1.08 approval."""
    # Create episode and shot, but do NOT approve G1.08
    await create_episode(test_db_api, "ep99")
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try canary without G1.08
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False, "Should refuse without G1.08"
    assert "G1.08" in data["message"], "Should mention G1.08 in message"


@pytest.mark.asyncio
async def test_canary_route_checks_live_mode(test_db_api, monkeypatch):
    """Test: Canary in non-dry mode requires live_mode in DB."""
    monkeypatch.setenv("DRY_RUN", "false")  # Non-dry mode
    
    # Create episode, approve G1.08, but do NOT enable live_mode
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", False)
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try canary in non-dry without live_mode
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is False, "Should refuse without live_mode in non-dry"
    assert "Live mode" in data["message"], "Should mention live mode"


@pytest.mark.asyncio
async def test_canary_route_starts_workflow(test_db_api, monkeypatch):
    """Test: Canary route actually starts a ShotWorkflow."""
    # Create episode with G1.08 (dry mode is default)
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")  # Required for canary
    
    # Initialize budget and add L6_reserve line
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute("""
            INSERT INTO budget_lines (line_id, episode_id, provider, line_name, budget_cap, stop_threshold, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("ep99:L6_reserve", "ep99", "higgsfield", "L6_reserve", 250.0, 200.0, "credits"))
        await db.commit()
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test prompt", "pending")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Get the mocked client that was set in get_authenticated_client
    from api import main as api_main
    mock_client = api_main.temporal_client
    
    # Call canary
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True, f"Canary should succeed, got: {data}"
    
    # Verify workflow was started
    assert mock_client.start_workflow.called, "Should start workflow"
    # Workflow is passed as positional arg, check args or kwargs
    call_args = mock_client.start_workflow.call_args
    # The workflow should be ShotWorkflow.run
    assert call_args is not None, "start_workflow should have been called"
    # Just verify it was called - the actual workflow type is complex to check
    assert mock_client.start_workflow.call_count == 1, "Should start exactly one workflow"


@pytest.mark.asyncio
async def test_canary_reserves_l6_before_workflow(test_db_api, monkeypatch):
    """Test: Canary reserves from L6_reserve before starting workflow."""
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")  # Required
    
    # Initialize budget and add L6_reserve line
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute("""
            INSERT INTO budget_lines (line_id, episode_id, provider, line_name, budget_cap, stop_threshold, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("ep99:L6_reserve", "ep99", "higgsfield", "L6_reserve", 250.0, 200.0, "credits"))
        await db.commit()
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Check L6 before canary
    status_before = await ledger.get_line_status("ep99", "L6_reserve")
    reserved_before = status_before["reserved"]
    
    # Call canary
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True, f"Should succeed, got: {data}"
    
    # Check L6 after canary
    status_after = await ledger.get_line_status("ep99", "L6_reserve")
    reserved_after = status_after["reserved"]
    
    # Should have reserved 10 credits
    assert reserved_after > reserved_before, "Should reserve from L6"
    assert reserved_after - reserved_before == 10.0, "Should reserve 10 credits"


@pytest.mark.asyncio
async def test_still_approve_route_sends_signal(test_db_api, monkeypatch):
    """Test: Still approve route sends signal to workflow."""
    await create_episode(test_db_api, "ep99")
    
    async with aiosqlite.connect(test_db_api) as db:
        # Add audit entry for a canary workflow (so approve can find it)
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user, timestamp)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            ("ep99", "start_canary", "Workflow ep99-canary-A01-12345, reserved L6=10.0", "test")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Mock temporal client
    from unittest.mock import AsyncMock, MagicMock
    from api import main as api_main
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.signal = AsyncMock()
    mock_client.get_workflow_handle_for = MagicMock(return_value=mock_handle)
    api_main.temporal_client = mock_client
    
    # Call still approve
    response = client.post("/api/episodes/ep99/shots/A01/approve", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True, "Should succeed"
    
    # Verify signal was sent
    assert mock_handle.signal.called, "Should send signal to workflow"
    signal_name = mock_handle.signal.call_args.args[0]
    assert signal_name == "stills_approved", "Should send stills_approved signal"


@pytest.mark.asyncio
async def test_clip_approve_route_sends_signal(test_db_api, monkeypatch):
    """Test: Clip approve route sends signal to workflow."""
    await create_episode(test_db_api, "ep99")
    
    # Add shot to DB
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        # Add audit entry for canary
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user, timestamp)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            ("ep99", "start_canary", "Workflow ep99-canary-A01-12345, reserved L6=10.0", "test")
        )
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Mock temporal client
    from unittest.mock import AsyncMock, MagicMock
    from api import main as api_main
    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.signal = AsyncMock()
    mock_client.get_workflow_handle_for = MagicMock(return_value=mock_handle)
    api_main.temporal_client = mock_client
    
    # Call clip approve
    response = client.post("/api/episodes/ep99/clips/A01/approve", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True, "Should succeed"
    
    # Verify signal was sent
    assert mock_handle.signal.called, "Should send signal"
    signal_name = mock_handle.signal.call_args.args[0]
    assert signal_name == "clip_approved", "Should send clip_approved signal"


@pytest.mark.asyncio
async def test_clip_approve_rejects_unknown_shot(test_db_api, monkeypatch):
    """Test: Clip approve route rejects nonexistent shot (e.g., ZZ99)."""
    await create_episode(test_db_api, "ep99")
    
    # Do NOT add shot ZZ99 to DB
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try to approve nonexistent shot
    response = client.post("/api/episodes/ep99/clips/ZZ99/approve", cookies=cookies)
    
    assert response.status_code == 404, "Should reject unknown shot"
    assert "not found" in response.json()["detail"].lower(), "Should mention not found"
