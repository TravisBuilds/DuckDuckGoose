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
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key")
    
    # Approve G1.08 but NOT live mode
    await approve_g108(test_db, "ep99")
    
    # Mock provider to avoid network call
    respx_mock.post("https://api.higgsfield.ai/v1/generate/image").mock(
        return_value=httpx.Response(200, json={"job_id": "test-123"})
    )
    
    # Activity should check live mode and refuse (activity-level enforcement)
    with pytest.raises(ValueError, match="not in live mode"):
        await submit_still_job_enforced("ep99", "A01", "Test", 1)


@pytest.mark.asyncio
async def test_idempotent_retry_no_double_charge(test_db, monkeypatch, respx_mock):
    """Test 5: Idempotency key is actually sent to provider."""
    import httpx
    from hfvg.activities.studio_generation import generate_idempotency_key
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "v1/generate/image")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Calculate expected idempotency key
    expected_key = generate_idempotency_key("ep99", "A01", 1, "Test prompt")
    
    # Mock provider - this will catch calls to the provider
    mock_route = respx_mock.post("https://api.higgsfield.ai/v1/generate/image").mock(
        return_value=httpx.Response(200, json={"job_id": "test-123"})
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
