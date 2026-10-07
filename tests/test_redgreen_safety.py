"""
Red-green safety tests: designed to fail when protections are removed.

These tests are specifically designed for mutation testing with scripts/redgreen.sh.
Each test exercises a critical safety mechanism and will fail if that mechanism is bypassed.
"""

import pytest
import aiosqlite

from hfvg.budget import BudgetLedger
from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
from hfvg.activities.studio_generation import (
    submit_still_job_enforced,
    submit_clip_job_enforced,
)


@pytest.fixture
async def test_db(tmp_path):
    """Create test database."""
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Initialize budget
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("""
            INSERT INTO budget_lines 
            (line_id, episode_id, provider, line_name, budget_cap, stop_threshold, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("ep99:L2_drafts", "ep99", "higgsfield", "L2_drafts", 100.0, 80.0, "credits"))
        await db.commit()
    
    yield db_path


@pytest.mark.asyncio
async def test_activity_level_enforcement(test_db, monkeypatch, respx_mock):
    """Test 4: Activity enforces live mode check directly (not just at API)."""
    import httpx
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Approve G1.08 but NOT live mode
    await approve_g108(test_db, "ep99")
    
    # Mock estimate endpoint (if check is bypassed, code will call this)
    mock_estimate = respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
    )
    
    # Mock provider submit (should never be called if protection works)
    mock_submit = respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123", "status": "queued"})
    )
    
    # Get ledger state before attempt
    ledger = BudgetLedger(test_db)
    await ledger.init_db()
    line_id = "ep99:L2_drafts"
    async with aiosqlite.connect(test_db) as db:
        async with db.execute(
            "SELECT reserved FROM budget_lines WHERE line_id = ?", (line_id,)
        ) as cursor:
            row = await cursor.fetchone()
            reserved_before = row[0] if row else 0.0
    
    # Activity should check live mode and refuse (activity-level enforcement)
    try:
        await submit_still_job_enforced("ep99", "A01", "Test", 1)
        # If mutation bypasses check, we reach here
        pytest.fail("Expected ValueError for missing live mode, but call succeeded")
    except ValueError as e:
        if "not in live mode" in str(e):
            # Correct: the protection worked
            pass
        else:
            # Wrong error
            raise
    
    # Assert provider was never called
    assert not mock_submit.called, "Provider should not be called without live mode"
    
    # Assert ledger unchanged (no budget reserved)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute(
            "SELECT reserved FROM budget_lines WHERE line_id = ?", (line_id,)
        ) as cursor:
            row = await cursor.fetchone()
            reserved_after = row[0] if row else 0.0
    assert reserved_after == reserved_before, "Budget should not be reserved without live mode"


@pytest.mark.asyncio
async def test_idempotent_retry_no_double_charge(test_db, monkeypatch, respx_mock):
    """Test 5: Idempotency key is actually sent to provider."""
    import httpx
    from hfvg.activities.studio_generation import generate_idempotency_key
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Calculate expected idempotency key with all parameters
    # submit_still_job_enforced defaults: refs=None, resolution="1k", quality="medium"
    expected_key = generate_idempotency_key(
        episode_id="ep99",
        shot_id="A01",
        version=1,
        prompt="Test prompt",
        refs=None,
        quality="medium",
        resolution="1k",
    )
    
    # Mock estimate endpoint
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
    )
    
    # Mock provider - this will catch calls to the provider
    mock_route = respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123", "status": "queued"})
    )
    
    # Call activity
    await submit_still_job_enforced("ep99", "A01", "Test prompt", 1)
    
    # Verify the EXACT deterministic key was sent (not a random UUID or None)
    assert mock_route.called, "Provider should be called"
    request = mock_route.calls[0].request
    actual_key = request.headers.get("Idempotency-Key")
    assert actual_key == expected_key, f"Idempotency-Key must match expected deterministic key. Expected: {expected_key}, Got: {actual_key}"


@pytest.mark.asyncio  
async def test_auth_fail_closed(monkeypatch, tmp_path):
    """Test 6: Auth rejects requests without valid secret (through real API app)."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi not available (API dependencies not installed)")
    
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    
    # Import AFTER setting env vars
    from api.main import app
    client = TestClient(app)
    
    # No auth - should fail
    response = client.get("/api/episodes/ep99")
    assert response.status_code == 401, "Should reject without auth"
    
    # Wrong auth - should also fail
    response = client.get("/api/episodes/ep99", headers={"Authorization": "Bearer wrong-secret"})
    assert response.status_code == 401 or response.status_code == 403, "Should reject wrong auth"


