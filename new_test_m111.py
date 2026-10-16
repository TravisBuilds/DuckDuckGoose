@pytest.mark.asyncio
@respx.mock
async def test_workflow_polls_with_provider_id_not_harness_id(tmp_path):
    """
    BUG 2: await_job_enforced must poll with provider ID, not harness ID.
    
    The workflow activities use harness IDs (still-ep-shot-vN), but provider APIs
    require provider IDs. The await_job_enforced activity must look up the provider
    ID from the harness ID before polling.
    
    This test verifies that when poll_job_status is called with a harness ID,
    it correctly resolves to the provider ID and polls the provider API with it.
    """
    # Setup test environment
    import os
    os.environ["HIGGSFIELD_API_KEY"] = "test_id:test_secret"
    os.environ["HIGGSFIELD_BASE_URL"] = "http://mock.api"
    os.environ["DATABASE_PATH"] = str(tmp_path / "test.db")
    os.environ["DRY_RUN"] = "false"
    
    # Initialize DB
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    await init_studio_db(os.environ["DATABASE_PATH"])
    await create_episode(os.environ["DATABASE_PATH"], "ep06", None)
    await set_live_mode(os.environ["DATABASE_PATH"], "ep06", True, "test")
    await approve_g108(os.environ["DATABASE_PATH"], "ep06", "test")
    
    # Initialize ledgers
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    budget_ledger = BudgetLedger(os.environ["DATABASE_PATH"])
    await budget_ledger.init_db()
    await budget_ledger.set_line("ep06", "L3_final_stills", 100_000_000, 80_000_000, "higgsfield")
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    
    credit_ledger = CreditLedger(os.environ["DATABASE_PATH"])
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep06", cap_credits=1000)
    
    # Mock Higgsfield API
    estimate_route = respx.post("http://mock.api/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "1.12", "usd": "1.12"})
    )
    
    submit_route = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "provider_still_xyz123"})
    )
    
    # Mock poll with provider ID - this is the critical check
    poll_provider_route = respx.get("http://mock.api/requests/provider_still_xyz123/status").mock(
        return_value=httpx.Response(200, json={
            "status": "completed",
            "image": {"url": "http://example.com/result.jpg"},
            "cost": 1.12
        })
    )
    
    # Mock poll with harness ID - this should NEVER be called
    poll_harness_route = respx.get("http://mock.api/requests/still-ep06-A01-v1/status").mock(
        return_value=httpx.Response(404, json={"error": "Job not found"})
    )
    
    # Submit still job
    from hfvg.activities.studio_generation import submit_still_job_enforced, poll_job_status
    
    result = await submit_still_job_enforced(
        episode_id="ep06",
        shot_id="A01",
        prompt="Test shot",
        params={
            "resolution": "1k",
            "quality": "medium",
            "aspect_ratio": "9:16"
        },
        refs=[],
        version=1
    )
    
    harness_job_id = result["job_id"]
    assert harness_job_id == "still-ep06-A01-v1"
    
    # Poll with harness ID - it should resolve to provider ID internally
    poll_result = await poll_job_status(
        job_id=harness_job_id,
        job_type="still",
        episode_id="ep06"
    )
    
    # CRITICAL ASSERTIONS:
    # 1. Poll succeeded using provider ID
    assert poll_result["status"] == "completed"
    assert poll_result["output_url"] == "http://example.com/result.jpg"
    
    # 2. Provider API was called with PROVIDER ID, not harness ID
    assert poll_provider_route.call_count == 1, "Provider API must be called with provider ID"
    assert poll_harness_route.call_count == 0, "Provider API must NOT be called with harness ID"


