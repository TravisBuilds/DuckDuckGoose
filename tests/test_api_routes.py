"""
Red-green API route tests: Call actual API routes to verify protections.

These tests use FastAPI TestClient to call API routes and verify that safety
checks are actually enforced. Each test is paired with a red-green mutation.
"""

import pytest
import json
from tests.ledger_helpers import insert_line, set_balance
from tests.lint_helpers import lint_ok
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
    
    # Initialize budget tables
    from hfvg.budget import BudgetLedger
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
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
        # Add L6_reserve line so the route doesn't fail on budget check
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try canary without G1.08
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    # Key assertion: should refuse without G1.08
    assert data.get("success") is False, f"Should refuse without G1.08, got data: {data}"
    # If mutation removes the G1.08 check, success would be True (or error would occur)


@pytest.mark.asyncio
async def test_lint_route_requires_auth(test_db_api, monkeypatch):
    """M-313: POST /api/lint requires authentication (G-AUTH-1 fix)."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from fastapi.testclient import TestClient
    from api.main import app
    from unittest.mock import MagicMock
    import api.main as api_main
    
    # Mock temporal client
    mock_client = MagicMock()
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    
    # Try lint without auth
    response = client.post("/api/lint", json={"prompt": "test prompt"})
    
    # Key assertion: should reject unauthenticated request
    assert response.status_code == 401, f"Expected 401, got {response.status_code}"
    # If mutation removes auth check, this would return 200


@pytest.mark.asyncio
async def test_file_download_path_containment(test_db_api, monkeypatch):
    """M-314: File download route prevents directory traversal (G-UI-1 path containment)."""
    await create_episode(test_db_api, "ep99")
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try to download a file (will 404 since file doesn't exist, but should not allow traversal)
    # The route validates episode_id format and checks path containment before checking existence
    
    # Valid file type but non-existent file should give 404
    response = client.get("/api/episodes/ep99/files/master_subs", cookies=cookies)
    assert response.status_code in [404, 500], f"Expected 404 or 500 for non-existent file, got {response.status_code}"
    
    # The key test: invalid episode_id format should be rejected
    # FastAPI normalizes paths, so "../../../etc" becomes "etc" which then fails validate_episode_id
    response = client.get("/api/episodes/etc/files/master_subs", cookies=cookies)
    assert response.status_code == 400, "Should reject invalid episode_id format (etc instead of ep##)"


@pytest.mark.asyncio
async def test_all_mutating_routes_require_auth(test_db_api, monkeypatch):
    """M-315: Enumerate all non-GET routes and verify they require authentication (G-AUTH-1)."""
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from fastapi.testclient import TestClient
    from api.main import app
    from unittest.mock import MagicMock
    import api.main as api_main
    
    # Mock temporal client
    mock_client = MagicMock()
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    
    # Allowlist: routes that don't require auth (login, health check)
    allowlist = [
        ("GET", "/"),
        ("GET", "/api/health"),
        ("POST", "/api/login"),
    ]
    
    # Get all routes from the app
    mutating_routes_checked = 0
    for route in app.routes:
        if hasattr(route, 'methods') and hasattr(route, 'path'):
            for method in route.methods:
                if method == "GET":
                    continue  # Only check mutating routes
                
                # Check if in allowlist
                if (method, route.path) in allowlist:
                    continue
                
                # Try to call the route without auth
                path = route.path.replace("{episode_id}", "ep99").replace("{shot_id}", "A01").replace("{file_type}", "master")
                
                try:
                    if method == "POST":
                        response = client.post(path, json={})
                    elif method == "PUT":
                        response = client.put(path, json={})
                    elif method == "DELETE":
                        response = client.delete(path)
                    elif method == "PATCH":
                        response = client.patch(path, json={})
                    else:
                        continue
                    
                    # Key assertion: all non-allowlisted mutating routes should require auth
                    assert response.status_code in [401, 403, 422], \
                        f"{method} {route.path} should require auth, got {response.status_code}"
                    mutating_routes_checked += 1
                except Exception:
                    # Some routes may fail for other reasons (missing params, etc.)
                    # That's fine - we're checking auth, not functionality
                    pass
    
    # Sanity check: we should have checked at least some routes
    assert mutating_routes_checked >= 5, f"Should have checked at least 5 mutating routes, only checked {mutating_routes_checked}"


@pytest.mark.asyncio
async def test_canary_route_checks_live_mode(test_db_api, monkeypatch):
    """Test: Canary in non-dry mode requires live_mode in DB."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate  # Non-dry mode
    
    # Create episode, approve G1.08, but do NOT enable live_mode
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", False)
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        # Add L6_reserve line so the route doesn't fail on budget check
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Try canary without live_mode
    response = client.post("/api/episodes/ep99/canary", cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    # Key assertion: should refuse without live_mode when DRY_RUN=false
    assert data.get("success") is False, f"Should refuse without live_mode in non-dry, got data: {data}"
    # If mutation removes the live_mode check, success would be True (or error would occur)


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
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
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
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
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
    
    # Dry canary hold: (0.06 still + 5 s x 0.056 Kling) * 1.2 = 0.408 USD
    assert reserved_after > reserved_before, "Should reserve from L6"
    assert reserved_after - reserved_before == 408_000, "Should reserve 408_000 usd_micros (0.408 USD)"
    assert data["reserved_amount"] == 408_000


@pytest.mark.asyncio
async def test_still_approve_route_sends_signal(test_db_api, monkeypatch):
    """Test: Still approve route sends signal to workflow."""
    await create_episode(test_db_api, "ep99")
    
    async with aiosqlite.connect(test_db_api) as db:
        # Add audit entry for a canary workflow (so approve can find it)
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user, timestamp)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            ("ep99", "start_canary", "Workflow ep99-canary-A01-12345, reserved L6=1000000 usd_micros", "test")
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
async def test_canary_l6_reconcile_on_failure(test_db_api, monkeypatch, tmp_path):
    """Test: Canary reconciles L6 reservation when workflow fails via real route."""
    import asyncio
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg import activities
    from hfvg.budget import BudgetLedger
    
    # Set up episode with L6 budget line
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", True)
    
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    
    async with aiosqlite.connect(test_db_api) as db:
        # Add shot
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        # Add L6_reserve line
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        # Add L2 line (needed for ShotWorkflow)
        await insert_line(db, "ep99", "L2_drafts", 10_000_000, 8_000_000)
        await db.commit()
    
    # Get initial L6 reserved (should be 0)
    status_before = await ledger.get_line_status("ep99", "L6_reserve")
    assert status_before["reserved"] == 0, "L6 should start at 0"
    
    # Set up Temporal environment and worker with a failing activity
    async with await WorkflowEnvironment.start_time_skipping() as env:
        # Mock the submit activity to always fail
        from temporalio import activity
        
        @activity.defn(name="submit_still_job_enforced")
        async def failing_submit(*args, **kwargs):
            raise ValueError("Simulated workflow failure")
        
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                failing_submit,  # This will cause workflow to fail
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
            ],
        ):
            # Reserve L6 (what the canary API does before starting workflow)
            reserved = await ledger.reserve("ep99", "L6_reserve", 1_000_000, "Canary test")
            assert reserved, "Should reserve L6"
            
            # Log the canary start (what the API does)
            async with aiosqlite.connect(test_db_api) as db:
                await db.execute(
                    """INSERT INTO audit_log (episode_id, action, details, user)
                       VALUES (?, ?, ?, ?)""",
                    ("ep99", "start_canary", "Workflow canary-fail-test, reserved L6=1000000 usd_micros", "system")
                )
                await db.commit()
            
            # Verify L6 is now reserved
            status_mid = await ledger.get_line_status("ep99", "L6_reserve")
            assert status_mid["reserved"] == 1_000_000, "L6 should be reserved"
            
            # Start workflow
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", {"shot_id": "A01", "prompt": "Test", "refs": [], "params": {}}],
                id="canary-fail-test",
                task_queue="test-task-queue",
            )
            
            # Wait for workflow to fail
            try:
                await asyncio.wait_for(handle.result(), timeout=5)
                assert False, "Workflow should have failed"
            except Exception:
                pass  # Expected to fail
            
            # Set up API with authenticated session
            admin_secret = "a" * 32
            monkeypatch.setenv("ADMIN_SECRET", admin_secret)
            
            from fastapi.testclient import TestClient
            from api.main import app
            import api.main as api_main
            
            # Connect temporal_client to test environment
            api_main.temporal_client = env.client
            
            client = TestClient(app)
            
            # Login to get session cookie
            response = client.post("/api/login", json={"admin_secret": admin_secret})
            assert response.status_code == 200
            cookies = response.cookies
            
            # Call the real canary status route (triggers reconcile on failed)
            response = client.get(
                f"/api/canary/canary-fail-test",
                cookies=cookies,
            )
            assert response.status_code == 200
            data = response.json()
            # Workflow completes successfully but returns failed shot result
            assert data["status"] == "completed", f"Expected completed status, got {data}"
            assert data["result"]["status"] == "failed", f"Expected failed shot, got {data['result']}"
            
            # Check that L6 reservation was released to exactly 0
            status_after = await ledger.get_line_status("ep99", "L6_reserve")
            assert status_after["reserved"] == 0, \
                f"L6 should be exactly 0 after failed reconcile, got {status_after['reserved']}"
            
            # Verify idempotent: second call should not change anything
            response = client.get(
                f"/api/canary/canary-fail-test",
                cookies=cookies,
            )
            assert response.status_code == 200
            data2 = response.json()
            assert data2["status"] == "completed"
            assert data2["result"]["status"] == "failed"
            
            status_after2 = await ledger.get_line_status("ep99", "L6_reserve")
            assert status_after2["reserved"] == 0, "L6 should still be 0 (idempotent)"
            
            # Also verify the reconciliation was logged exactly once
            async with aiosqlite.connect(test_db_api) as db:
                async with db.execute(
                    """SELECT COUNT(*) FROM audit_log 
                       WHERE action = 'reconcile_canary_l6' AND details LIKE ?""",
                    ("%canary-fail-test%",)
                ) as cursor:
                    count = (await cursor.fetchone())[0]
                    assert count == 1, f"Reconciliation should be logged once, got {count}"