@pytest.mark.asyncio
async def test_canary_starts_shot_workflow(tmp_path, monkeypatch):
    """Test 9: Canary actually starts ShotWorkflow (not a stub)."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg import activities
    from hfvg.workflows.shot import ShotWorkflow
    from hfvg.studio_db import init_studio_db, create_episode
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    # Verify ShotWorkflow is properly decorated
    # Check for the __temporal_workflow_definition attribute that @workflow.defn adds
    assert hasattr(ShotWorkflow, "__temporal_workflow_definition"), \
        "ShotWorkflow must be decorated with @workflow.defn (missing __temporal_workflow_definition)"
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[ShotWorkflow],
            activities=[
                activities.submit_still_job_enforced,
                activities.await_job_enforced,
                activities.precheck_still_qc,
                activities.review_still,
                activities.record_shot_result,
            ],
        ):
            # Start ShotWorkflow (what canary does)
            handle = await env.client.start_workflow(
                ShotWorkflow.run,
                args=["ep99", {"shot_id": "A01", "prompt": "Test", "params": {"duration": 5.0}}],
                id="test-shot",
                task_queue="test-queue",
            )
            
            # Verify workflow actually started
            await env.sleep(0.1)
            
            # This should raise if workflow doesn't exist
            result = await handle.describe()
            assert result.status.name in ("RUNNING", "COMPLETED"), "ShotWorkflow should be running"


@pytest.mark.asyncio
async def test_approval_signal_reaches_episode_workflow(monkeypatch, tmp_path):
    """Test 10: Approval signal actually reaches running workflow."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg import activities
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    
    db_path = str(tmp_path / "test.db")
    await init_studio_db(db_path)
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
            ],
        ):
            # Start workflow
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep99", None, True],
                id="test-ep",
                task_queue="test-queue",
            )
            
            await env.sleep(0.1)
            
            # Check state before approval
            state = await handle.query("get_state")
            assert not state["approvals"]["g101"], "Should not be approved yet"
            
            # Send signal
            await handle.signal("approve_g101")
            await env.sleep(0.1)
            
            # Check state after - signal should have reached workflow
            state_after = await handle.query("get_state")
            assert state_after["approvals"]["g101"], "Signal should reach workflow and approve"


# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Task #2): Simpler tests for undetected protections
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_auth_rejects_invalid_secret(monkeypatch):
    """Test 11: API rejects requests with invalid/missing secrets."""
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    # Try with wrong secret (403 when secret doesn't match)
    response = client.post(
        "/api/episodes",
        json={"episode_id": "ep99"},
        headers={"Authorization": "Bearer wrong_secret"}
    )
    assert response.status_code == 403, "Should reject wrong secret"


@pytest.mark.asyncio
async def test_episode_cap_enforced(test_db):
    """Test 12: Episode cannot exceed 1,250 credit cap."""
    from hfvg.budget import BudgetLedger
    import aiosqlite
    
    ledger = BudgetLedger(test_db)
    await ledger.init_db()
    
    # Update line cap and stop threshold to be higher than episode cap
    async with aiosqlite.connect(test_db) as db:
        await db.execute(
            "UPDATE budget_lines SET budget_cap = ?, stop_threshold = ? WHERE line_id = ?",
            (2000.0, 1600.0, "ep99:L2_drafts")
        )
        await db.commit()
    
    # Try to reserve 1,251 credits (over episode cap of 1,250)
    exception_raised = False
    error_message = ""
    try:
        await ledger.reserve("ep99", "L2_drafts", 1251.0, "Over cap")
    except ValueError as e:
        exception_raised = True
        error_message = str(e)
    
    # MUST raise ValueError (not just return False)
    assert exception_raised, \
        "Episode cap check must raise ValueError, not silently return False"
    assert "1,250 credit cap" in error_message, \
        f"Error should mention 1,250 credit cap, got: {error_message}"


@pytest.mark.asyncio
async def test_dry_run_forces_dry_never_live(test_db, monkeypatch, respx_mock):
    """Test 13: DRY_RUN=true forces dry mode (never forces live)."""
    import httpx
    from hfvg.activities.studio_generation import submit_still_job_enforced
    
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Even with live mode and G1.08 enabled in DB
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Mock estimate endpoint (should NOT be called in dry mode)
    estimate_mock = respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
    )
    
    # Mock generation endpoint (should NOT be called in dry mode)
    generate_mock = respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123"})
    )
    
    # Should return dry job (not call provider)
    result = await submit_still_job_enforced("ep99", "A01", "Test prompt", 1)
    
    assert result["job_id"].startswith("still-dry-"), "Should return dry job"
    assert result["reserved_amount"] == 2.5, "Should return estimate"
    
    # Key assertion: provider endpoints should NOT be called when DRY_RUN=true
    assert not estimate_mock.called, "Should NOT call estimate endpoint in dry mode"
    assert not generate_mock.called, "Should NOT call generate endpoint in dry mode"


