"""
Red-green safety tests: designed to fail when protections are removed.

These tests are specifically designed for mutation testing with scripts/redgreen.sh.
Each test exercises a critical safety mechanism and will fail if that mechanism is bypassed.
"""

import hashlib
import json
import pytest
from temporalio import activity
from tests.lint_helpers import lint_ok, lint_meta
from tests.ledger_helpers import insert_line, set_balance
import aiosqlite

from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger
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
        await insert_line(db, "ep99", "L2_drafts", 10_000_000, 8_000_000)
        await db.commit()
    await set_balance(db_path)  # GC.01: live paid jobs need a manual balance
    
    # Initialize credit ledger
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    yield db_path


@pytest.mark.asyncio
async def test_activity_level_enforcement(test_db, monkeypatch, respx_mock):
    """Test 4: Activity enforces live mode check directly (not just at API)."""
    import httpx
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
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
            "SELECT reserved_usd_micros FROM budget_lines WHERE line_id = ?", (line_id,)
        ) as cursor:
            row = await cursor.fetchone()
            reserved_before = row[0] if row else 0.0
    
    # Activity should check live mode and refuse (activity-level enforcement)
    call_succeeded = False
    try:
        await submit_still_job_enforced("ep99", "A01", "Test", 1)
        call_succeeded = True
    except ValueError as e:
        assert "not in live mode" in str(e), f"Expected live mode error, got: {e}"
    
    assert not call_succeeded, "Call should have raised ValueError for missing live mode"
    assert not mock_submit.called, "Provider should not be called without live mode"
    
    # Assert ledger unchanged (no budget reserved)
    async with aiosqlite.connect(test_db) as db:
        async with db.execute(
            "SELECT reserved_usd_micros FROM budget_lines WHERE line_id = ?", (line_id,)
        ) as cursor:
            row = await cursor.fetchone()
            reserved_after = row[0] if row else 0.0
    assert reserved_after == reserved_before, "Budget should not be reserved without live mode"