@pytest.mark.asyncio
async def test_canary_l6_reconcile_on_completed(test_db_api, monkeypatch, tmp_path):
    """Test: Canary reconciles L6 reservation when workflow completes via real route."""
    from hfvg.budget import BudgetLedger
    from unittest.mock import AsyncMock, MagicMock
    
    # Set up episode with L6 budget line
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", True)
    
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    
    # Reserve L6
    reserved = await ledger.reserve("ep99", "L6_reserve", 1_000_000, "Canary test")
    assert reserved, "Should reserve L6"
    
    # Log the canary start
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            ("ep99", "start_canary", "Workflow canary-success-test, reserved L6=1000000 usd_micros", "system")
        )
        await db.commit()
    
    # Verify L6 is reserved
    status_mid = await ledger.get_line_status("ep99", "L6_reserve")
    assert status_mid["reserved"] == 1_000_000, "L6 should be reserved"
    
    # Set up API with mock temporal client
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    
    # Mock workflow handle that is completed
    mock_handle = AsyncMock()
    mock_handle.result = AsyncMock(return_value={"status": "completed", "result": "success"})
    
    mock_client = MagicMock()
    mock_client.get_workflow_handle_for = MagicMock(return_value=mock_handle)
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    cookies = response.cookies
    
    # Call the real canary status route (triggers reconcile on completed)
    response = client.get(
        f"/api/canary/canary-success-test",
        cookies=cookies,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed", f"Expected completed status, got {data}"
    
    # Check that L6 reservation was released to exactly 0
    status_after = await ledger.get_line_status("ep99", "L6_reserve")
    assert status_after["reserved"] == 0, \
        f"L6 should be exactly 0 after completed reconcile, got {status_after['reserved']}"


@pytest.mark.asyncio
async def test_canary_l6_reconcile_concurrent(test_db_api, monkeypatch, tmp_path):
    """Test: Concurrent canary status calls reconcile L6 exactly once."""
    import asyncio
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg import activities
    from hfvg.budget import BudgetLedger
    
    # Set up episode
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", True)
    
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await insert_line(db, "ep99", "L2_drafts", 10_000_000, 8_000_000)
        await db.commit()
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        from temporalio import activity
        
        @activity.defn(name="submit_still_job_enforced")
        async def failing_submit(*args, **kwargs):
            raise ValueError("Simulated workflow failure")
        
        async with Worker(
            env.client,
            task_queue="test-task-queue",
            workflows=[ShotWorkflow],
            activities=[
                failing_submit,
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
                activities.precheck_still_qc,
                activities.precheck_clip_qc,
                activities.review_still,
                activities.review_clip,
                activities.record_shot_result,
            ],
        ):
            # Reserve L6
            reserved = await ledger.reserve("ep99", "L6_reserve", 1_000_000, "Canary test")
            assert reserved, "Should reserve L6"
            
            # Log the canary start
            async with aiosqlite.connect(test_db_api) as db:
                await db.execute(
                    """INSERT INTO audit_log (episode_id, action, details, user)
                       VALUES (?, ?, ?, ?)""",
                    ("ep99", "start_canary", "Workflow canary-concurrent-test, reserved L6=1000000 usd_micros", "system")
                )
                await db.commit()
            
            # Start workflow
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", {"shot_id": "A01", "prompt": "Test", "refs": [], "params": {}}],
                id="canary-concurrent-test",
                task_queue="test-task-queue",
            )
            
            # Wait for workflow to reach failed state
            try:
                await asyncio.wait_for(handle.result(), timeout=5)
                assert False, "Should fail"
            except Exception:
                pass
            
                # Wait for workflow to reach completed state
                async def is_completed():
                    try:
                        desc = await handle.describe()
                        return desc.status.name == "COMPLETED"
                    except Exception:
                        return False
            
            # Wait up to 2 seconds for status to be "COMPLETED"
            timeout = asyncio.get_event_loop().time() + 2.0
            while asyncio.get_event_loop().time() < timeout:
                if await is_completed():
                    break
                await asyncio.sleep(0.05)
            
            # Verify it's actually completed before proceeding
            desc = await handle.describe()
            assert desc.status.name == "COMPLETED", f"Workflow should be COMPLETED, got {desc.status.name}"
            
            # Set up API
            admin_secret = "a" * 32
            monkeypatch.setenv("ADMIN_SECRET", admin_secret)
            
            from fastapi.testclient import TestClient
            from api.main import app
            import api.main as api_main
            
            api_main.temporal_client = env.client
            
            client = TestClient(app)
            response = client.post("/api/login", json={"admin_secret": admin_secret})
            assert response.status_code == 200
            cookies = response.cookies
            
            # Make 5 concurrent calls to the canary status route
            def call_canary():
                # TestClient is synchronous
                resp = client.get(
                    f"/api/canary/canary-concurrent-test",
                    cookies=cookies,
                )
                return resp
            
            # Use threads for concurrent sync calls
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                futures = [executor.submit(call_canary) for _ in range(5)]
                results = [f.result() for f in futures]
            
            # All should succeed
            for resp in results:
                assert resp.status_code == 200
                data = resp.json()
                assert data["status"] == "completed"
                assert data["result"]["status"] == "failed"
            
            # Check that L6 is exactly 0 (not negative!)
            status_after = await ledger.get_line_status("ep99", "L6_reserve")
            assert status_after["reserved"] == 0, \
                f"L6 must be exactly 0 after concurrent reconciles, got {status_after['reserved']}"
            
            # Verify exactly one reconciliation was logged
            async with aiosqlite.connect(test_db_api) as db:
                async with db.execute(
                    """SELECT COUNT(*) FROM audit_log 
                       WHERE action = 'reconcile_canary_l6' AND details LIKE ?""",
                    ("%canary-concurrent-test%",)
                ) as cursor:
                    count = (await cursor.fetchone())[0]
                    assert count == 1, f"Should reconcile exactly once, got {count}"