@pytest.mark.asyncio
async def test_canary_checks_g108(test_db, monkeypatch):
    """Test 14: Canary route check for G1.08 (simpler logic test)."""
    # This test verifies the G1.08 check logic that the canary route uses
    monkeypatch.setenv("DATABASE_PATH", test_db)
    
    from hfvg.studio_db import create_episode, approve_g108
    from hfvg.activities.studio_generation import check_live_mode_and_g108
    
    # Create episode without G1.08 approval
    await create_episode(test_db, "ep99")
    
    # Check should return (False, False)
    live_mode, g108_approved = await check_live_mode_and_g108("ep99")
    assert not g108_approved, "G1.08 should not be approved"
    
    # Approve G1.08
    await approve_g108(test_db, "ep99")
    
    # Check should return (False, True)
    live_mode, g108_approved = await check_live_mode_and_g108("ep99")
    assert g108_approved, "G1.08 should be approved"


@pytest.mark.asyncio
async def test_canary_checks_live_mode_in_live(test_db, monkeypatch):
    """Test 15: Canary logic for live mode (simpler logic test)."""
    # This test verifies the live mode check logic used by canary
    monkeypatch.setenv("DATABASE_PATH", test_db)
    
    from hfvg.studio_db import create_episode, set_live_mode
    from hfvg.activities.studio_generation import check_live_mode_and_g108
    
    # Create episode with live mode OFF
    await create_episode(test_db, "ep99")
    await set_live_mode(test_db, "ep99", False)
    
    # Check should return (False, False)
    live_mode, g108_approved = await check_live_mode_and_g108("ep99")
    assert not live_mode, "Live mode should be OFF"
    
    # Enable live mode
    await set_live_mode(test_db, "ep99", True)
    
    # Check should return (True, False)
    live_mode, g108_approved = await check_live_mode_and_g108("ep99")
    assert live_mode, "Live mode should be ON"


@pytest.mark.asyncio
async def test_still_activity_reserves_budget(test_db, monkeypatch, respx_mock):
    """Test 16: Still submit activity actually reserves from budget before calling provider."""
    import httpx
    from hfvg.budget import BudgetLedger
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Mock estimate endpoint
    respx_mock.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "4.0", "usd": "0.04"})
    )
    
    # Mock provider
    respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123", "status": "queued"})
    )
    
    # Check initial reserved amount
    ledger = BudgetLedger(test_db)
    status_before = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_before["reserved"] == 0, "Should start with 0 reserved"
    
    # Call activity
    result = await submit_still_job_enforced("ep99", "A01", "Test prompt", 1)
    
    # Check reserved amount increased
    status_after = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_after["reserved"] > 0, "Should have reserved budget"
    assert result["reserved_amount"] > 0, "Should return reserved amount"


@pytest.mark.asyncio
async def test_clip_idempotency_key_sent(test_db, monkeypatch, respx_mock):
    """Test 17: Clip submit sends Idempotency-Key header to provider."""
    import httpx
    from hfvg.activities.studio_generation import generate_idempotency_key
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("KLING_MODEL_PATH", "kling-video/v3.0/pro/image-to-video")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Initialize budget for L4_video
    ledger = BudgetLedger(test_db)
    async with aiosqlite.connect(test_db) as db:
        await db.execute("""
            INSERT INTO budget_lines 
            (line_id, episode_id, provider, line_name, budget_cap, stop_threshold, unit)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("ep99:L4_video", "ep99", "higgsfield", "L4_video", 500.0, 400.0, "credits"))
        await db.commit()
    
    # Calculate expected key with all parameters
    # submit_clip_job_enforced will be called with: episode_id, shot_id, start_image_url, prompt, duration, version
    expected_key = generate_idempotency_key(
        episode_id="ep99",
        shot_id="A01",
        version=1,
        prompt="clip:Test prompt",  # Activity prefixes with "clip:"
        start_image="https://example.com/still.jpg",
        duration=5.0,
    )
    
    # Mock estimate endpoint
    respx_mock.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.075"})
    )
    
    # Mock provider
    mock_route = respx_mock.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123", "status": "queued"})
    )
    
    # Call clip activity
    result = await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/still.jpg", "Test prompt", 5.0, 1
    )
    
    # Verify idempotency key was sent
    assert mock_route.called, "Provider should be called"
    request = mock_route.calls[0].request
    actual_key = request.headers.get("Idempotency-Key")
    assert actual_key == expected_key, f"Idempotency-Key mismatch. Expected: {expected_key}, Got: {actual_key}"
