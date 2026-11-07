"""Master dashboard and user identity system tests.

Tests:
- User creation and authentication
- User tagging on episodes/jobs/ledger
- Dashboard aggregations
- Cost tracking (actual vs estimated)
- Monthly run-rate projections
- Spend milestones
"""

import sys
import pytest
import aiosqlite

from hfvg.users import UserManager, LEGACY_USER_ID, ROLE_ADMIN, ROLE_MEMBER
from hfvg.cost_tracking import CostTracker
from hfvg.budget import BudgetLedger
from hfvg.studio_db import init_studio_db, create_episode
from hfvg.migrations import run_all_migrations
from tests.ledger_helpers import set_balance


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "dashboard_test.db")
    await init_studio_db(path)
    
    ledger = BudgetLedger(path)
    await ledger.init_db()
    
    user_mgr = UserManager(path)
    await user_mgr.init_db()
    
    cost_tracker = CostTracker(path)
    await cost_tracker.init_db()
    
    await run_all_migrations(path)
    
    monkeypatch.setenv("DATABASE_PATH", path)
    monkeypatch.setenv("DRY_RUN", "true")
    yield path


@pytest.fixture(autouse=True)
def fresh_api_module():
    sys.modules.pop("api.main", None)
    yield
    sys.modules.pop("api.main", None)


def _client(monkeypatch):
    monkeypatch.setenv("ADMIN_SECRET", "a" * 32)
    from fastapi.testclient import TestClient
    from api.main import app

    client = TestClient(app)
    response = client.post("/api/login", json={"admin_secret": "a" * 32})
    assert response.status_code == 200
    return client, response.cookies


@pytest.mark.asyncio
async def test_user_creation_and_authentication(db_path):
    """Test creating users and authenticating with password or token."""
    user_mgr = UserManager(db_path)
    
    # Create user with password
    user = await user_mgr.create_user(
        name="Alice",
        email="alice@example.com",
        role=ROLE_ADMIN,
        password="secure123"
    )
    
    assert user["user_id"].startswith("usr_")
    assert user["name"] == "Alice"
    assert user["email"] == "alice@example.com"
    assert user["role"] == ROLE_ADMIN
    assert "api_token" in user
    assert user["api_token"].startswith("hfvg_")
    
    # Authenticate with password
    auth = await user_mgr.authenticate_password("alice@example.com", "secure123")
    assert auth is not None
    assert auth["user_id"] == user["user_id"]
    assert "password_hash" not in auth  # Never returned
    
    # Wrong password
    auth = await user_mgr.authenticate_password("alice@example.com", "wrong")
    assert auth is None
    
    # Authenticate with token
    auth = await user_mgr.authenticate_token(user["api_token"])
    assert auth is not None
    assert auth["user_id"] == user["user_id"]
    
    # Duplicate email
    with pytest.raises(ValueError, match="already exists"):
        await user_mgr.create_user("Alice2", "alice@example.com", ROLE_MEMBER)


@pytest.mark.asyncio
async def test_legacy_user_exists(db_path):
    """Test that legacy/shared user is created automatically."""
    user_mgr = UserManager(db_path)
    
    user = await user_mgr.get_user(LEGACY_USER_ID)
    assert user is not None
    assert user["user_id"] == LEGACY_USER_ID
    assert user["name"] == "Legacy/Shared"
    assert user["role"] == ROLE_ADMIN


@pytest.mark.asyncio
async def test_user_monthly_cap(db_path):
    """Test per-user monthly spending cap."""
    user_mgr = UserManager(db_path)
    
    user = await user_mgr.create_user(
        name="Bob",
        email="bob@example.com",
        role=ROLE_MEMBER,
        monthly_cap_usd_micros=50_000_000  # $50 cap
    )
    
    assert user["monthly_cap_usd_micros"] == 50_000_000
    
    # Update cap
    await user_mgr.update_user_cap(user["user_id"], 100_000_000)  # $100 cap
    
    updated = await user_mgr.get_user(user["user_id"])
    assert updated["monthly_cap_usd_micros"] == 100_000_000


@pytest.mark.asyncio
async def test_user_tagging_on_jobs(db_path):
    """Test that jobs can be tagged with user_id."""
    ledger = BudgetLedger(db_path)
    user_mgr = UserManager(db_path)
    
    await create_episode(db_path, "ep01")
    await ledger.set_line("ep01", "L4_video", 10_000_000, 8_000_000)
    
    # Create user
    user = await user_mgr.create_user("Charlie", "charlie@example.com", ROLE_MEMBER)
    user_id = user["user_id"]
    
    # Record job with user_id
    await ledger.record_job(
        episode_id="ep01",
        job_id="job-1",
        shot_id="A01",
        model="kling-3.0-pro",
        tier="final",
        line_name="L4_video",
        usd_micros=280_000
    )
    
    # Tag job with user
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE job_ledger SET user_id = ? WHERE job_id = ?",
            (user_id, "job-1")
        )
        await db.commit()
    
    # Verify tagging
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT user_id FROM job_ledger WHERE job_id = ?", ("job-1",)
        ) as cursor:
            row = await cursor.fetchone()
            assert row[0] == user_id