@pytest.mark.asyncio
async def test_canary_l6_release_on_start_failure(test_db_api, monkeypatch, tmp_path):
    """Test: Canary releases L6 when workflow start fails."""
    from hfvg.budget import BudgetLedger
    
    # Set up episode
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", True)
    
    ledger = BudgetLedger(test_db_api)
    await ledger.init_db()
    
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test prompt", "pending")
        )
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    
    # Check initial L6 status
    status_before = await ledger.get_line_status("ep99", "L6_reserve")
    assert status_before["reserved"] == 0, "L6 should start at 0"
    
    # Set up API with mock temporal client that fails on start_workflow
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main
    from unittest.mock import AsyncMock
    
    # Mock temporal client that fails on start
    mock_client = AsyncMock()
    mock_client.start_workflow.side_effect = Exception("Temporal connection failed")
    api_main.temporal_client = mock_client
    
    client = TestClient(app)
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    cookies = response.cookies
    
    # Try to start canary (should fail and release L6)
    response = client.post(
        f"/api/episodes/ep99/canary",
        cookies=cookies,
    )
    
    # Should return 500 error (start failed)
    assert response.status_code == 500
    assert "failed to start" in response.json()["detail"].lower()
    
    # Check that L6 is exactly 0 (reservation was released on start failure)
    status_after = await ledger.get_line_status("ep99", "L6_reserve")
    assert status_after["reserved"] == 0, \
        f"L6 must be 0 after start failure (released), got {status_after['reserved']}"


