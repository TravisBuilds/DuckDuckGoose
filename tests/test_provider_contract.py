"""Contract tests for provider method calls.

Ensures activities call real provider methods (not mocked), catching AttributeError
before CI passes.
"""

import pytest
import respx
import httpx


@pytest.mark.asyncio
async def test_higgsfield_provider_has_poll_job_method():
    """Test: HiggsfieldProvider has poll_job method."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    provider = HiggsfieldProvider(dry_run=True)
    assert hasattr(provider, "poll_job")
    assert callable(getattr(provider, "poll_job"))


@pytest.mark.asyncio
async def test_higgsfield_provider_has_get_job_status_method():
    """Test: HiggsfieldProvider has get_job_status method."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    provider = HiggsfieldProvider(dry_run=True)
    assert hasattr(provider, "get_job_status")
    assert callable(getattr(provider, "get_job_status"))


@pytest.mark.asyncio
async def test_ref_generation_calls_real_provider_methods(tmp_path, monkeypatch):
    """Test: ref generation activity calls real provider methods with respx mock."""
    from hfvg.activities.ref_generation import generate_ref_activity
    from hfvg.studio_db import init_studio_db
    from hfvg.credit_ledger import CreditLedger
    import temporalio.activity
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-id:test-secret")
    
    await init_studio_db(db_path)
    
    # Initialize budget ledger with manual balance for GC.01 check
    from hfvg.budget import BudgetLedger
    budget_ledger = BudgetLedger(db_path)
    await budget_ledger.init_db()
    await budget_ledger.set_manual_balance(100_000_000, "test", "test balance")
    # Create L1_refs line for ref generation
    await budget_ledger.set_line("ep99", "L1_refs", 100_000_000, 80_000_000, "higgsfield")
    
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    output_path = str(tmp_path / "refs" / "duck.png")
    
    # Mock activity info
    original_info = temporalio.activity.info
    
    # Track which provider methods were actually called
    called_methods = []
    
    # Mock the QC check to always pass (not what we're testing)
    from hfvg.activities import ref_generation
    original_check_qc = ref_generation._check_ref_qc
    
    async def mock_qc(*args, **kwargs):
        return {"passed": True}
    
    ref_generation._check_ref_qc = mock_qc
    
    # Wrap provider methods to track calls
    from hfvg.providers import higgsfield
    original_submit = higgsfield.HiggsfieldProvider.submit_image
    original_get_status = higgsfield.HiggsfieldProvider.get_job_status
    original_poll = higgsfield.HiggsfieldProvider.poll_job
    
    async def tracked_submit(self, *args, **kwargs):
        called_methods.append("submit_image")
        return await original_submit(self, *args, **kwargs)
    
    async def tracked_get_status(self, *args, **kwargs):
        called_methods.append("get_job_status")
        return await original_get_status(self, *args, **kwargs)
    
    async def tracked_poll(self, *args, **kwargs):
        called_methods.append("poll_job")
        return await original_poll(self, *args, **kwargs)
    
    try:
        temporalio.activity.info = lambda: type('obj', (), {'workflow_id': 'test-wf', 'workflow_client': lambda: None})()
        
        # Patch provider methods with tracked versions
        higgsfield.HiggsfieldProvider.submit_image = tracked_submit
        higgsfield.HiggsfieldProvider.get_job_status = tracked_get_status
        higgsfield.HiggsfieldProvider.poll_job = tracked_poll
        
        # Mock HTTP responses
        with respx.mock:
            respx.post("https://api.higgsfield.ai/higgsfield-ai/soul/v2/standard").mock(
                return_value=httpx.Response(200, json={"request_id": "req-test-123"})
            )
            respx.get("https://api.higgsfield.ai/requests/req-test-123/status").mock(
                return_value=httpx.Response(200, json={
                    "status": "completed",
                    "images": [{"url": "https://example.com/output.png"}]
                })
            )
            respx.get("https://example.com/output.png").mock(
                return_value=httpx.Response(200, content=b"fake-image-data" * 1000)  # Make it large enough to pass QC
            )
            
            result = await generate_ref_activity(
                episode_id="ep99",
                ref_key="r_duck",
                prompt="Duck character sheet",
                output_path=output_path,
                version=1,
            )
            
            # Verify result
            assert result["status"] == "completed"
            
            # Verify that real provider methods were called (not mocked)
            assert "submit_image" in called_methods, "submit_image should be called"
            assert "poll_job" in called_methods, "poll_job should be called"
            assert "get_job_status" in called_methods, "get_job_status should be called (via poll_job)"
    
    finally:
        temporalio.activity.info = original_info
        # Restore original methods
        higgsfield.HiggsfieldProvider.submit_image = original_submit
        higgsfield.HiggsfieldProvider.get_job_status = original_get_status
        higgsfield.HiggsfieldProvider.poll_job = original_poll
        ref_generation._check_ref_qc = original_check_qc


@pytest.mark.asyncio
async def test_provider_method_calls_with_dry_run():
    """Test: Provider methods work in dry run mode."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    
    provider = HiggsfieldProvider(dry_run=True)
    
    # Test submit_image
    job_id = await provider.submit_image(
        prompt="Test prompt",
        model="soul_v2",
        resolution="2k",
    )
    assert job_id is not None
    
    # Test get_job_status
    status = await provider.get_job_status(job_id)
    assert status.status.value == "completed"
    assert status.output_url is not None
    
    # Test poll_job
    result = await provider.poll_job(job_id, max_polls=5)
    assert result.status.value == "completed"
    assert result.output_url is not None


@pytest.mark.asyncio
async def test_all_provider_activities_have_required_methods():
    """Test: All provider classes have required methods for their use cases."""
    from hfvg.providers.higgsfield import HiggsfieldProvider
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    from hfvg.providers.kling_video import KlingVideoProvider
    from hfvg.providers.seedance_video import SeedanceVideoProvider
    
    # HiggsfieldProvider (used for refs)
    provider = HiggsfieldProvider(dry_run=True)
    assert hasattr(provider, "submit_image")
    assert hasattr(provider, "get_job_status")
    assert hasattr(provider, "poll_job")
    
    # HiggsfieldStillProvider (used for stills)
    still_provider = HiggsfieldStillProvider()
    assert hasattr(still_provider, "submit_image")
    assert hasattr(still_provider, "get_job_status")
    assert hasattr(still_provider, "estimate_usd_micros")
    
    # Video providers
    kling = KlingVideoProvider()
    assert hasattr(kling, "submit_video")
    assert hasattr(kling, "get_job_status")
    
    seedance = SeedanceVideoProvider()
    assert hasattr(seedance, "submit_video")
    assert hasattr(seedance, "get_job_status")
