"""Smoke test activities for validating Higgsfield API connectivity and billing.

The smoke test stage performs minimal real API calls to verify:
1. Upload flow: generate-upload-url -> PUT with headers -> GET public_url
2. Generation flow: SOUL V2 ref (~0.09 credits) -> poll by provider ID -> download result

All costs are ledgered against the episode cap. The test must pass before paid stages run.
"""

import hashlib
import httpx
import os
from datetime import datetime

from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger
from hfvg.providers.higgsfield_still import HiggsfieldStillProvider


async def run_smoke_test(
    episode_id: str,
    db_path: str,
) -> dict:
    """
    Run live smoke test: upload probe + generation probe.
    
    Returns:
        {
            "passed": bool,
            "timestamp": str (ISO),
            "upload_probe": {"passed": bool, "public_url": str | None, "error": str | None},
            "generation_probe": {"passed": bool, "provider_job_id": str | None, "cost": float | None, "error": str | None},
        }
    
    Raises:
        RuntimeError: If DRY_RUN is enabled or episode not live-approved
    """
    if os.getenv("DRY_RUN", "").lower() == "true":
        raise RuntimeError("Smoke test requires live mode (DRY_RUN must be false)")
    
    # Check episode is live-approved
    from hfvg.studio_db import get_episode_metadata
    meta = await get_episode_metadata(db_path, episode_id)
    if not meta.get("live_mode"):
        raise RuntimeError(f"Episode {episode_id} not in live mode")
    if not meta.get("g108_approved"):
        raise RuntimeError(f"Episode {episode_id} not G1.08-approved")
    
    result = {
        "passed": False,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "upload_probe": {"passed": False, "public_url": None, "error": None},
        "generation_probe": {"passed": False, "provider_job_id": None, "cost": None, "error": None},
    }
    
    # Probe 1: Upload flow
    try:
        upload_result = await _probe_upload()
        result["upload_probe"] = upload_result
    except Exception as e:
        result["upload_probe"]["error"] = str(e)
    
    # Probe 2: Generation flow (only if upload passed)
    if result["upload_probe"]["passed"]:
        try:
            gen_result = await _probe_generation(episode_id, db_path)
            result["generation_probe"] = gen_result
        except Exception as e:
            result["generation_probe"]["error"] = str(e)
    
    result["passed"] = result["upload_probe"]["passed"] and result["generation_probe"]["passed"]
    return result


