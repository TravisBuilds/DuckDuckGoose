"""Test core logic without Temporal: idempotency, credits, heartbeats."""

import asyncio

import pytest

from hfvg.errors import InsufficientCreditsError
from hfvg.ledger import Ledger
from hfvg.models import GenerationRequest


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_duplicate_submit_same_idempotency_key(tmp_path):
    """Test that duplicate requests generate the same idempotency key."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    req1 = GenerationRequest(
        shot_id="shot-001",
        version=1,
        prompt="A cozy kitchen",
        refs=["ref1.jpg"],
        params={"type": "still"},
        estimated_cost=6.5,
    )

    req2 = GenerationRequest(
        shot_id="shot-001",
        version=1,
        prompt="A cozy kitchen",
        refs=["ref1.jpg"],
        params={"type": "still"},
        estimated_cost=6.5,
    )

    key1 = ledger.idempotency_key(req1)
    key2 = ledger.idempotency_key(req2)

    assert key1 == key2

    await ledger.insert_job(key1, "job-123", "ep-001", "shot-001", req1)
    
    existing = await ledger.get_job(key2)
    assert existing is not None
    assert existing["provider_job_id"] == "job-123"


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_different_versions_different_keys(tmp_path):
    """Test that different versions create different idempotency keys."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)

    req_v1 = GenerationRequest(
        shot_id="shot-001",
        version=1,
        prompt="A cozy kitchen",
        refs=["ref1.jpg"],
        params={"type": "still"},
        estimated_cost=6.5,
    )

    req_v2 = GenerationRequest(
        shot_id="shot-001",
        version=2,
        prompt="A cozy kitchen",
        refs=["ref1.jpg"],
        params={"type": "still"},
        estimated_cost=6.5,
    )

    key1 = ledger.idempotency_key(req_v1)
    key2 = ledger.idempotency_key(req_v2)

    assert key1 != key2


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_insufficient_credits_check_raises(tmp_path):
    """Test that insufficient credits raises error."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    await ledger.deduct_credits("ep-001", 9990.0, "test")
    balance = await ledger.get_balance()
    assert balance < 20.0

    with pytest.raises(InsufficientCreditsError) as exc_info:
        await ledger.check_balance(100.0)

    assert exc_info.value.required == 100.0
    assert exc_info.value.available < 20.0


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_sufficient_credits_check_passes(tmp_path):
    """Test that sufficient credits passes check."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    balance = await ledger.get_balance()
    assert balance >= 100.0

    await ledger.check_balance(100.0)

    await ledger.deduct_credits("ep-001", 100.0, "test")
    new_balance = await ledger.get_balance()
    assert new_balance == balance - 100.0


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_heartbeat_simulation():
    """Test that activities can heartbeat (simulation without Temporal)."""
    heartbeat_count = 0

    async def mock_activity_with_heartbeat():
        nonlocal heartbeat_count
        for i in range(5):
            heartbeat_count += 1
            await asyncio.sleep(0.01)
        return "completed"

    result = await mock_activity_with_heartbeat()
    assert result == "completed"
    assert heartbeat_count == 5