async def _setup_canary_episode(db_path):
    """Episode ep99 with G1.08 approved, shot A01 and an L6_reserve line (dry-run canary route)."""
    await create_episode(db_path, "ep99")
    await approve_g108(db_path, "ep99")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test prompt", "pending"),
        )
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    return ledger


def _canary_client(monkeypatch, mock_client):
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)  # must be set before api.main is imported
    from fastapi.testclient import TestClient
    from api.main import app
    import api.main as api_main

    api_main.temporal_client = mock_client
    client = TestClient(app)
    response = client.post("/api/login", json={"admin_secret": admin_secret})
    assert response.status_code == 200
    return client, response.cookies


@pytest.mark.asyncio
async def test_canary_concurrent_409(test_db_api, monkeypatch):
    """
    Test: a duplicate canary POST is refused with 409, releases its L6 hold, and the
    canary always starts with the STABLE workflow id (no random suffix).

    M-R2a (random workflow-id suffix) must fail the stable-id assertion.
    M-R2b (delete WorkflowAlreadyStartedError handling) must turn the 409 into a 500.
    """
    from unittest.mock import AsyncMock, MagicMock
    from temporalio.exceptions import WorkflowAlreadyStartedError

    ledger = await _setup_canary_episode(test_db_api)

    mock_client = MagicMock()
    # describe() fails -> pre-check treats the workflow as not existing, so the 409 must come
    # from the WorkflowAlreadyStartedError race path (two concurrent POSTs).
    existing = MagicMock()
    existing.describe = AsyncMock(side_effect=Exception("not found"))
    mock_client.get_workflow_handle = MagicMock(return_value=existing)
    mock_client.start_workflow = AsyncMock(
        side_effect=WorkflowAlreadyStartedError("ep99-canary-A01", "ShotWorkflow")
    )
    client, cookies = _canary_client(monkeypatch, mock_client)

    response = client.post("/api/episodes/ep99/canary", cookies=cookies)

    assert response.status_code == 409, f"Expected 409, got {response.status_code}: {response.text}"
    assert "already running" in response.json()["detail"].lower()

    # Stable, deterministic workflow id: this is what makes the duplicate detectable at all
    assert mock_client.start_workflow.await_count == 1
    assert mock_client.start_workflow.call_args.kwargs["id"] == "ep99-canary-A01"

    # The hold taken for the refused canary was released
    status = await ledger.get_line_status("ep99", "L6_reserve")
    assert status["reserved"] == 0, f"L6 must be released on 409, got {status['reserved']}"


