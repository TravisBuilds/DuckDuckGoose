#!/usr/bin/env python3
"""Seed database with multi-user test data for dashboard screenshots."""

import asyncio
import os
import sys
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hfvg.users import UserManager
from hfvg.budget import BudgetLedger
from hfvg.cost_tracking import CostTracker
from hfvg.studio_db import init_studio_db, create_episode
from hfvg.migrations import run_all_migrations
import aiosqlite


async def seed_data(db_path: str):
    """Seed database with test data."""
    print(f"Seeding database at {db_path}...")
    
    # Initialize
    await init_studio_db(db_path)
    
    user_mgr = UserManager(db_path)
    await user_mgr.init_db()
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    cost_tracker = CostTracker(db_path)
    await cost_tracker.init_db()
    
    await run_all_migrations(db_path)
    
    # Create users
    print("Creating users...")
    alice = await user_mgr.create_user(
        name="Alice Chen",
        email="alice@example.com",
        role="admin",
        password="password123",
        monthly_cap_usd_micros=100_000_000  # $100
    )
    
    bob = await user_mgr.create_user(
        name="Bob Martinez",
        email="bob@example.com",
        role="member",
        password="password123",
        monthly_cap_usd_micros=50_000_000  # $50
    )
    
    charlie = await user_mgr.create_user(
        name="Charlie Davis",
        email="charlie@example.com",
        role="member",
        password="password123"
    )
    
    # Mark users as active
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE users SET last_active = ? WHERE user_id IN (?, ?, ?)",
            (now.isoformat(), alice["user_id"], bob["user_id"], charlie["user_id"])
        )
        await db.commit()
    
    # Create episodes and jobs
    print("Creating episodes and jobs...")
    
    episodes = [
        ("ep01", alice["user_id"], 15),  # 15 jobs
        ("ep02", bob["user_id"], 12),
        ("ep03", alice["user_id"], 8),
        ("ep04", charlie["user_id"], 10),
        ("ep05", bob["user_id"], 6),
    ]
    
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    for ep_id, user_id, job_count in episodes:
        await create_episode(db_path, ep_id)
        await ledger.set_line(ep_id, "L2_drafts", 10_000_000, 8_000_000)
        await ledger.set_line(ep_id, "L3_final_stills", 15_000_000, 12_000_000)
        await ledger.set_line(ep_id, "L4_video", 20_000_000, 16_000_000)
        await ledger.set_line(ep_id, "L7_revision", 10_000_000, 10_000_000)
        
        # Create jobs spread across the month
        for i in range(job_count):
            # Mix of stages
            if i < job_count // 3:
                line = "L2_drafts"
                model = "seedance"
                tier = "draft"
                cost = 50_000 + (i * 5_000)
            elif i < 2 * job_count // 3:
                line = "L3_final_stills"
                model = "seedance"
                tier = "final"
                cost = 75_000 + (i * 3_000)
            else:
                line = "L4_video"
                model = "kling-3.0-pro"
                tier = "final"
                cost = 280_000 + (i * 10_000)
            
            # Some revisions
            if i % 5 == 0 and i > 0:
                tier = "revision"
                line = "L7_revision"
                cost = int(cost * 0.7)  # Revisions typically cheaper
            
            job_id = f"job-{ep_id}-{i:02d}"
            
            # Record job
            await ledger.record_job(
                episode_id=ep_id,
                job_id=job_id,
                shot_id=f"A{i:02d}",
                model=model,
                tier=tier,
                line_name=line,
                usd_micros=cost
            )
            
            # Tag with user and set timestamp (spread across month)
            days_ago = (job_count - i) * 2  # Spread over ~1 month
            created_at = (now - timedelta(days=days_ago)).isoformat()
            
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "UPDATE job_ledger SET user_id = ?, created_at = ? WHERE job_id = ?",
                    (user_id, created_at, job_id)
                )
                await db.commit()
            
            # Record actual costs (some variance from estimate)
            actual_cost = int(cost * (0.95 + (i % 10) * 0.01))  # 95-105% of estimate
            await cost_tracker.record_actual_cost(
                job_id=job_id,
                actual_usd_micros=actual_cost,
                provider_credits=actual_cost / 47_500,  # Rough credits
                cost_source="provider_reported"
            )
            
            # Some failed jobs
            if i % 8 == 0:
                async with aiosqlite.connect(db_path) as db:
                    await db.execute(
                        "UPDATE job_ledger SET verdict = ?, used = 0 WHERE job_id = ?",
                        ("rejected", job_id)
                    )
                    await db.commit()
    
    # Set manual balance
    print("Setting manual balance...")
    await ledger.set_manual_balance(
        balance_usd_micros=45_000_000,  # $45 remaining
        set_by="admin",
        note="Test seeded balance"
    )
    
    # Check milestones
    print("Checking milestones...")
    total_spend = 0
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT SUM(COALESCE(actual_usd_micros, usd_micros)) FROM job_ledger"
        ) as cursor:
            total_spend = (await cursor.fetchone())[0] or 0
    
    await cost_tracker.check_spend_milestones(total_spend)
    
    print(f"\nDatabase seeded successfully!")
    print(f"Total spend: ${total_spend / 1_000_000:.2f}")
    print(f"Users: {len(episodes)} with various activity levels")
    print(f"Episodes: {len(episodes)}")
    print(f"Jobs: {sum(count for _, _, count in episodes)}")


if __name__ == "__main__":
    db_path = os.getenv("DATABASE_PATH", "./data/dashboard_demo.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    
    asyncio.run(seed_data(db_path))
    print(f"\nTo use this database, set: export DATABASE_PATH={db_path}")
