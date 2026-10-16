"""
Smoke test activity for Higgsfield API health check.

Tests:
1. Upload probe: upload a tiny PNG and verify byte-for-byte download
2. Generation probe: submit a cheap SOUL V2 ref, poll until complete, verify cost
"""

import os
import uuid
import asyncio
from datetime import datetime
from temporalio import activity

from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger


async def _probe_upload(provider: HiggsfieldStillProvider) -> dict:
    """Test file upload and download."""
    import tempfile
    import httpx
    from pathlib import Path
    
    # Create a minimal 1x1 PNG (not text/plain)
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a"  # PNG signature
        "0000000d49484452"  # IHDR chunk
        "00000001000000010802000000907753de"
        "0000000c49444154"  # IDAT chunk
        "789c6260010000000200010e0d"
        "0000000049454e44ae426082"  # IEND
    )
    
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(png_bytes)
        tmp_path = tmp.name
    
    try:
        # Upload using provider's real method
        upload_result = await provider.upload_reference(tmp_path, version=1)
        public_url = upload_result["public_url"]
        
        # Download and verify byte-for-byte match
        async with httpx.AsyncClient() as client:
            resp = await client.get(public_url)
            resp.raise_for_status()
            downloaded = resp.content
        
        if downloaded != png_bytes:
            return {
                "passed": False,
                "public_url": public_url,
                "error": f"Downloaded bytes mismatch: expected {len(png_bytes)}, got {len(downloaded)}"
            }
        
        return {
            "passed": True,
            "public_url": public_url,
            "error": None
        }
    finally:
        Path(tmp_path).unlink(missing_ok=True)


async def _probe_generation(episode_id: str, db_path: str, provider: HiggsfieldStillProvider) -> dict:
    """Test generation with cheap SOUL V2 ref."""
    # Use cheapest model for smoke test: SOUL V2 standard (~0.09 credits)
    model_path = "higgsfield-ai/soul/v2/standard"
    prompt = "smoke test"
    
    # Unique job ID per smoke test run
    run_id = uuid.uuid4().hex[:8]
    job_id = f"smoke-{episode_id}-{run_id}"
    
    try:
        # Get real estimate
        estimated_cost_micros = await provider.estimate_usd_micros(
            prompt=prompt,
            resolution="1k",
            quality="standard",
            aspect_ratio="1:1"
        )
        
        # Reserve from budget ledger (pass episode_id, line_name, amount, reason)
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name="L2_drafts",
            amount_usd_micros=estimated_cost_micros,
            reason=f"Smoke test {run_id}",
            revision=False,
            require_balance=False  # Don't block smoke test on balance
        )
        
        if not reserved:
            return {
                "passed": False,
                "provider_job_id": None,
                "cost": None,
                "error": "Budget reserve failed"
            }
        
        # Reserve credits (using estimate in credits, not USD)
        from hfvg.credit_pricing import estimate_still_credits
        estimated_credits = estimate_still_credits(model_path, "standard")
        
        credit_ledger = CreditLedger(db_path)
        await credit_ledger.init_db()
        
        credit_result = await credit_ledger.reserve_credits(
            episode_id, job_id, estimated_credits,
            notes="smoke test"
        )
        
        if not credit_result.get("reserved"):
            await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, "Smoke test credit reserve failed")
            return {
                "passed": False,
                "provider_job_id": None,
                "cost": None,
                "error": "Credit reserve failed"
            }
        
        # Generate idempotency key
        import hashlib
        key_input = f"{model_path}|{prompt}|smoke-{run_id}"
        key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        idempotency_key = f"{job_id}-{key_hash}"
        
        # Submit using provider's real method
        provider_job_id = await provider.submit_image(
            prompt=prompt,
            resolution="1k",
            quality="standard",
            image_urls=[],
            idempotency_key=idempotency_key,
            aspect_ratio="1:1"
        )
        
        # Persist mapping
        await credit_ledger.persist_provider_job(job_id, provider_job_id, "higgsfield")
        
        # Poll until complete (max 60s)
        for _ in range(60):
            status = await provider.get_job_status(provider_job_id)
            if status["status"] == "completed":
                # Get actual cost
                actual_cost = status.get("cost")
                
                # Commit reservations
                await ledger.commit(episode_id, "L2_drafts", estimated_cost_micros, 
                                   actual_cost_usd_micros=int(actual_cost * 1_000_000) if actual_cost else estimated_cost_micros,
                                   job_id=job_id)
                await credit_ledger.commit_credits(episode_id, job_id, 
                                                  cost_credits=actual_cost if actual_cost else estimated_credits)
                
                return {
                    "passed": True,
                    "provider_job_id": provider_job_id,
                    "cost": actual_cost if actual_cost else estimated_credits,
                    "error": None
                }
            elif status["status"] == "failed":
                # Release on failure
                await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, "Smoke test failed")
                await credit_ledger.release_credits(episode_id, job_id, notes="Smoke test failed")
                return {
                    "passed": False,
                    "provider_job_id": provider_job_id,
                    "cost": None,
                    "error": status.get("error", "Generation failed")
                }
            
            await asyncio.sleep(1)
        
        # Timeout - release
        await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, "Smoke test timeout")
        await credit_ledger.release_credits(episode_id, job_id, notes="Smoke test timeout")
        return {
            "passed": False,
            "provider_job_id": provider_job_id,
            "cost": None,
            "error": "Timeout after 60s"
        }
        
    except Exception as e:
        return {
            "passed": False,
            "provider_job_id": None,
            "cost": None,
            "error": str(e)
        }


@activity.defn
async def run_smoke_test(episode_id: str, db_path: str) -> dict:
    """Run smoke test probes and return results."""
    provider = HiggsfieldStillProvider()
    
    try:
        upload_result = await _probe_upload(provider)
        generation_result = await _probe_generation(episode_id, db_path, provider)
        
        passed = upload_result["passed"] and generation_result["passed"]
        
        result = {
            "passed": passed,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "upload_probe": upload_result,
            "generation_probe": generation_result
        }
        
        return result
    finally:
        await provider.close()