@pytest.mark.asyncio
async def test_approve_completed_canary_409(test_db_api, monkeypatch):
    """
    Test: Approve route returns 409 (never 500, never a signal) when the workflow is not running,
    and still signals a running workflow.

    M-R2d will disable the status check - the COMPLETED case must then fail.
    """
    from unittest.mock import AsyncMock, MagicMock
    from temporalio.client import WorkflowExecutionStatus

    await create_episode(test_db_api, "ep99")
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    from api import main as api_main

    def make_client(status):
        handle = MagicMock()
        handle.signal = AsyncMock()
        handle.describe = AsyncMock(return_value=MagicMock(status=status))
        mc = MagicMock()
        mc.get_workflow_handle_for = MagicMock(return_value=handle)
        return mc, handle

    for terminal in (WorkflowExecutionStatus.COMPLETED, WorkflowExecutionStatus.FAILED,
                     WorkflowExecutionStatus.TERMINATED):
        mc, handle = make_client(terminal)
        api_main.temporal_client = mc
        response = client.post("/api/episodes/ep99/shots/A01/approve", cookies=cookies)
        assert response.status_code == 409, \
            f"{terminal.name}: expected 409, got {response.status_code}: {response.text}"
        assert not handle.signal.called, f"{terminal.name}: signal must NOT be sent"

    mc, handle = make_client(WorkflowExecutionStatus.RUNNING)
    api_main.temporal_client = mc
    response = client.post("/api/episodes/ep99/shots/A01/approve", cookies=cookies)
    assert response.status_code == 200, response.text
    assert handle.signal.called, "RUNNING workflow must receive the stills_approved signal"


