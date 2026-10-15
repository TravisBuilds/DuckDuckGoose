"""Activities for reference image generation (character sheets, lineup shots, etc.)."""

import os
import json
from pathlib import Path
from temporalio import activity

from hfvg.config import config
from hfvg.credit_ledger import CreditLedger
from hfvg.providers.higgsfield import HiggsfieldProvider


async def _check_ref_qc(image_path: str) -> dict:
    """
    Check reference image quality.
    
    Basic checks:
    - File exists and is readable
    - File size > 1KB (not empty/stub)
    - Valid image format (can be opened)
    
    Returns:
        dict with 'passed' (bool) and optional 'reason' (str)
    """
    path = Path(image_path)
    
    if not path.exists():
        return {"passed": False, "reason": "file does not exist"}
    
    file_size = path.stat().st_size
    if file_size < 1024:
        return {"passed": False, "reason": f"file too small ({file_size} bytes)"}
    
    # Try to open as image
    try:
        from PIL import Image
        with Image.open(path) as img:
            width, height = img.size
            if width < 256 or height < 256:
                return {"passed": False, "reason": f"image too small ({width}x{height})"}
    except Exception as e:
        return {"passed": False, "reason": f"not a valid image: {e}"}
    
    return {"passed": True}


@activity.defn
async def generate_ref_activity(
    episode_id: str,
    ref_key: str,
    prompt: str,
    output_path: str,
    version: int = 1,
    model: str = "soul_v2",
    resolution: str = "2k",
) -> dict:
    """
    Generate a reference image (character sheet, lineup, etc.).
    
    Args:
        episode_id: Episode identifier
        ref_key: Reference key (e.g., "r_duck_sheet", "r_lineup")
        prompt: Generation prompt
        output_path: Where to write the generated image
        version: Version number for idempotency (increments on reroll)
        model: Model to use (default: soul_v2)
        resolution: Image resolution
    
    Returns:
        dict with status, job_id, output_path, credits_used
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Idempotent job_id
    job_id = f"ref-{episode_id}-{ref_key}-v{version}"
    
    activity.logger.info(f"Generating ref {ref_key} v{version} for {episode_id}")
    
    # Estimate credits
    from hfvg.credit_pricing import estimate_still_credits
    quality = "high" if resolution == "2k" else "standard"
    estimated_credits = estimate_still_credits(model, quality)
    
    # Reserve credits from credit ledger
    ledger = CreditLedger(db_path)
    await ledger.init_db()
    
    try:
        await ledger.reserve_credits(
            episode_id,
            job_id,
            estimated_credits,
            notes=f"ref {ref_key} v{version}",
        )
    except Exception as e:
        activity.logger.error(f"Failed to reserve credits for {job_id}: {e}")
        raise
    
    try:
        if dry_run:
            # Dry mode: create fake output
            activity.logger.info(f"[DRY-RUN] Generated ref {ref_key} -> {output_path}")
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake-ref-image")
            provider_job_id = f"fake-{job_id}"
        else:
            # Live mode: call provider
            provider = HiggsfieldProvider(dry_run=False)
            provider_job_id = await provider.submit_image(
                prompt=prompt,
                model=model,
                resolution=resolution,
                idempotency_key=job_id,
            )
            
            # Poll until complete
            status = await provider.poll_job(provider_job_id)
            if status.status != "completed":
                raise ValueError(f"Ref generation failed: {status.error}")
            
            # Download and save
            if not status.output_urls:
                raise ValueError("No output URLs in completed job")
            
            # Download first image
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(status.output_urls[0])
                resp.raise_for_status()
                
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(resp.content)
            
            activity.logger.info(f"Downloaded ref {ref_key} to {output_path}")
            
            # Run QC
            qc_result = await _check_ref_qc(output_path)
            if not qc_result["passed"]:
                raise ValueError(f"Ref QC failed: {qc_result.get('reason', 'unknown')}")
        
        # Commit credits
        await ledger.commit_credits(episode_id, job_id, estimated_credits)
        
        return {
            "status": "completed",
            "job_id": job_id,
            "provider_job_id": provider_job_id,
            "output_path": output_path,
            "credits_used": estimated_credits,
        }
        
    except Exception as e:
        # Release reserved credits on failure
        activity.logger.error(f"Ref generation failed for {job_id}: {e}")
        await ledger.release_credits(episode_id, job_id)
        raise


@activity.defn
async def load_refs_to_generate_activity(episode_id: str, shots_subset: list[str] | None = None) -> list[dict]:
    """
    Load refs.json and extract to_generate list, filtered by shots_subset.
    
    Validates each entry has required fields: key, prompt, output_path, type.
    
    Args:
        episode_id: Episode identifier
        shots_subset: Optional list of shot IDs to filter refs
    
    Returns:
        List of refs to generate, each with keys: key, prompt, output_path, type, version
    
    Raises:
        ValueError: If any to_generate entry is missing required fields
    """
    from hfvg.shot_data import refs_json_path
    
    refs_path = refs_json_path(episode_id)
    if not refs_path.exists():
        activity.logger.warning(f"No refs.json for {episode_id}, no refs to generate")
        return []
    
    refs_data = json.loads(refs_path.read_text())
    to_generate = refs_data.get("to_generate", [])
    
    if not to_generate:
        activity.logger.info(f"No to_generate list in refs.json for {episode_id}")
        return []
    
    # Validate each entry has required fields
    required_fields = ["key", "prompt", "output_path", "type"]
    for i, ref_spec in enumerate(to_generate):
        for field in required_fields:
            if field not in ref_spec or not ref_spec[field]:
                raise ValueError(
                    f"refs.json to_generate[{i}] missing required field '{field}'. "
                    f"Entry: {ref_spec}. Required fields: {required_fields}"
                )
    
    # Filter by shots_subset if provided
    if shots_subset:
        # Load per_shot to see which refs are used by subset shots
        per_shot = refs_data.get("per_shot", {})
        needed_keys = set()
        for shot_id in shots_subset:
            shot_refs = per_shot.get(shot_id, [])
            for ref_entry in shot_refs:
                if isinstance(ref_entry, dict):
                    needed_keys.add(ref_entry.get("key"))
        
        # Filter to_generate to only refs needed by subset
        filtered = [ref for ref in to_generate if ref.get("key") in needed_keys]
        activity.logger.info(
            f"Filtered to_generate from {len(to_generate)} to {len(filtered)} "
            f"refs needed by shots_subset {shots_subset}"
        )
        return filtered
    
    return to_generate
