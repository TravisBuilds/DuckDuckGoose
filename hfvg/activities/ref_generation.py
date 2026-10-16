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
    
    # In dry mode, redirect output to scratch directory but keep original path for checking
    original_output_path = output_path
    if dry_run:
        import temporalio.activity
        workflow_id = temporalio.activity.info().workflow_id
        scratch_dir = Path(f"data/dry_run_outputs/{workflow_id}/refs")
        scratch_dir.mkdir(parents=True, exist_ok=True)
        # Check original location for existing files, but work in scratch
        output_path = str(scratch_dir / Path(original_output_path).name)
        activity.logger.info(f"Dry mode: redirecting output to {output_path}")
    
    # Check if ref already exists at original location (even in dry mode, check original)
    check_path = original_output_path if dry_run else output_path
    if Path(check_path).exists():
        # Check version match (stored in sidecar file at original location)
        version_sidecar = Path(check_path).parent / ".versions" / f"{Path(check_path).stem}.json"
        existing_version = 1  # Default version
        
        if version_sidecar.exists():
            try:
                version_data = json.loads(version_sidecar.read_text())
                existing_version = version_data.get("version", 1)
            except Exception as e:
                activity.logger.warning(f"Failed to read version sidecar: {e}")
        
        if existing_version != version:
            # Version mismatch - archive old version at original location
            # In dry mode, DON'T archive real files - skip archiving entirely
            if not dry_run:
                activity.logger.info(
                    f"Ref {ref_key} version mismatch (file: v{existing_version}, requested: v{version}). "
                    "Archiving old version and regenerating..."
                )
                archive_dir = Path(check_path).parent / f"_v{existing_version}"
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_path = archive_dir / Path(check_path).name
                Path(check_path).rename(archive_path)
                activity.logger.info(f"Archived v{existing_version} to {archive_path}")
            else:
                activity.logger.info(
                    f"Ref {ref_key} version mismatch (file: v{existing_version}, requested: v{version}). "
                    f"[DRY MODE] Skipping archive of real file, will generate in scratch."
                )
        else:
            # Version matches - check QC
            activity.logger.info(f"Ref {ref_key} v{version} already exists, checking QC...")
            qc_result = await _check_ref_qc(check_path)
            if qc_result["passed"]:
                activity.logger.info(f"Ref {ref_key} v{version} exists and passes QC - reusing (no spend)")
                return {
                    "status": "reused",
                    "job_id": job_id,
                    "provider_job_id": None,
                    "output_path": output_path,  # Return scratch path in dry mode, original otherwise
                    "credits_used": 0,
                }
            else:
                activity.logger.warning(
                    f"Ref {ref_key} v{version} exists but fails QC: {qc_result.get('reason')}. Regenerating..."
                )
    
    activity.logger.info(f"Generating ref {ref_key} v{version} for {episode_id}")
    
    # Estimate credits
    # Refs use SOUL V2: higgsfield-ai/soul/v2/standard
    model_path = "higgsfield-ai/soul/v2/standard"
    from hfvg.credit_pricing import estimate_ref_credits
    estimated_credits = estimate_ref_credits(model_path)
    
    # Estimate USD cost for ref generation from pricing table (not hardcoded literal)
    # SOUL V2: credits ≈ USD (both ~0.09), so convert credits to USD micros
    from hfvg.pricing import usd_to_micros
    estimated_usd_micros = usd_to_micros(str(estimated_credits))
    
    # Initialize USD ledger for live mode
    from hfvg.budget import BudgetLedger
    usd_ledger = BudgetLedger(db_path)
    await usd_ledger.init_db()
    
    # Reserve USD budget from L1_refs line (live mode only)
    if not dry_run:
        # Gate: Smoke test (live mode only)
        from hfvg.studio_db import enforce_smoke_test_gate
        await enforce_smoke_test_gate(db_path, episode_id, "generate_ref_activity")
        
        usd_reserved = await usd_ledger.reserve(
            episode_id=episode_id,
            line_name="L1_refs",
            amount_usd_micros=estimated_usd_micros,
            reason=f"Ref {ref_key} v{version}",
            require_balance=True,  # Always require balance in live mode
        )
        
        if not usd_reserved:
            # Release credit reservation since USD reserve failed
            await ledger.release_credits(episode_id, job_id, notes="USD reserve failed")
            raise ValueError(
                f"USD budget reserve failed for L1_refs. "
                f"At or over 80% stop threshold. Requested: {estimated_usd_micros} usd_micros."
            )
    
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
            
            # Write version sidecar (dry mode)
            version_sidecar_dir = Path(output_path).parent / ".versions"
            version_sidecar_dir.mkdir(parents=True, exist_ok=True)
            version_sidecar = version_sidecar_dir / f"{Path(output_path).stem}.json"
            version_sidecar.write_text(json.dumps({"version": version, "ref_key": ref_key}))
            
            # Commit credits only (no USD reservation in dry mode)
            await ledger.commit_credits(episode_id, job_id, estimated_credits)
        else:
            # Live mode: call provider
            # Check if we already submitted this job (activity retry)
            existing_provider_job_id = await ledger.get_provider_job(job_id)
            
            if existing_provider_job_id:
                # Activity retry: resume polling existing job
                activity.logger.info(
                    f"Resuming polling for {job_id} -> provider job {existing_provider_job_id}"
                )
                provider_job_id = existing_provider_job_id
            else:
                # First attempt: submit to provider
                provider = HiggsfieldProvider(dry_run=False)
                provider_job_id = await provider.submit_image(
                    prompt=prompt,
                    model=model,
                    resolution=resolution,
                    idempotency_key=job_id,
                )
                activity.logger.info(f"Submitted {job_id} -> provider job {provider_job_id}")
                
                # Persist provider job ID immediately (before polling)
                await ledger.persist_provider_job(job_id, provider_job_id, provider="higgsfield")
            
            # Poll until complete (raises RuntimeError on failure)
            provider = HiggsfieldProvider(dry_run=False)
            job = await provider.poll_job(provider_job_id)
            
            # Provider completed - commit credits and USD immediately (charged whether QC passes or not)
            await ledger.commit_credits(episode_id, job_id, estimated_credits)
            await usd_ledger.commit(
                episode_id=episode_id,
                line_name="L1_refs",
                reserved_usd_micros=estimated_usd_micros,
                actual_usd_micros=estimated_usd_micros,
                job_id=job_id,
                reason=f"Ref {ref_key} v{version} completed"
            )
            activity.logger.info(f"Committed credits and USD for {job_id} (provider completed)")
            
            # Download and save to staging path (not final path yet)
            if not job.output_url:
                raise ValueError("No output URL in completed job")
            
            staging_path = Path(output_path).parent / f".staging_{Path(output_path).name}"
            staging_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download image to staging
            import httpx
            async with httpx.AsyncClient() as client:
                resp = await client.get(job.output_url)
                resp.raise_for_status()
                staging_path.write_bytes(resp.content)
            
            activity.logger.info(f"Downloaded ref {ref_key} to staging: {staging_path}")
            
            # Run QC on staging file
            qc_result = await _check_ref_qc(str(staging_path))
            if not qc_result["passed"]:
                # QC failed - keep staging file for review, don't write to final path
                from hfvg.errors import QCFailedError
                raise QCFailedError(
                    f"Ref QC failed for {ref_key}: {qc_result.get('reason', 'unknown')}. "
                    f"Staging file kept at {staging_path} for review. "
                    "Reroll with new version or accept at gate.",
                    staging_path=str(staging_path)
                )
            
            # QC passed - move to final path atomically
            staging_path.rename(output_path)
            activity.logger.info(f"QC passed, moved to final path: {output_path}")
            
            # Write version sidecar
            version_sidecar_dir = Path(output_path).parent / ".versions"
            version_sidecar_dir.mkdir(parents=True, exist_ok=True)
            version_sidecar = version_sidecar_dir / f"{Path(output_path).stem}.json"
            version_sidecar.write_text(json.dumps({"version": version, "ref_key": ref_key}))
            activity.logger.info(f"Wrote version sidecar: v{version}")
        
        return {
            "status": "completed",
            "job_id": job_id,
            "provider_job_id": provider_job_id,
            "output_path": output_path,
            "credits_used": estimated_credits,
        }
        
    except Exception as e:
        # Only release credits and USD if provider never completed
        # Check if credits were already committed
        import aiosqlite
        committed = False
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM credit_transactions WHERE job_id = ? AND txn_type = 'commit'",
                (job_id,)
            ) as cursor:
                row = await cursor.fetchone()
                committed = row[0] > 0
        
        if not committed:
            # Provider failed before completing - release credits (and USD in live mode)
            activity.logger.info(f"Provider failed, releasing credits and USD for {job_id}: {e}")
            await ledger.release_credits(episode_id, job_id)
            if not dry_run:
                await usd_ledger.release(
                    episode_id=episode_id,
                    line_name="L1_refs",
                    amount_usd_micros=estimated_usd_micros,
                    reason=f"Ref {ref_key} failed: {str(e)[:100]}"
                )
        else:
            # Provider completed (charged) but QC or download failed - credits and USD already committed
            activity.logger.info(f"Provider completed, credits and USD already committed for {job_id}")
        
        raise