async def _probe_upload() -> dict:
    """
    Probe 1: Upload a small test file via Higgsfield API.
    
    Flow:
        POST /files/generate-upload-url {content_type}
        -> PUT upload_url with upload_headers (no auth)
        -> GET public_url and byte-compare
    
    Returns:
        {"passed": bool, "public_url": str | None, "error": str | None}
    """
    base_url = os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    api_key = os.getenv("HIGGSFIELD_API_KEY")
    if not api_key:
        return {"passed": False, "public_url": None, "error": "HIGGSFIELD_API_KEY not set"}
    
    test_content = b"smoke-test-probe-upload"
    content_type = "text/plain"
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Step 1: Generate upload URL
        resp = await client.post(
            f"{base_url}/files/generate-upload-url",
            json={"content_type": content_type},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if resp.status_code != 200:
            return {"passed": False, "public_url": None, "error": f"generate-upload-url failed: {resp.status_code} {resp.text}"}
        
        data = resp.json()
        upload_url = data.get("upload_url")
        public_url = data.get("public_url")
        upload_headers = data.get("upload_headers", {})
        
        if not upload_url or not public_url:
            return {"passed": False, "public_url": None, "error": "Missing upload_url or public_url in response"}
        
        # Step 2: PUT file with exact returned headers (no auth)
        put_resp = await client.put(
            upload_url,
            content=test_content,
            headers=upload_headers,
        )
        if put_resp.status_code not in (200, 204):
            return {"passed": False, "public_url": None, "error": f"PUT failed: {put_resp.status_code} {put_resp.text}"}
        
        # Step 3: GET public_url and byte-compare
        get_resp = await client.get(public_url)
        if get_resp.status_code != 200:
            return {"passed": False, "public_url": public_url, "error": f"GET public_url failed: {get_resp.status_code}"}
        
        if get_resp.content != test_content:
            return {"passed": False, "public_url": public_url, "error": f"Content mismatch: expected {len(test_content)} bytes, got {len(get_resp.content)}"}
        
        return {"passed": True, "public_url": public_url, "error": None}


async def _probe_generation(episode_id: str, db_path: str) -> dict:
    """
    Probe 2: Submit a cheap SOUL V2 ref generation and poll to completion.
    
    Uses real ledgers to reserve/commit credits and USD. Cost ~0.09 credits, ledgered
    against episode cap.
    
    Returns:
        {"passed": bool, "provider_job_id": str | None, "cost": float | None, "error": str | None}
    """
    # Initialize ledgers
    budget_ledger = BudgetLedger(db_path)
    credit_ledger = CreditLedger(db_path)
    
    # Use SOUL V2 model
    model = "higgsfield-ai/soul/v2/standard"
    prompt = "smoke test probe: a simple red circle on white background"
    job_id = f"smoke-{episode_id}-{hashlib.md5(prompt.encode()).hexdigest()[:8]}"
    
    # Estimate cost
    provider = HiggsfieldStillProvider()
    try:
        estimate = await provider.estimate_job(
            prompt=prompt,
            params={"resolution": "1k", "quality": "medium", "aspect_ratio": "1:1"},
            refs=[],
            model=model,
        )
        estimated_credits = float(estimate.get("credits", 0.5))
        estimated_usd_micros = int(float(estimate.get("usd", 0.5)) * 1_000_000)
    except Exception as e:
        return {"passed": False, "provider_job_id": None, "cost": None, "error": f"Estimate failed: {e}"}
    
    # Reserve budget
    try:
        await credit_ledger.reserve_credits(episode_id, job_id, estimated_credits, "smoke test probe")
        await budget_ledger.reserve(episode_id, "L1_refs", estimated_usd_micros, job_id, "smoke test probe")
    except Exception as e:
        return {"passed": False, "provider_job_id": None, "cost": None, "error": f"Budget reserve failed: {e}"}
    
    # Submit generation
    try:
        submit_result = await provider.submit_job(
            prompt=prompt,
            params={"resolution": "1k", "quality": "medium", "aspect_ratio": "1:1"},
            refs=[],
            model=model,
        )
        provider_job_id = submit_result.get("request_id")
        if not provider_job_id:
            raise ValueError("No request_id in submit response")
        
        # Persist provider job mapping
        await credit_ledger.persist_provider_job(job_id, provider_job_id, "higgsfield")
    except Exception as e:
        # Release reservations
        await credit_ledger.release_credits(episode_id, job_id, notes=f"smoke test submit failed: {e}")
        await budget_ledger.release(episode_id, "L1_refs", estimated_usd_micros, f"smoke test submit failed: {e}")
        return {"passed": False, "provider_job_id": None, "cost": None, "error": f"Submit failed: {e}"}
    
    # Poll by provider ID
    try:
        import asyncio
        max_polls = 60
        for _ in range(max_polls):
            status_result = await provider.get_job_status(provider_job_id)
            status = status_result.get("status")
            
            if status == "completed":
                actual_cost_credits = float(status_result.get("cost", estimated_credits))
                actual_cost_usd_micros = int(actual_cost_credits * 1_000_000)
                
                # Commit budget
                await credit_ledger.commit_credits(episode_id, job_id, actual_cost_credits)
                await budget_ledger.commit(episode_id, "L1_refs", estimated_usd_micros, actual_cost_usd_micros, f"smoke test {job_id}")
                
                return {"passed": True, "provider_job_id": provider_job_id, "cost": actual_cost_credits, "error": None}
            
            elif status in ("failed", "blocked", "canceled"):
                error_msg = status_result.get("error", f"Provider status: {status}")
                # Release reservations
                await credit_ledger.release_credits(episode_id, job_id, notes=error_msg)
                await budget_ledger.release(episode_id, "L1_refs", estimated_usd_micros, error_msg)
                return {"passed": False, "provider_job_id": provider_job_id, "cost": None, "error": error_msg}
            
            await asyncio.sleep(5)
        
        # Poll exhausted
        await credit_ledger.release_credits(episode_id, job_id, notes="smoke test poll timeout")
        await budget_ledger.release(episode_id, "L1_refs", estimated_usd_micros, "smoke test poll timeout")
        return {"passed": False, "provider_job_id": provider_job_id, "cost": None, "error": "Poll timeout after 5 minutes"}
        
    except Exception as e:
        # Release reservations
        await credit_ledger.release_credits(episode_id, job_id, notes=f"smoke test poll failed: {e}")
        await budget_ledger.release(episode_id, "L1_refs", estimated_usd_micros, f"smoke test poll failed: {e}")
        return {"passed": False, "provider_job_id": provider_job_id, "cost": None, "error": f"Poll failed: {e}"}
