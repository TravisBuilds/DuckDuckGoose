"""Tests for job_id versioning on retries."""

import pytest
import httpx
import respx

from hfvg.activities.studio_generation import submit_still_job_enforced, submit_clip_job_enforced
from hfvg.studio_db import init_studio_db, create_episode
from hfvg.credit_ledger import CreditLedger
from hfvg.budget import BudgetLedger
from tests.lint_helpers import lint_ok, lint_meta
from tests.ledger_helpers import set_balance


@pytest.mark.asyncio
@respx.mock
async def test_same_version_generates_same_job_id(tmp_path, monkeypatch):
    """
    Test: Calling submit activity with same version generates same job_id.
    
    This simulates Temporal activity retries - the workflow version doesn't change,
    so each retry attempt generates the same deterministic job_id.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Initialize DB
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    # Initialize budget and credit ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await set_balance(db_path)
    await ledger.set_line("ep99", "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Mock provider API
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"})
    )
    respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "provider-123"})
    )
    
    # Call submit activity multiple times with version 1
    result1 = await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Test prompt"), version=1, shot_meta=lint_meta()
    )
    
    # Reset credit ledger for second call (simulate idempotent retry)
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    result2 = await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Test prompt"), version=1, shot_meta=lint_meta()
    )
    
    # Both calls should generate the same job_id
    assert result1["job_id"] == "still-ep99-A01-v1"
    assert result2["job_id"] == "still-ep99-A01-v1"
    assert result1["job_id"] == result2["job_id"]


@pytest.mark.asyncio
@respx.mock
async def test_incremented_version_generates_new_job_id(tmp_path, monkeypatch):
    """
    Test: Calling submit activity with incremented version generates new job_id.
    
    This simulates workflow-level retry/reroll - the workflow version is incremented,
    so a new deterministic job_id is generated (v2 instead of v1).
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_GPT_IMAGE_2", "xai/grok-imagine-image-2.0")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Initialize DB
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    # Initialize budget and credit ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await set_balance(db_path)
    await ledger.set_line("ep99", "L2_drafts", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Mock provider API
    respx.post("https://api.higgsfield.ai/estimate/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"credits": "6.5", "usd": "0.39"})
    )
    respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "provider-123"})
    )
    
    # First call with version 1
    result_v1 = await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Test prompt"), version=1, shot_meta=lint_meta()
    )
    
    # Second call with version 2 (simulates reroll)
    # Need more credits for second submission
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    result_v2 = await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Test prompt"), version=2, shot_meta=lint_meta()
    )
    
    # Third call with version 3
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    result_v3 = await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Test prompt"), version=3, shot_meta=lint_meta()
    )
    
    # Each version should generate a unique job_id
    assert result_v1["job_id"] == "still-ep99-A01-v1"
    assert result_v2["job_id"] == "still-ep99-A01-v2"
    assert result_v3["job_id"] == "still-ep99-A01-v3"
    assert len({result_v1["job_id"], result_v2["job_id"], result_v3["job_id"]}) == 3


@pytest.mark.asyncio
@respx.mock
async def test_clip_job_id_versioning(tmp_path, monkeypatch):
    """
    Test: Clip job_ids also use version for deterministic generation.
    """
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_KLING", "kling-video/v3.0/pro/image-to-video")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    
    # Initialize DB
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    
    # Set live mode and G1.08
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = ?",
            ("ep99",)
        )
        await db.commit()
    
    # Initialize budget and credit ledgers
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await set_balance(db_path)
    await ledger.set_line("ep99", "L4_video", cap_usd_micros=10_000_000, stop_usd_micros=8_000_000)
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    
    # Mock provider API
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.45"})
    )
    respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "provider-456"})
    )
    
    # Call with version 1
    result_v1 = await submit_clip_job_enforced(
        "ep99", "B01", "https://fake/still.jpg", lint_ok("Test prompt"), 
        version=1, duration=5.0, shot_meta=lint_meta()
    )
    
    # Call with version 2
    await credit_ledger.create_episode_line("ep99", cap_credits=500)
    result_v2 = await submit_clip_job_enforced(
        "ep99", "B01", "https://fake/still.jpg", lint_ok("Test prompt"), 
        version=2, duration=5.0, shot_meta=lint_meta()
    )
    
    # Each version should generate a unique clip job_id
    assert result_v1["job_id"] == "clip-ep99-B01-v1"
    assert result_v2["job_id"] == "clip-ep99-B01-v2"
