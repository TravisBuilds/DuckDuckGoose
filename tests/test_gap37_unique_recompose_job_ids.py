"""Test GAP-37: unique recompose job IDs and committed job ID refusal."""
import pytest
import tempfile
import aiosqlite
from temporalio.exceptions import ApplicationError
from hfvg.activities.studio_generation import refuse_committed_job_id


@pytest.mark.asyncio
async def test_refuse_committed_job_id_accepts_new_id():
    """refuse_committed_job_id allows a new job ID (no row in job_ledger)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    # Initialize a job_ledger table
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE job_ledger (job_id TEXT PRIMARY KEY, status TEXT, committed_at TEXT)"
        )
        await db.commit()
    
    # Should not raise for a new job ID
    await refuse_committed_job_id(db_path, "still-ep99-A01-v1")


@pytest.mark.asyncio
async def test_refuse_committed_job_id_accepts_reserved():
    """refuse_committed_job_id allows a reserved (in-flight) job ID (activity retry case)."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE job_ledger (job_id TEXT PRIMARY KEY, status TEXT, committed_at TEXT)"
        )
        await db.execute(
            "INSERT INTO job_ledger (job_id, status) VALUES (?, ?)",
            ("still-ep99-A02-v1", "reserved")
        )
        await db.commit()
    
    # Should not raise for a reserved job (activity retry)
    await refuse_committed_job_id(db_path, "still-ep99-A02-v1")


@pytest.mark.asyncio
async def test_refuse_committed_job_id_refuses_committed():
    """refuse_committed_job_id raises ApplicationError for a committed job ID."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE job_ledger (job_id TEXT PRIMARY KEY, status TEXT, committed_at TEXT)"
        )
        await db.execute(
            "INSERT INTO job_ledger (job_id, status, committed_at) VALUES (?, ?, ?)",
            ("still-ep99-A03-v1", "committed", "2026-10-27T12:00:00Z")
        )
        await db.commit()
    
    # Should raise ApplicationError with type JobIdAlreadyCommitted
    with pytest.raises(ApplicationError) as exc_info:
        await refuse_committed_job_id(db_path, "still-ep99-A03-v1")
    
    assert exc_info.value.type == "JobIdAlreadyCommitted"
    assert "already committed" in str(exc_info.value)


@pytest.mark.asyncio
async def test_refuse_committed_job_id_no_table():
    """refuse_committed_job_id silently allows when job_ledger table doesn't exist yet."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name
    
    # Empty DB with no job_ledger table
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE dummy (id INTEGER)")
        await db.commit()
    
    # Should not raise (no job_ledger table = nothing committed yet)
    await refuse_committed_job_id(db_path, "still-ep99-A04-v1")