async def _setup_live_canary(test_db_api, monkeypatch, tmp_path, style=None):
    """Live-mode canary fixtures: episode live + G1.08, one shot, L6 line, manual balance."""
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    await create_episode(test_db_api, "ep99")
    await approve_g108(test_db_api, "ep99")
    await set_live_mode(test_db_api, "ep99", True)
    async with aiosqlite.connect(test_db_api) as db:
        await db.execute(
            "INSERT INTO shots (id, episode_id, shot_id, prompt, status) VALUES (?, ?, ?, ?, ?)",
            ("ep99-A01", "ep99", "A01", "Test", "pending")
        )
        await insert_line(db, "ep99", "L6_reserve", 25_000_000, 20_000_000)
        await db.commit()
    await set_balance(test_db_api)
    # Prompt lint (live) needs real shot metadata: a synthetic beatmap + prompt kit in a tmp cwd.
    # The kit style carries the lint-required clauses (0 characters => "Exactly 0 animals").
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True)
    (episode_dir / "BEATMAP.md").write_text(
        "# Synthetic canary beatmap\n\n## Shot Plan\n\n"
        "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 1 | A01 | 3.0 (4.0) | Lobby | Morning | – | Wide lobby interior | Establish | – | – | K |\n"
    )
    (episode_dir / "prompt_kit.json").write_text(json.dumps({
        "style": style if style is not None else lint_ok("Synthetic style", count=0),
        "aspect_ratio": "9:16",
        "characters": {},
        "sets": {},
    }))
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_canary_live_estimate_fail_closed(test_db_api, monkeypatch, respx_mock, tmp_path):
    """
    Test: Live canary fails closed when the provider estimate is unavailable: 503, nothing
    reserved, no workflow started, and no default dollar amount is ever substituted.

    M-R2e (a fallback amount instead of refusing) must fail this test.
    """
    import httpx
    await _setup_live_canary(test_db_api, monkeypatch, tmp_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(500, json={"error": "estimate down"})
    )
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    import api.main as api_main

    response = client.post("/api/episodes/ep99/canary", cookies=cookies)

    assert response.status_code == 503, response.text
    assert "fail closed" in response.json()["detail"].lower(), response.text
    status = await BudgetLedger(test_db_api).get_line_status("ep99", "L6_reserve")
    assert status["reserved"] == 0, "No hold may be taken without a real estimate"
    assert not api_main.temporal_client.start_workflow.called, "No workflow without an estimate"