@pytest.mark.asyncio
async def test_cost_tracking_actual_vs_estimated(db_path):
    """Test recording actual costs and calculating variance."""
    ledger = BudgetLedger(db_path)
    cost_tracker = CostTracker(db_path)
    
    await create_episode(db_path, "ep02")
    await ledger.set_line("ep02", "L2_drafts", 10_000_000, 8_000_000)
    
    # Record job with estimate
    await ledger.record_job(
        episode_id="ep02",
        job_id="job-2",
        shot_id="A01",
        model="seedance",
        tier="draft",
        line_name="L2_drafts",
        usd_micros=60_000  # Estimate: $0.06
    )
    
    # Record actual cost (slightly higher)
    await cost_tracker.record_actual_cost(
        job_id="job-2",
        actual_usd_micros=65_000,  # Actual: $0.065
        provider_credits=1.37,
        cost_source="provider_reported"
    )
    
    # Get metrics
    metrics = await cost_tracker.get_cost_metrics("job-2")
    assert metrics is not None
    assert metrics.estimated_usd_micros == 60_000
    assert metrics.actual_usd_micros == 65_000
    assert metrics.variance_usd_micros == 5_000
    assert metrics.variance_percent == 8.3  # ~8.3% over estimate


@pytest.mark.asyncio
async def test_episode_cost_summary(db_path):
    """Test episode-level cost summary with actual vs estimated."""
    ledger = BudgetLedger(db_path)
    cost_tracker = CostTracker(db_path)
    
    await create_episode(db_path, "ep03")
    await ledger.set_line("ep03", "L2_drafts", 10_000_000, 8_000_000)
    
    # Record multiple jobs
    for i in range(3):
        await ledger.record_job(
            episode_id="ep03",
            job_id=f"job-{i}",
            shot_id=f"A0{i}",
            model="seedance",
            tier="draft",
            line_name="L2_drafts",
            usd_micros=50_000
        )
        # Record actual (10% over estimate)
        await cost_tracker.record_actual_cost(
            job_id=f"job-{i}",
            actual_usd_micros=55_000,
            cost_source="provider_reported"
        )
    
    # Get summary
    summary = await cost_tracker.get_episode_cost_summary("ep03")
    assert summary["job_count"] == 3
    assert summary["estimated_usd_micros"] == 150_000
    assert summary["actual_usd_micros"] == 165_000
    assert summary["variance_usd_micros"] == 15_000
    assert summary["variance_percent"] == 10.0
    assert summary["jobs_with_actual_cost"] == 3
    assert summary["cost_tracking_coverage_percent"] == 100.0


@pytest.mark.asyncio
async def test_monthly_runrate_projection(db_path):
    """Test monthly spend run-rate projection (informational only)."""
    ledger = BudgetLedger(db_path)
    cost_tracker = CostTracker(db_path)
    user_mgr = UserManager(db_path)
    
    user = await user_mgr.create_user("Dave", "dave@example.com", ROLE_MEMBER)
    user_id = user["user_id"]
    
    await create_episode(db_path, "ep04")
    await ledger.set_line("ep04", "L2_drafts", 10_000_000, 8_000_000)
    
    # Simulate some spend
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Add jobs
    for i in range(5):
        await ledger.record_job(
            episode_id="ep04",
            job_id=f"job-{i}",
            shot_id=f"A0{i}",
            model="seedance",
            tier="draft",
            line_name="L2_drafts",
            usd_micros=100_000
        )
        
        # Tag with user and set actual cost
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE job_ledger SET user_id = ?, created_at = ?, actual_usd_micros = ? WHERE job_id = ?",
                (user_id, month_start.isoformat(), 100_000, f"job-{i}")
            )
            await db.commit()
    
    # Get runrate
    runrate = await cost_tracker.get_monthly_runrate(user_id=user_id)
    
    assert "current_spend_usd_micros" in runrate
    assert "daily_rate_usd_micros" in runrate
    assert "projected_month_end_usd_micros" in runrate
    assert runrate["current_spend_usd_micros"] == 500_000


@pytest.mark.asyncio
async def test_spend_milestones(db_path):
    """Test passive spend milestone tracking (non-blocking)."""
    cost_tracker = CostTracker(db_path)
    
    # Check $100 milestone
    await cost_tracker.check_spend_milestones(
        current_spend_usd_micros=100_000_000  # $100
    )
    
    milestones = await cost_tracker.get_recent_milestones()
    assert len(milestones) == 1
    assert milestones[0]["threshold_usd_micros"] == 100_000_000
    assert "100.00" in milestones[0]["message"]
    
    # Should not duplicate
    await cost_tracker.check_spend_milestones(
        current_spend_usd_micros=100_000_001
    )
    milestones = await cost_tracker.get_recent_milestones()
    assert len(milestones) == 1  # Still only one


