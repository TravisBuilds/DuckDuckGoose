"""Job and credit ledgers using SQLite."""

import hashlib
import json
import uuid
from datetime import datetime
from typing import Optional

import aiosqlite

from hfvg.config import config
from hfvg.errors import InsufficientCreditsError
from hfvg.models import CreditTransaction, GenerationRequest


class Ledger:
    """Job and credit ledger backed by SQLite."""

    def __init__(self, db_path: str = config.DB_PATH):
        self.db_path = db_path
        if db_path == ":memory:":
            self.db_path = "file::memory:?cache=shared"

    async def init_db(self):
        """Initialize database tables."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    idempotency_key TEXT PRIMARY KEY,
                    provider_job_id TEXT NOT NULL,
                    episode_id TEXT NOT NULL,
                    shot_id TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS credits (
                    tx_id TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    job_id TEXT,
                    amount REAL NOT NULL,
                    tx_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    balance_after REAL NOT NULL
                )
                """
            )

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_balance (
                    account_id TEXT PRIMARY KEY,
                    balance REAL NOT NULL
                )
                """
            )

            await db.execute(
                """
                INSERT OR IGNORE INTO credit_balance (account_id, balance)
                VALUES ('default', ?)
                """,
                (config.INITIAL_CREDIT_BALANCE,),
            )

            await db.commit()

    def idempotency_key(self, req: GenerationRequest) -> str:
        """Generate idempotency key from request."""
        content = f"{req.shot_id}:{req.version}:{req.prompt}:{json.dumps(req.refs)}:{json.dumps(req.params)}"
        return hashlib.sha256(content.encode()).hexdigest()

    async def get_job(self, key: str) -> Optional[dict]:
        """Get existing job by idempotency key."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return dict(row)
        return None

    async def insert_job(
        self,
        key: str,
        provider_job_id: str,
        episode_id: str,
        shot_id: str,
        request: GenerationRequest,
        status: str = "running",
    ):
        """Insert a new job."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute(
                """
                INSERT INTO jobs (idempotency_key, provider_job_id, episode_id, shot_id, request_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    provider_job_id,
                    episode_id,
                    shot_id,
                    request.model_dump_json(),
                    status,
                    datetime.utcnow().isoformat(),
                ),
            )
            await db.commit()

    async def update_job_status(self, key: str, status: str):
        """Update job status by idempotency key."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute(
                "UPDATE jobs SET status = ? WHERE idempotency_key = ?", (status, key)
            )
            await db.commit()
    
    async def update_job_status_by_job_id(self, job_id: str, status: str):
        """Update job status by provider job ID."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            await db.execute(
                "UPDATE jobs SET status = ? WHERE provider_job_id = ?", (status, job_id)
            )
            await db.commit()

    async def get_balance(self, account_id: str = "default") -> float:
        """Get current credit balance."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            async with db.execute(
                "SELECT balance FROM credit_balance WHERE account_id = ?", (account_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return row[0]
        return 0.0

    async def check_balance(self, required: float, account_id: str = "default"):
        """Check if sufficient credits available, raise if not."""
        balance = await self.get_balance(account_id)
        if balance < required:
            raise InsufficientCreditsError(
                f"Insufficient credits: required {required}, available {balance}",
                required=required,
                available=balance,
            )

    async def deduct_credits(
        self,
        episode_id: str,
        amount: float,
        tx_type: str = "estimate",
        job_id: Optional[str] = None,
        account_id: str = "default",
    ) -> CreditTransaction:
        """Deduct credits and record transaction."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            balance = await self.get_balance(account_id)
            new_balance = balance - amount

            tx_id = str(uuid.uuid4())
            timestamp = datetime.utcnow()

            await db.execute(
                """
                INSERT INTO credits (tx_id, episode_id, job_id, amount, tx_type, timestamp, balance_after)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (tx_id, episode_id, job_id, -amount, tx_type, timestamp.isoformat(), new_balance),
            )

            await db.execute(
                "UPDATE credit_balance SET balance = ? WHERE account_id = ?",
                (new_balance, account_id),
            )

            await db.commit()

            return CreditTransaction(
                tx_id=tx_id,
                episode_id=episode_id,
                job_id=job_id,
                amount=-amount,
                tx_type=tx_type,
                timestamp=timestamp,
                balance_after=new_balance,
            )

    async def add_credits(
        self, episode_id: str, amount: float, tx_type: str = "grant", account_id: str = "default"
    ) -> CreditTransaction:
        """Add credits and record transaction."""
        async with aiosqlite.connect(self.db_path, uri=True) as db:
            balance = await self.get_balance(account_id)
            new_balance = balance + amount

            tx_id = str(uuid.uuid4())
            timestamp = datetime.utcnow()

            await db.execute(
                """
                INSERT INTO credits (tx_id, episode_id, job_id, amount, tx_type, timestamp, balance_after)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (tx_id, episode_id, None, amount, tx_type, timestamp.isoformat(), new_balance),
            )

            await db.execute(
                "UPDATE credit_balance SET balance = ? WHERE account_id = ?",
                (new_balance, account_id),
            )

            await db.commit()

            return CreditTransaction(
                tx_id=tx_id,
                episode_id=episode_id,
                job_id=None,
                amount=amount,
                tx_type=tx_type,
                timestamp=timestamp,
                balance_after=new_balance,
            )
