#!/usr/bin/env python3
"""API-based E2E dry run with screenshot artifacts"""

import asyncio
import sys
import json
from pathlib import Path
import httpx
from datetime import datetime

WORKSPACE = Path(__file__).parent.parent
SCREENSHOTS_DIR = WORKSPACE / "screenshots" / "e2e"
ADMIN_SECRET_FILE = Path("/tmp/admin_secret.txt")
API_URL = "http://localhost:8000"
BEATMAP_FILE = WORKSPACE / "data" / "episodes" / "ep04" / "BEATMAP.md"

def create_text_screenshot(filename: str, title: str, content: dict):
    """Create a text-based screenshot artifact"""
    screenshot_path = SCREENSHOTS_DIR / f"{filename}.txt"
    with open(screenshot_path, 'w') as f:
        f.write(f"=" * 80 + "\n")
        f.write(f" {title}\n")
        f.write(f" Generated: {datetime.now().isoformat()}\n")
        f.write(f"=" * 80 + "\n\n")
        f.write(json.dumps(content, indent=2))
        f.write("\n")
    print(f"✓ Screenshot: {filename}.txt")
    return screenshot_path

async def main():
    print("=== API-Based E2E Dry Run ===\n")
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    admin_secret = ADMIN_SECRET_FILE.read_text().strip()
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        cookies = {"studio_admin_token": admin_secret}
        
        try:
            # 1. Health check
            print("1. Health check...")
            r = await client.get(f"{API_URL}/api/health")
            create_text_screenshot("01_health", "API Health Check", r.json())
            
            # 2. Upload episode
            print("2. Upload Ep04...")
            files = {"beatmap_file": ("BEATMAP.md", BEATMAP_FILE.read_bytes())}
            data = {"episode_id": "ep04"}
            r = await client.post(f"{API_URL}/api/episodes/upload", files=files, data=data, cookies=cookies)
            result = r.json()
            create_text_screenshot("02_upload", f"Episode Upload - {result['shots_count']} shots", result)
            
            # 3. Get shots
            print("3. Get shots...")
            r = await client.get(f"{API_URL}/api/episodes/ep04/shots", cookies=cookies)
            shots = r.json()
            create_text_screenshot("03_shots", f"Episode Shots ({len(shots['shots'])} total)", shots)
            
            # 4. Canary refused (no G1.08)
            print("4. Canary (should be refused)...")
            r = await client.post(f"{API_URL}/api/episodes/ep04/canary", cookies=cookies)
            result = r.json()
            create_text_screenshot("04_canary_refused", "Canary Refused - No G1.08", result)
            print(f"   Result: {result.get('message', result.get('success'))}")
            
            # 5. Approve G1.08
            print("5. Approve G1.08...")
            r = await client.post(f"{API_URL}/api/episodes/ep04/approve-g108", cookies=cookies)
            result = r.json()
            create_text_screenshot("05_g108_approved", "G1.08 Approved", result)
            
            # 6. Canary success
            print("6. Canary (should succeed in dry run)...")
            r = await client.post(f"{API_URL}/api/episodes/ep04/canary", cookies=cookies)
            result = r.json()
            create_text_screenshot("06_canary_success", "Canary Success - Dry Run", result)
            if result.get("success"):
                canary = result.get("canary_result", {})
                print(f"   ✓ Status: {canary.get('status')}")
                print(f"   ✓ Still: {canary.get('still_url')}")
                print(f"   ✓ Clip: {canary.get('clip_url')}")
            
            # 7. Budget
            print("7. Budget status...")
            r = await client.get(f"{API_URL}/api/episodes/ep04/budget", cookies=cookies)
            budget = r.json()
            create_text_screenshot("07_budget", "Budget Status", budget)
            print(f"   ✓ {len(budget.get('lines', []))} budget lines")
            
            # 8. Audit trail
            print("8. Audit trail...")
            r = await client.get(f"{API_URL}/api/episodes/ep04/audit", cookies=cookies)
            audit = r.json()
            create_text_screenshot("08_audit", f"Audit Trail ({len(audit.get('audit_log', []))} entries)", audit)
            
            print(f"\n✓ Complete! Artifacts: {SCREENSHOTS_DIR}")
            print(f"  Total artifacts: 8")
            return 0
            
        except Exception as e:
            print(f"\n✗ Error: {e}")
            import traceback
            traceback.print_exc()
            create_text_screenshot("error", "Error", {"error": str(e), "traceback": traceback.format_exc()})
            return 1

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