@activity.defn
async def load_refs_to_generate_activity(
    episode_id: str, 
    shots_subset: list[str] | None = None,
    is_live: bool = False
) -> list[dict]:
    """
    Load refs.json and extract to_generate list, filtered by shots_subset.
    
    Validates each entry has required fields: key, prompt, output_path, type.
    In live mode, fails if refs.json is missing.
    
    When refs.json has no versions map or no version for a ref, defaults to the
    latest version present on disk (from sidecars or _vN archives), not v1.
    
    Args:
        episode_id: Episode identifier
        shots_subset: Optional list of shot IDs to filter refs
        is_live: If True, fail if refs.json missing (live mode requirement)
    
    Returns:
        List of refs to generate, each with keys: key, prompt, output_path, type, version
    
    Raises:
        FileNotFoundError: If refs.json missing in live mode
        ValueError: If any to_generate entry is missing required fields
    """
    from hfvg.shot_data import refs_json_path
    
    def _get_latest_version_on_disk(ref_path: str) -> int:
        """
        Determine latest version present on disk for a ref.
        
        Checks:
        1. Version sidecar at .versions/{stem}.json
        2. Archived versions at refs/_vN/{filename}
        
        Returns:
            Latest version number found, or 1 if none found
        """
        p = Path(ref_path)
        
        # Check version sidecar first
        sidecar = p.parent / ".versions" / f"{p.stem}.json"
        if sidecar.exists():
            try:
                data = json.loads(sidecar.read_text())
                return data.get("version", 1)
            except (json.JSONDecodeError, OSError):
                pass
        
        # Check archived versions: refs/_v2, refs/_v3, etc.
        refs_dir = p.parent
        max_version = 1  # Default
        
        # If current file exists, it's at least v1
        if p.exists():
            max_version = 1
        
        # Scan for _vN directories
        for archive_dir in refs_dir.glob("_v*"):
            if archive_dir.is_dir():
                try:
                    version_num = int(archive_dir.name[2:])  # Extract N from "_vN"
                    archived_file = archive_dir / p.name
                    if archived_file.exists() and version_num > max_version:
                        max_version = version_num
                except (ValueError, OSError):
                    pass
        
        return max_version
    
    refs_path = refs_json_path(episode_id)
    if not refs_path.exists():
        if is_live:
            raise FileNotFoundError(
                f"refs.json required in live mode but not found for {episode_id}. "
                f"Expected at {refs_path}"
            )
        activity.logger.warning(f"No refs.json for {episode_id}, no refs to generate")
        return []
    
    refs_data = json.loads(refs_path.read_text())
    to_generate_raw = refs_data.get("to_generate", [])
    prompts_map = refs_data.get("prompts", {})
    versions_map = refs_data.get("versions", {})  # Map of {key: version}
    
    if not to_generate_raw:
        activity.logger.info(f"No to_generate in refs.json for {episode_id}")
        return []
    
    # Normalize to_generate format
    # Support both map {key: path} and list [{key, prompt, output_path, ...}]
    to_generate = []
    
    if isinstance(to_generate_raw, dict):
        # Map format: {key: path}
        for key, path in to_generate_raw.items():
            # Determine version: versions map, then latest on disk, then 1
            if key in versions_map:
                version = versions_map[key]
            else:
                version = _get_latest_version_on_disk(path)
            
            ref_spec = {
                "key": key,
                "output_path": path,
                "type": "character_sheet",  # default
                "version": version,
            }
            # Get prompt from prompts map if available
            if key in prompts_map:
                ref_spec["prompt"] = prompts_map[key]
            else:
                raise ValueError(
                    f"refs.json to_generate map entry '{key}' has no prompt. "
                    f"Add it to the prompts map: {{\"prompts\": {{\"{key}\": \"your prompt\"}}}} "
                    f"or use list format with prompt inline."
                )
            to_generate.append(ref_spec)
    elif isinstance(to_generate_raw, list):
        # List format: [{key, prompt, output_path, type}, ...]
        for i, ref_spec in enumerate(to_generate_raw):
            if not isinstance(ref_spec, dict):
                raise ValueError(
                    f"refs.json to_generate[{i}] must be an object, got {type(ref_spec).__name__}"
                )
            
            # Apply defaults
            if "type" not in ref_spec:
                ref_spec["type"] = "character_sheet"
            if "version" not in ref_spec:
                # Determine version: versions map, then latest on disk, then 1
                key = ref_spec.get("key")
                output_path = ref_spec.get("output_path")
                
                if key and key in versions_map:
                    ref_spec["version"] = versions_map[key]
                elif output_path:
                    ref_spec["version"] = _get_latest_version_on_disk(output_path)
                else:
                    ref_spec["version"] = 1
            
            # Check for prompt in entry or prompts map
            if "prompt" not in ref_spec or not ref_spec["prompt"]:
                key = ref_spec.get("key")
                if key and key in prompts_map:
                    ref_spec["prompt"] = prompts_map[key]
                else:
                    raise ValueError(
                        f"refs.json to_generate[{i}] missing required field 'prompt'. "
                        f"Entry: {ref_spec}. Add prompt inline or in prompts map."
                    )
            
            to_generate.append(ref_spec)
    else:
        raise ValueError(
            f"refs.json to_generate must be a map {{key: path}} or list of objects, "
            f"got {type(to_generate_raw).__name__}"
        )
    
    # Validate all entries have required fields after normalization
    required_fields = ["key", "prompt", "output_path", "type"]
    for i, ref_spec in enumerate(to_generate):
        for field in required_fields:
            if field not in ref_spec or not ref_spec[field]:
                raise ValueError(
                    f"refs.json to_generate[{i}] missing required field '{field}' "
                    f"after normalization. Entry: {ref_spec}"
                )
    
    # Check which refs are needed by shots
    per_shot = refs_data.get("per_shot", {})
    if shots_subset:
        # Filter to only refs needed by subset shots
        needed_keys = set()
        for shot_id in shots_subset:
            shot_refs = per_shot.get(shot_id, [])
            for ref_entry in shot_refs:
                if isinstance(ref_entry, dict):
                    needed_keys.add(ref_entry.get("key"))
    else:
        # All shots - collect all ref keys
        needed_keys = set()
        for shot_id, shot_refs in per_shot.items():
            for ref_entry in shot_refs:
                if isinstance(ref_entry, dict):
                    needed_keys.add(ref_entry.get("key"))
    
    # In live mode, verify all needed refs are either on disk or in to_generate
    if is_live and needed_keys:
        to_generate_keys = {ref.get("key") for ref in to_generate}
        missing_refs = []
        
        for ref_key in needed_keys:
            # Check if ref exists on disk
            ref_on_disk = False
            for shot_refs in per_shot.values():
                for ref_entry in shot_refs:
                    if ref_entry.get("key") == ref_key:
                        ref_path = ref_entry.get("path")
                        if ref_path and Path(ref_path).exists():
                            ref_on_disk = True
                            break
                if ref_on_disk:
                    break
            
            # If not on disk, must be in to_generate
            if not ref_on_disk and ref_key not in to_generate_keys:
                missing_refs.append(ref_key)
        
        if missing_refs:
            raise ValueError(
                f"Live mode: shots need refs {missing_refs} but they are neither "
                f"on disk nor in to_generate. Add them to refs.json to_generate or "
                f"place the files at their specified paths."
            )
    
    # Filter to_generate to only refs needed by shots_subset
    if shots_subset:
        filtered = [ref for ref in to_generate if ref.get("key") in needed_keys]
        activity.logger.info(
            f"Filtered to_generate from {len(to_generate)} to {len(filtered)} "
            f"refs needed by shots_subset {shots_subset}"
        )
        return filtered
    
    return to_generate