@pytest.mark.asyncio
async def test_idempotent_retry_no_double_charge(test_db, monkeypatch, respx_mock):
    """Test 5: Identical inputs produce the same idempotency key; changed prompt produces different key."""
    import httpx
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
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
    
    # Mock provider - this will catch calls to the provider
    mock_route = respx_mock.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "test-123", "status": "queued"})
    )
    
    # Call activity first time
    await submit_still_job_enforced("ep99", "A01", lint_ok("Test prompt"), 1, shot_meta=lint_meta())
    
    # Get idempotency key from first call
    assert mock_route.call_count == 1, "Provider should be called once"
    first_request = mock_route.calls[0].request
    first_key = first_request.headers.get("Idempotency-Key")
    assert first_key is not None, "Idempotency-Key header must be present"
    
    # Call activity second time with identical inputs
    await submit_still_job_enforced("ep99", "A01", lint_ok("Test prompt"), 1, shot_meta=lint_meta())
    
    # Get idempotency key from second call
    assert mock_route.call_count == 2, "Provider should be called twice"
    second_request = mock_route.calls[1].request
    second_key = second_request.headers.get("Idempotency-Key")
    
    # Identical inputs should produce the same key
    assert second_key == first_key, f"Identical inputs should give same key: {first_key} vs {second_key}"
    
    # Call with changed prompt
    await submit_still_job_enforced("ep99", "A01", lint_ok("Different prompt"), 1, shot_meta=lint_meta())
    
    # Get idempotency key from third call
    assert mock_route.call_count == 3, "Provider should be called three times"
    third_request = mock_route.calls[2].request
    third_key = third_request.headers.get("Idempotency-Key")
    
    # Changed prompt should produce a different key
    assert third_key != first_key, f"Changed prompt should give different key: {first_key} vs {third_key}"


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
                activities.poll_job_status,
                activities.commit_job_budget,
                activities.release_job_budget,
                activities.mark_job_pending_reconcile,
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
    
    # Create minimal episode files for preflight validation
    episode_dir = tmp_path / "data" / "episodes" / "ep99"
    episode_dir.mkdir(parents=True, exist_ok=True)
    (episode_dir / "BEATMAP.md").write_text("A01 (2.5s)")
    (episode_dir / "shots.json").write_text(json.dumps({
        "shots": [{
            "id": "A01",
            "prompt": "Test",
            "count": 1,
            "characters": ["D"],
            "wet": False,
            "model": "kling",
            "duration": 3,
            "end_frame": False,
            "ref_keys": []
        }]
    }))
    (episode_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": []}}))
    (episode_dir / "prompt_kit.json").write_text('{}')
    
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    
    from hfvg.activities.run_lock import acquire_run_lock, release_run_lock
    from hfvg.activities.credit_activities import init_episode_credit_line_activity, init_episode_budget_lines_activity
    from hfvg.activities.ref_generation import generate_ref_activity, load_refs_to_generate_activity
    from hfvg.activities.g1_plan import compute_g1_plan_activity
    from hfvg.studio_db import list_anchor_stills, approve_g2_shots
    
    @activity.defn(name="recheck_refs_activity")
    async def noop_recheck(*a): return {}
    
    @activity.defn(name="verify_episode_files_activity")
    async def noop_verify(*a): return {}
    
    @activity.defn(name="update_credit_cap_activity")
    async def noop_update_cap(*a): return {}
    
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(
            env.client,
            task_queue="test-queue",
            workflows=[EpisodeWorkflowV2],
            activities=[
                activities.load_gate_policy_activity,
                activities.parse_beatmap_activity,
                activities.preflight_validate_episode_activity,
                activities.upsert_episode_activity,
                activities.approve_g108_activity,
                init_episode_credit_line_activity,
                init_episode_budget_lines_activity,
                load_refs_to_generate_activity,
                generate_ref_activity,
                acquire_run_lock,
                release_run_lock,
                compute_g1_plan_activity,
                list_anchor_stills,
                approve_g2_shots,
                noop_recheck,
                noop_verify,
                noop_update_cap,
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
            assert not state["approvals"]["g1_shot_plan"], "Should not be approved yet"
            
            # Send update (was signal, now update)
            await handle.execute_update("approve_g1_shot_plan")
            await env.sleep(0.1)
            
            # Check state after - update should have reached workflow
            state_after = await handle.query("get_state")
            assert state_after["approvals"]["g1_shot_plan"], "Update should reach workflow and approve"


# ─────────────────────────────────────────────────────────────────────────────
# NEW MUTATIONS (Task #2): Simpler tests for undetected protections
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_auth_rejects_invalid_secret(monkeypatch):
    """Test 11: API /api/login rejects invalid admin_secret in POST body."""
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    
    from fastapi.testclient import TestClient
    from api.main import app
    
    client = TestClient(app)
    
    # Try login with wrong secret - should get 401
    response = client.post(
        "/api/login",
        json={"admin_secret": "wrong_secret"}
    )
    assert response.status_code == 401, f"Should reject wrong secret, got {response.status_code}"
    
    # Try login with no secret - should get 401 or 422
    response = client.post(
        "/api/login",
        json={}
    )
    assert response.status_code in [401, 422], f"Should reject missing secret, got {response.status_code}"


@pytest.mark.asyncio
async def test_episode_cap_enforced(test_db):
    """Test 12: Episode cannot exceed the 60.00 USD episode cap."""
    from hfvg.budget import BudgetLedger
    import aiosqlite
    
    ledger = BudgetLedger(test_db)
    await ledger.init_db()
    
    # Line cap and stop higher than the episode cap (100.00 USD each)
    await ledger.set_line("ep99", "L2_drafts", 100_000_000, 100_000_000)
    
    # Try to reserve 60.000001 USD (1 micro-dollar over the 60.00 USD episode cap)
    exception_raised = False
    error_message = ""
    try:
        await ledger.reserve("ep99", "L2_drafts", 60_000_001, "Over cap")
    except ValueError as e:
        exception_raised = True
        error_message = str(e)
    
    # MUST raise ValueError (not just return False)
    assert exception_raised, \
        "Episode cap check must raise ValueError, not silently return False"
    assert "episode cap" in error_message.lower() and "60.00 USD" in error_message, \
        f"Error should mention the 60.00 USD episode cap, got: {error_message}"
    status = await ledger.get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0, "Nothing may be reserved when the cap check fails"


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
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    
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
    result = await submit_still_job_enforced("ep99", "A01", lint_ok("Test prompt"), 1, shot_meta=lint_meta())
    
    assert result["job_id"].startswith("still-dry-"), f"Should return dry job, got {result['job_id']}"
    assert result["reserved_amount"] == 60_000, "Should return the dry estimate (0.06 USD in usd_micros)"
    
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
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Ensure credit line exists
    credit_ledger = CreditLedger(test_db)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
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
    result = await submit_still_job_enforced("ep99", "A01", lint_ok("Test prompt"), 1, shot_meta=lint_meta())
    
    # Check reserved amount increased
    status_after = await ledger.get_line_status("ep99", "L2_drafts")
    assert status_after["reserved"] == 40_000, "Should have reserved the 0.04 USD estimate"
    assert result["reserved_amount"] == 40_000, "Should return the reserved usd_micros"


@pytest.mark.asyncio
async def test_clip_idempotency_key_sent(test_db, monkeypatch, respx_mock):
    """Test 17: Clip submit sends Idempotency-Key header to provider."""
    import httpx
    from hfvg.activities.studio_generation import generate_idempotency_key
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("DATABASE_PATH", test_db)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("KLING_MODEL_PATH", "kling-video/v3.0/pro/image-to-video")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    await set_live_mode(test_db, "ep99", True)
    await approve_g108(test_db, "ep99")
    
    # Initialize budget for L4_video
    ledger = BudgetLedger(test_db)
    async with aiosqlite.connect(test_db) as db:
        await insert_line(db, "ep99", "L4_video", 50_000_000, 40_000_000)
        await db.commit()
    
    # Idempotency key is now job_id + hash of request params
    # Format: clip-ep99-A01-v1-{hash}
    expected_key_prefix = "clip-ep99-A01-v1-"
    
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
        "ep99", "A01", "https://example.com/still.jpg", lint_ok("Test prompt"), 5.0, 1,
        shot_meta=lint_meta(),
    )
    
    # Verify idempotency key was sent with correct format
    assert mock_route.called, "Provider should be called"
    request = mock_route.calls[0].request
    actual_key = request.headers.get("Idempotency-Key")
    assert actual_key is not None, "Idempotency-Key header must be present"
    assert actual_key.startswith(expected_key_prefix), f"Idempotency-Key should start with {expected_key_prefix}, got: {actual_key}"
    # Verify hash suffix is 16 hex chars
    hash_suffix = actual_key[len(expected_key_prefix):]
    assert len(hash_suffix) == 16, f"Hash suffix should be 16 chars, got {len(hash_suffix)}"
    assert all(c in "0123456789abcdef" for c in hash_suffix), f"Hash suffix should be hex, got: {hash_suffix}"
