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
    from PIL import Image
    
    # Generate a minimal 1x1 PNG with PIL
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    
    try:
        # Create and save 1x1 red PNG
        img = Image.new('RGB', (1, 1), color='red')
        img.save(tmp_path, 'PNG')
        
        # Read back the bytes for verification
        with open(tmp_path, 'rb') as f:
            png_bytes = f.read()
        
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
        
        # Verify downloaded bytes decode as PNG
        try:
            from io import BytesIO
            img_check = Image.open(BytesIO(downloaded))
            img_check.verify()
        except Exception as decode_err:
            return {
                "passed": False,
                "public_url": public_url,
                "error": f"Downloaded bytes don't decode as PNG: {decode_err}"
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
    prompt = "smoke test"
    
    # Unique job ID per smoke test run
    run_id = uuid.uuid4().hex[:8]
    job_id = f"smoke-{episode_id}-{run_id}"
    
    budget_reserved = False
    credits_reserved = False
    
    try:
        # Get real estimate (SOUL V2 API: prompt, resolution, aspect_ratio - no quality field)
        estimated_cost_micros = await provider.estimate_usd_micros(
            prompt=prompt,
            resolution="720p",
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
        
        budget_reserved = True
        
        # Reserve credits (using estimate in credits, not USD)
        from hfvg.credit_pricing import estimate_still_credits
        estimated_credits = estimate_still_credits(provider.model_path, "standard")
        
        credit_ledger = CreditLedger(db_path)
        await credit_ledger.init_db()
        
        credit_result = await credit_ledger.reserve_credits(
            episode_id, job_id, estimated_credits,
            notes="smoke test"
        )
        
        if not credit_result.get("reserved"):
            await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, "Smoke test credit reserve failed")
            budget_reserved = False
            return {
                "passed": False,
                "provider_job_id": None,
                "cost": None,
                "error": "Credit reserve failed"
            }
        
        credits_reserved = True
        
        # Generate idempotency key
        import hashlib
        key_input = f"{provider.model_path}|{prompt}|smoke-{run_id}"
        key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        idempotency_key = f"{job_id}-{key_hash}"
        
        # Submit using provider's real method (SOUL V2 schema: prompt, resolution, aspect_ratio)
        provider_job_id = await provider.submit_image(
            prompt=prompt,
            resolution="720p",
            image_urls=[],
            idempotency_key=idempotency_key,
            aspect_ratio="1:1"
        )
        
        # Persist mapping
        await credit_ledger.persist_provider_job(job_id, provider_job_id, "higgsfield")
        
        # Poll until complete (max 60s)
        for _ in range(60):
            status = await provider.get_job_status(provider_job_id)
            
            # Terminal statuses
            if status.status == "completed":
                # Get actual cost (status.cost is in USD float, convert to micros)
                if status.cost:
                    actual_cost_micros = int(status.cost * 1_000_000)
                else:
                    actual_cost_micros = estimated_cost_micros
                
                # Download output and verify it decodes
                import httpx
                from PIL import Image
                from io import BytesIO
                
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(status.output_url)
                        resp.raise_for_status()
                        img_bytes = resp.content
                        
                        # Verify it decodes as image
                        img = Image.open(BytesIO(img_bytes))
                        img.verify()
                except Exception as download_err:
                    # Release on download failure
                    await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, f"Smoke test download failed: {download_err}")
                    await credit_ledger.release_credits(episode_id, job_id, notes=f"Download failed: {download_err}")
                    return {
                        "passed": False,
                        "provider_job_id": provider_job_id,
                        "cost": None,
                        "error": f"Output download/decode failed: {download_err}"
                    }
                
                # Commit reservations
                await ledger.commit(episode_id, "L2_drafts", estimated_cost_micros, 
                                   actual_usd_micros=actual_cost_micros,
                                   job_id=job_id)
                await credit_ledger.commit_credits(episode_id, job_id, 
                                                  actual_credits=status.cost if status.cost else estimated_credits)
                
                budget_reserved = False
                credits_reserved = False
                
                return {
                    "passed": True,
                    "provider_job_id": provider_job_id,
                    "cost": status.cost if status.cost else estimated_credits,
                    "error": None
                }
            elif status.status in ("failed", "nsfw", "canceled"):
                # Release on failure/nsfw/canceled
                await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, f"Smoke test {status.status}")
                await credit_ledger.release_credits(episode_id, job_id, notes=f"Smoke test {status.status}")
                budget_reserved = False
                credits_reserved = False
                return {
                    "passed": False,
                    "provider_job_id": provider_job_id,
                    "cost": None,
                    "error": status.error or f"Generation {status.status}"
                }
            
            await asyncio.sleep(1)
        
        # Timeout - release
        await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, "Smoke test timeout")
        await credit_ledger.release_credits(episode_id, job_id, notes="Smoke test timeout")
        budget_reserved = False
        credits_reserved = False
        return {
            "passed": False,
            "provider_job_id": provider_job_id,
            "cost": None,
            "error": "Timeout after 60s"
        }
        
    except Exception as e:
        # Release budget and credits if still held
        if budget_reserved:
            try:
                await ledger.release(episode_id, "L2_drafts", estimated_cost_micros, f"Smoke test exception: {e}")
            except Exception:
                pass
        if credits_reserved:
            try:
                await credit_ledger.release_credits(episode_id, job_id, notes=f"Smoke test exception: {e}")
            except Exception:
                pass
        return {
            "passed": False,
            "provider_job_id": None,
            "cost": None,
            "error": str(e)
        }


@activity.defn
async def run_smoke_test(episode_id: str, db_path: str) -> dict:
    """Run smoke test probes and return results."""
    # Use cheapest model: SOUL V2 standard (~0.09 credits)
    model_path = "higgsfield-ai/soul/v2/standard"
    provider = HiggsfieldStillProvider(model_path=model_path)
    
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
