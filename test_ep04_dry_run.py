#!/usr/bin/env python3
"""
End-to-end Ep04 dry run test.

Tests:
- Database initialization
- Beatmap parsing (31 shots)
- Credit plan parsing
- Budget initialization
- Episode creation
"""

import asyncio
import sys
from pathlib import Path

from hfvg.studio_db import init_studio_db, create_episode
from hfvg.episode_parser import parse_beatmap
from hfvg.credit_plan_parser import parse_credit_plan
from hfvg.budget import BudgetLedger


async def main():
    print("=== Ep04 Dry Run Test ===\n")
    
    db_path = "./data/studio.db"
    episode_id = "ep04"
    beatmap_path = "./data/episodes/ep04/BEATMAP.md"
    credit_plan_path = "./data/episodes/ep04/CREDIT-PLAN.md"
    
    # 1. Initialize database
    print("1. Initializing database...")
    await init_studio_db(db_path)
    print("   ✓ Database initialized\n")
    
    # 2. Parse beatmap
    print("2. Parsing beatmap...")
    if not Path(beatmap_path).exists():
        print(f"   ✗ Beatmap not found: {beatmap_path}")
        return 1
    
    shots = parse_beatmap(beatmap_path)
    print(f"   ✓ Parsed {len(shots)} shots")
    
    if len(shots) != 31:
        print(f"   ✗ ERROR: Expected 31 shots, got {len(shots)}")
        return 1
    
    print(f"   ✓ Correct shot count: 31")
    
    # Show some shots
    print(f"   Shots: {', '.join([s['shot_id'] for s in shots[:10]])}...")
    print()
    
    # 3. Parse credit plan
    print("3. Parsing credit plan...")
    if not Path(credit_plan_path).exists():
        print(f"   ✗ Credit plan not found: {credit_plan_path}")
        return 1
    
    credit_plan = parse_credit_plan(credit_plan_path)
    print(f"   ✓ Parsed credit plan")
    print(f"   Lines: {', '.join(credit_plan['lines'].keys())}")
    print(f"   Higgsfield cap: {credit_plan['higgsfield_cap']} credits")
    print(f"   Stop fraction: {credit_plan['stop_fraction']} (80%)")
    print()
    
    # 4. Create episode
    print("4. Creating episode record...")
    await create_episode(db_path, episode_id, beatmap_path)
    print(f"   ✓ Episode {episode_id} created\n")
    
    # 5. Initialize budget
    print("5. Initializing budget...")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget_from_plan(episode_id, credit_plan)
    
    # Check budget lines
    summary = await ledger.get_episode_summary(episode_id)
    print(f"   ✓ Initialized {len(summary['lines'])} budget lines")
    
    for line in summary["lines"][:5]:  # Show first 5
        print(f"     - {line['line_name']}: cap={line['cap']}, stop={line['stop']}")
    
    print()
    
    # 6. Summary
    print("=== Test Summary ===")
    print(f"✓ Database: {db_path}")
    print(f"✓ Episode: {episode_id}")
    print(f"✓ Shots: {len(shots)}")
    print(f"✓ Budget lines: {len(summary['lines'])}")
    print(f"✓ Higgsfield total budget: {credit_plan['higgsfield_cap']} credits")
    print()
    print("=== All checks passed ===")
    
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