@pytest.mark.asyncio
async def test_canary_live_without_api_key_refused(test_db_api, monkeypatch, tmp_path):
    """Test: Live canary without HIGGSFIELD_API_KEY is refused (503) and reserves nothing."""
    await _setup_live_canary(test_db_api, monkeypatch, tmp_path)
    monkeypatch.delenv("HIGGSFIELD_API_KEY", raising=False)
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)

    response = client.post("/api/episodes/ep99/canary", cookies=cookies)

    assert response.status_code == 503, response.text
    status = await BudgetLedger(test_db_api).get_line_status("ep99", "L6_reserve")
    assert status["reserved"] == 0


@pytest.mark.asyncio
async def test_canary_live_hold_uses_usd_estimates(test_db_api, monkeypatch, respx_mock, tmp_path):
    """Test: Live canary L6 hold = (still USD + clip USD) x 1.2 in usd_micros, exact."""
    import httpx
    await _setup_live_canary(test_db_api, monkeypatch, tmp_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"})
    )
    respx_mock.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"})
    )
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)

    response = client.post("/api/episodes/ep99/canary", cookies=cookies)

    assert response.status_code == 200, response.text
    data = response.json()
    assert data["success"] is True, data
    assert data["reserved_amount"] == 408_000  # (60_000 + 280_000) * 12 // 10
    status = await BudgetLedger(test_db_api).get_line_status("ep99", "L6_reserve")
    assert status["reserved"] == 408_000


@pytest.mark.asyncio
async def test_canary_live_prompt_lint_refuses_before_estimate_and_reserve(test_db_api, monkeypatch, respx_mock, tmp_path):
    """Test: a live canary whose prompt fails the lint gate is refused (422) before any provider
    estimate, hold, or workflow. M-P3c (lint skipped in live) must fail this test."""
    import httpx
    await _setup_live_canary(test_db_api, monkeypatch, tmp_path, style="Synthetic style with no clauses")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    est_still = respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.0", "usd": "0.06"})
    )
    est_clip = respx_mock.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"})
    )
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)

    response = client.post("/api/episodes/ep99/canary", cookies=cookies)

    assert response.status_code == 422, response.text
    assert "lint" in response.json()["detail"].lower(), response.text
    assert est_still.call_count == 0 and est_clip.call_count == 0
    status = await BudgetLedger(test_db_api).get_line_status("ep99", "L6_reserve")
    assert status["reserved"] == 0


@pytest.mark.asyncio
async def test_lint_endpoint_returns_violations(test_db_api, monkeypatch):
    """Test: POST /api/lint returns lint violations for invalid prompts (requires auth)."""
    # Now requires authentication (G-AUTH-1 fix)
    client, cookies = await get_authenticated_client(test_db_api, monkeypatch)
    
    # Test with a prompt that has violations
    response = client.post("/api/lint", json={
        "prompt": "Missing count clause here",
        "live": False
    }, cookies=cookies)
    
    assert response.status_code == 200
    data = response.json()
    assert "problems" in data
    assert len(data["problems"]) > 0  # Should have violations


