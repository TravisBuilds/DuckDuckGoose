"""Smoke tests: basic imports and functionality."""

import pytest

from hfvg.config import config
from hfvg.errors import ContentBlockError, InsufficientCreditsError, RetryableError
from hfvg.ledger import Ledger
from hfvg.models import (
    EpisodeState,
    GenerationRequest,
    PipelineStage,
)


def test_imports():
    """Test that all modules import successfully."""
    from hfvg import activities, workflows

    assert activities is not None
    assert workflows is not None


def test_config():
    """Test configuration."""
    assert config.DRY_RUN is True
    assert config.INITIAL_CREDIT_BALANCE > 0


def test_error_classes():
    """Test error classes."""
    err = RetryableError("test")
    assert "test" in str(err)

    err = ContentBlockError("blocked", "provider")
    assert err.provider == "provider"

    err = InsufficientCreditsError("low", 100.0, 10.0)
    assert err.required == 100.0
    assert err.available == 10.0


@pytest.mark.asyncio
async def test_ledger_init(tmp_path):
    """Test ledger initialization."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    balance = await ledger.get_balance()
    assert balance == config.INITIAL_CREDIT_BALANCE


@pytest.mark.asyncio
async def test_ledger_idempotency_key():
    """Test idempotency key generation."""
    ledger = Ledger(":memory:")

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

    req3 = GenerationRequest(
        shot_id="shot-001",
        version=2,
        prompt="A cozy kitchen",
        refs=["ref1.jpg"],
        params={"type": "still"},
        estimated_cost=6.5,
    )

    key1 = ledger.idempotency_key(req1)
    key2 = ledger.idempotency_key(req2)
    key3 = ledger.idempotency_key(req3)

    assert key1 == key2
    assert key1 != key3


@pytest.mark.asyncio
async def test_credit_operations(tmp_path):
    """Test credit ledger operations."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    initial = await ledger.get_balance()
    assert initial == config.INITIAL_CREDIT_BALANCE

    await ledger.deduct_credits("ep-001", 100.0, "estimate")
    balance = await ledger.get_balance()
    assert balance == initial - 100.0

    await ledger.add_credits("ep-001", 50.0, "grant")
    balance = await ledger.get_balance()
    assert balance == initial - 50.0


@pytest.mark.asyncio
async def test_insufficient_credits_check(tmp_path):
    """Test that check_balance raises when insufficient."""
    db_path = str(tmp_path / "test.db")
    ledger = Ledger(db_path)
    await ledger.init_db()

    await ledger.deduct_credits("ep-001", config.INITIAL_CREDIT_BALANCE - 10.0, "test")

    with pytest.raises(InsufficientCreditsError) as exc_info:
        await ledger.check_balance(100.0)

    assert exc_info.value.required == 100.0


def test_episode_state():
    """Test episode state model."""
    state = EpisodeState(
        episode_id="ep-001",
        stage=PipelineStage.INFO_GATHERING,
        idea="Test idea",
    )

    assert state.episode_id == "ep-001"
    assert state.stage == PipelineStage.INFO_GATHERING
    assert not state.readback_approved
