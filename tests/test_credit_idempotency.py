"""Test credit reservation idempotency."""

import pytest


@pytest.mark.asyncio
async def test_reserve_credits_idempotent_same_job_id(tmp_path):
    """Test: Reserving credits with the same job_id is idempotent."""
    from hfvg.credit_ledger import CreditLedger
    
    db_path = str(tmp_path / "test.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    # Create episode line
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # First reservation
    result1 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v1", 5.0, "First reserve"
    )
    assert result1["reserved"] is True
    assert result1.get("idempotent") is None  # First call is not idempotent return
    
    # Check that credits were reserved
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT reserved_credits, spent_credits FROM credit_lines WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 5.0  # reserved
        assert row[1] == 0    # spent
    
    # Second reservation with same job_id (idempotent)
    result2 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v1", 5.0, "Second reserve (should be idempotent)"
    )
    assert result2["reserved"] is True
    assert result2["idempotent"] is True
    assert result2["existing_amount"] == 5.0
    
    # Check that credits were NOT double-reserved
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT reserved_credits FROM credit_lines WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 5.0  # Still 5.0, not 10.0
        
        # Check that only one reserve transaction exists
        cursor = await db.execute(
            "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'reserve'",
            ("ref-ep99-r_duck-v1",)
        )
        count = await cursor.fetchone()
        assert count[0] == 1  # Only one reserve transaction


@pytest.mark.asyncio
async def test_reserve_credits_idempotent_with_float_amount(tmp_path):
    """Test: Idempotency works with float amounts like 0.09."""
    from hfvg.credit_ledger import CreditLedger
    
    db_path = str(tmp_path / "test.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    await ledger.create_episode_line("ep99", cap_credits=10)
    
    # Reserve with float amount
    result1 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v1", 0.09, "SOUL V2 ref"
    )
    assert result1["reserved"] is True
    
    # Retry with same job_id
    result2 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v1", 0.09, "Retry"
    )
    assert result2["reserved"] is True
    assert result2["idempotent"] is True
    assert result2["existing_amount"] == 0.09
    
    # Check total reserved is still 0.09, not 0.18
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT reserved_credits FROM credit_lines WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 0.09


@pytest.mark.asyncio
async def test_reserve_credits_different_job_ids_not_idempotent(tmp_path):
    """Test: Different job_ids create separate reservations."""
    from hfvg.credit_ledger import CreditLedger
    
    db_path = str(tmp_path / "test.db")
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    await ledger.create_episode_line("ep99", cap_credits=100)
    
    # Reserve for first job
    result1 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v1", 5.0, "Duck v1"
    )
    assert result1["reserved"] is True
    
    # Reserve for second job (different job_id)
    result2 = await ledger.reserve_credits(
        "ep99", "ref-ep99-r_duck-v2", 5.0, "Duck v2"
    )
    assert result2["reserved"] is True
    assert result2.get("idempotent") is None  # Not idempotent, it's a new job
    
    # Check both reservations exist
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT reserved_credits FROM credit_lines WHERE episode_id = ?",
            ("ep99",)
        )
        row = await cursor.fetchone()
        assert row[0] == 10.0  # Both reservations: 5 + 5