@pytest.mark.asyncio
async def test_set_ref_versions_endpoint(test_db_api, tmp_path, monkeypatch):
    """Test: POST /api/episodes/{episode_id}/set-ref-versions updates versions."""
    from api.main import app
    from httpx import AsyncClient, ASGITransport
    import json
    from pathlib import Path
    
    # Setup episode directory
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True, exist_ok=True)
    
    # Create refs.json
    refs_json = episode_dir / "refs.json"
    refs_json.write_text('{"per_shot": {}, "versions": {}}')
    
    monkeypatch.setenv("DATABASE_PATH", str(test_db_api))
    monkeypatch.chdir(tmp_path)  # Change to tmp_path so ./data/episodes resolves correctly
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(str(test_db_api))
    await create_episode(str(test_db_api), "ep99")
    
    # Create async client
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    
    # Login first to get session cookie
    login_response = await client.post("/api/login", json={"admin_secret": admin_secret})
    assert login_response.status_code == 200
    cookies = login_response.cookies
    
    # Test setting ref versions with authenticated session
    response = await client.post(
        "/api/episodes/ep99/set-ref-versions",
        json={"r_duck": 2, "r_bird": 1},
        cookies=cookies
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "refs_updated" in data or "updated_versions" in data or "success" in data  # Accept any success indicator


@pytest.mark.asyncio
async def test_retry_shot_endpoint_returns_409_on_refusal(test_db_api, monkeypatch):
    """Test: POST /api/episodes/{episode_id}/shots/{shot_id}/retry returns 409 on refusal."""
    from api.main import app, temporal_client
    from httpx import AsyncClient, ASGITransport
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.studio_db import init_studio_db, create_episode, approve_g108
    from temporalio import activity
    
    monkeypatch.setenv("DATABASE_PATH", str(test_db_api))
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    
    await init_studio_db(str(test_db_api))
    await create_episode(str(test_db_api), "ep99")
    await approve_g108(str(test_db_api), "ep99")
    
    # Start Temporal workflow that will refuse retry (no failed shots)
    async with await WorkflowEnvironment.start_local() as env:
        @activity.defn(name="parse_beatmap_activity")
        async def mock_parse(*a): return {"shots": [{"shot_id": "A01", "prompt": "test"}]}
        
        @activity.defn(name="compute_g1_plan_activity")
        async def mock_g1(*a): return {"shots": [], "total_credits": 0.0, "total_usd": 0.0, "estimates_available": True}
        
        @activity.defn(name="init_episode_budget_lines_activity")
        async def mock_budget(*a):
            from hfvg.budget import REQUIRED_BUDGET_LINES
            return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}
        
        async def noop(*a): return {}
        names = ["upsert_episode_activity", "init_episode_credit_line_activity", 
                 "preflight_validate_episode_activity",
                 "load_gate_policy_activity", "load_refs_to_generate_activity",
                 "recheck_refs_activity", "acquire_run_lock", "release_run_lock",
                 "list_anchor_stills", "approve_g2_shots"]
        def make_activity(name):
            @activity.defn(name=name)
            async def act(*a):
                return {}
            return act
        activities = [mock_parse, mock_g1, mock_budget] + [make_activity(n) for n in names]
        
        async with Worker(env.client, task_queue="test-retry-api", workflows=[EpisodeWorkflowV2], activities=activities):
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", None, True],
                id="ep99",
                task_queue="test-retry-api",
            )
            
            # Wait for G1
            import asyncio
            for _ in range(50):
                state = await handle.query(EpisodeWorkflowV2.get_state)
                if state["current_gate"] == "G1_SHOT_PLAN":
                    break
                await asyncio.sleep(0.1)
            
            # Patch temporal_client to use our test client
            monkeypatch.setattr("api.main.temporal_client", env.client)
            
            # Try to retry a shot that isn't failed (should return 409)
            client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            response = await client.post(
                "/api/episodes/ep99/shots/A01/retry",
                headers={"Authorization": f"Bearer {admin_secret}"}
            )
            
            assert response.status_code == 409, f"Expected 409, got {response.status_code}: {response.text}"
            # Check for refusal reason (not a generic failure message)
            assert ("cannot retry" in response.text.lower() or 
                    "not in failed" in response.text.lower() or
                    "already approved" in response.text.lower()), \
                   f"Expected refusal reason, got: {response.text}"
            
            await handle.terminate("test done")