@pytest.mark.asyncio
async def test_dashboard_api_endpoint(db_path, monkeypatch):
    """Test comprehensive dashboard API endpoint."""
    client, cookies = _client(monkeypatch)
    
    # Set up test data
    user_mgr = UserManager(db_path)
    ledger = BudgetLedger(db_path)
    cost_tracker = CostTracker(db_path)
    
    # Create users
    user1 = await user_mgr.create_user("User1", "user1@example.com", ROLE_MEMBER)
    user2 = await user_mgr.create_user("User2", "user2@example.com", ROLE_MEMBER)
    
    # Create episodes and jobs
    await create_episode(db_path, "ep10")
    await ledger.set_line("ep10", "L2_drafts", 10_000_000, 8_000_000)
    
    for i in range(3):
        await ledger.record_job(
            episode_id="ep10",
            job_id=f"job-u1-{i}",
            shot_id=f"A{i:02d}",
            model="seedance",
            tier="draft",
            line_name="L2_drafts",
            usd_micros=50_000
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE job_ledger SET user_id = ? WHERE job_id = ?",
                (user1["user_id"], f"job-u1-{i}")
            )
            await db.commit()
    
    for i in range(2):
        await ledger.record_job(
            episode_id="ep10",
            job_id=f"job-u2-{i}",
            shot_id=f"B{i:02d}",
            model="seedance",
            tier="draft",
            line_name="L2_drafts",
            usd_micros=50_000
        )
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE job_ledger SET user_id = ? WHERE job_id = ?",
                (user2["user_id"], f"job-u2-{i}")
            )
            await db.commit()
    
    # Call dashboard endpoint
    response = client.get("/api/dashboard", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    
    # Verify structure
    assert "user_stats" in data
    assert "spend_summary" in data
    assert "users" in data
    assert "episodes" in data
    assert "stages" in data
    assert "providers" in data
    assert "daily_series" in data
    assert "monthly_runrate" in data
    
    # Verify user stats
    assert data["user_stats"]["total_users"] >= 2  # At least our 2 test users
    
    # Verify spend summary
    assert data["spend_summary"]["total_estimated_usd_micros"] == 250_000  # 5 jobs * 50k
    
    # Verify user breakdown
    users_data = {u["user_id"]: u for u in data["users"]}
    assert user1["user_id"] in users_data
    assert user2["user_id"] in users_data
    assert users_data[user1["user_id"]]["job_count"] == 3
    assert users_data[user2["user_id"]]["job_count"] == 2


@pytest.mark.asyncio
async def test_user_list_endpoint(db_path, monkeypatch):
    """Test user listing API endpoint."""
    client, cookies = _client(monkeypatch)
    
    user_mgr = UserManager(db_path)
    await user_mgr.create_user("Eve", "eve@example.com", ROLE_MEMBER)
    
    response = client.get("/api/users", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert "users" in data
    assert len(data["users"]) >= 1
    
    # Find Eve
    eve = next((u for u in data["users"] if u["email"] == "eve@example.com"), None)
    assert eve is not None
    assert eve["name"] == "Eve"
    assert eve["role"] == ROLE_MEMBER


@pytest.mark.asyncio
async def test_create_user_endpoint(db_path, monkeypatch):
    """Test user creation API endpoint."""
    client, cookies = _client(monkeypatch)
    
    response = client.post(
        "/api/users",
        json={
            "name": "Frank",
            "email": "frank@example.com",
            "role": "admin",
            "password": "password123"
        },
        cookies=cookies
    )
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert data["success"] is True
    assert "user" in data
    assert data["user"]["name"] == "Frank"
    assert data["user"]["role"] == ROLE_ADMIN
    assert "api_token" in data["user"]


@pytest.mark.asyncio
async def test_user_details_endpoint(db_path, monkeypatch):
    """Test user details API endpoint with spend summary."""
    client, cookies = _client(monkeypatch)
    
    user_mgr = UserManager(db_path)
    ledger = BudgetLedger(db_path)
    
    user = await user_mgr.create_user("Grace", "grace@example.com", ROLE_MEMBER)
    user_id = user["user_id"]
    
    # Add some jobs for this user
    await create_episode(db_path, "ep20")
    await ledger.set_line("ep20", "L2_drafts", 10_000_000, 8_000_000)
    
    await ledger.record_job(
        episode_id="ep20",
        job_id="job-grace-1",
        shot_id="A01",
        model="seedance",
        tier="draft",
        line_name="L2_drafts",
        usd_micros=75_000
    )
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE job_ledger SET user_id = ?, actual_usd_micros = ? WHERE job_id = ?",
            (user_id, 75_000, "job-grace-1")
        )
        await db.commit()
    
    response = client.get(f"/api/users/{user_id}", cookies=cookies)
    assert response.status_code == 200, response.text
    
    data = response.json()
    assert data["user_id"] == user_id
    assert data["name"] == "Grace"
    assert "episodes" in data
    assert len(data["episodes"]) == 1
    assert data["episodes"][0]["episode_id"] == "ep20"
    assert "monthly_runrate" in data
