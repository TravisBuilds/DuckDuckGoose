"""
Studio generation activities with full enforcement.

Enforcement checklist (fail closed):
1. Episode must be in live mode (stored in episodes table)
2. G1.08 credit plan must be approved (gate status in episodes table)
3. Budget reserve must succeed before submitting (BudgetLedger)
4. Idempotency-Key on every provider call
5. Cost estimate before submitting
6. Activity-level enforcement (not just API-level)

Default mode: DRY_RUN (fake providers)
Real providers only when all checks pass.
"""

import os
import asyncio
import uuid
import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from temporalio import activity

from hfvg.budget import BudgetLedger, REVISION_LINE
from hfvg.credit_ledger import CreditLedger
from hfvg.config import config, max_refs_for, still_model_path
from hfvg.pricing import (
    DRY_CLIP_USD_PER_SECOND_MICROS,
    DRY_STILL_USD_MICROS,
    clip_estimate_usd_micros,
    micros_to_usd_str,
    usd_to_micros,
)
from hfvg.credit_pricing import estimate_still_credits, estimate_clip_credits
from hfvg.prompt_lint import enforce_prompt_lint
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider, SeedanceVideoProvider
from hfvg.routing import plan_route


def _get_ref_version_from_sidecar(ref_path: str) -> int:
    """
    Get ref version from sidecar file.
    
    Args:
        ref_path: Path to the reference image
    
    Returns:
        Version number from sidecar, or 1 if no sidecar exists
    """
    ref_path_obj = Path(ref_path)
    version_sidecar = ref_path_obj.parent / ".versions" / f"{ref_path_obj.stem}.json"
    
    if version_sidecar.exists():
        try:
            import json as json_lib
            sidecar_data = json_lib.loads(version_sidecar.read_text())
            return sidecar_data.get("version", 1)
        except (json_lib.JSONDecodeError, OSError):
            # Fall back to v1 if sidecar is corrupted
            return 1
    
    # No sidecar = version 1
    return 1


def _create_fake_still(output_path: str) -> str:
    """Create a small real PNG file for dry-run mode."""
    # Create a minimal valid 1x1 PNG (67 bytes)
    # PNG signature + IHDR + IDAT + IEND chunks
    png_data = (
        b'\x89PNG\r\n\x1a\n'  # PNG signature
        b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x02\x00\x00\x00\x90wS\xde'  # 1x1, RGB
        b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x00\x01\x0d\n-\xb4'
        b'\x00\x00\x00\x00IEND\xaeB`\x82'
    )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(png_data)
    return output_path


def _create_fake_clip(output_path: str) -> str:
    """Create a small real MP4 file for dry-run mode using ffmpeg."""
    import subprocess
    
    # Create a 1-second 64x64 orange video
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run([
            "ffmpeg", "-f", "lavfi", "-i", "color=c=orange:s=64x64:d=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", output_path
        ], check=True, capture_output=True, timeout=10)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        # Fallback: create a minimal valid MP4 if ffmpeg fails
        # This is a minimal ftyp + mdat MP4 container
        mp4_data = (
            b'\x00\x00\x00 ftypisomisomiso2mp41'
            b'\x00\x00\x00\x08free'
            b'\x00\x00\x00\x08mdat'
        )
        Path(output_path).write_bytes(mp4_data)
    return output_path


@activity.defn
async def check_live_mode_and_g108(episode_id: str) -> tuple[bool, bool]:
    """
    Check if episode is in live mode and G1.08 is approved.
    
    Reads DATABASE_PATH from environment (studio DB).
    
    Args:
        episode_id: Episode identifier
    
    Returns:
        (live_mode, g108_approved)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT live_mode, g108_approved FROM episodes WHERE episode_id = ?",
            (episode_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, False
            return bool(row[0]), bool(row[1])


@activity.defn
async def verify_episode_files_activity(episode_id: str) -> dict:
    """
    Verify required files exist for episode in live mode.
    
    Args:
        episode_id: Episode identifier
    
    Returns:
        dict with 'passed' (bool) and 'missing_files' (list)
    
    Raises:
        FileNotFoundError: If shots.json is missing in live mode
    """
    shots_json_path = f"data/episodes/{episode_id}/shots.json"
    
    if not os.path.exists(shots_json_path):
        raise FileNotFoundError(
            f"shots.json missing for episode {episode_id} at {shots_json_path}. "
            "Live mode requires shots.json to be present before workflow start."
        )
    
    return {"passed": True, "missing_files": []}


def generate_idempotency_key(episode_id: str = None, shot_id: str = None, version: int = None, prompt: str = None, **kwargs) -> str:
    """
    Generate deterministic idempotency key from request parameters.
    
    For backward compatibility, accepts legacy positional args.
    For new calls, pass all parameters as kwargs for full coverage:
    - episode_id, shot_id, version, prompt (base)
    - refs, quality, resolution (for stills)
    - start_image, duration (for clips)
    
    Returns: 32-character hex hash
    """
    import json
    
    # Build key data from all parameters
    if episode_id is not None:
        # Legacy positional args provided
        key_data = {
            'episode_id': episode_id,
            'shot_id': shot_id,
            'version': version,
            'prompt': prompt,
        }
        # Also include any kwargs
        key_data.update(kwargs)
    else:
        # New style: all kwargs
        key_data = kwargs
    
    # Sort keys for deterministic ordering
    key_str = json.dumps(key_data, sort_keys=True)
    hash_bytes = hashlib.sha256(key_str.encode()).digest()
    return hash_bytes.hex()[:32]


@activity.defn
async def submit_still_job_enforced(
    episode_id: str,
    shot_id: str,
    prompt: str,
    version: int = 1,
    refs: list[str] | None = None,
    resolution: str = "1k",
    quality: str = "medium",
    aspect_ratio: str = "9:16",
    tier: str | None = None,
    shot_meta: dict[str, Any] | None = None,
    missing_refs: list[str] | None = None,
) -> dict[str, Any]:
    """
    Submit still generation job with full enforcement.
    
    Enforcement:
    - Prompt lint gate (live: refuse BEFORE estimate/reserve/provider call; dry: warn)
    - Episode in live mode (fail if not)
    - G1.08 approved (fail if not)
    - Budget reserve succeeds (fail if at stop)
    - Idempotency-Key (deterministic)
    - Cost estimate (USD) before submit; GC.01 manual balance required
    
    Args:
        tier: draft/final/retry/revision for the per-job ledger. Defaults: draft on L2,
            final on L1/L3. tier="revision" draws on the revision reserve (revision tag).
    
    Returns:
        dict with job_id, line_name, reserved_amount (int usd_micros) for polling
    
    Raises:
        ValueError: If enforcement checks fail
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Gate: Smoke test (live mode only, unless override)
    if not dry_run_env:
        from hfvg.studio_db import enforce_smoke_test_gate
        await enforce_smoke_test_gate(db_path, episode_id, "submit_still_job_enforced")
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        enforce_prompt_lint(episode_id, prompt, shot_meta, live=False)
        if missing_refs:
            activity.logger.warning(
                f"[DRY-RUN] {shot_id}: refs not on disk (live would hold at G2.01): {sorted(missing_refs)}"
            )
        job_id = f"still-dry-{uuid.uuid4().hex[:12]}"
        activity.logger.info(
            f"[DRY-RUN] Submitted still {job_id} for {episode_id}/{shot_id} (DRY_RUN=true)"
        )
        return {
            "job_id": job_id,
            "line_name": "L2_drafts",
            "reserved_amount": DRY_STILL_USD_MICROS,
        }
    
    # Check DB for live mode and G1.08 (DRY_RUN=false, so check requirements)
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    if not live_mode:
        raise ValueError(
            f"Episode {episode_id} not in live mode. "
            "Switch to live mode before generating."
        )
    
    if not g108_approved:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve credit plan before generating."
        )
    
    # Live mode: all checks passed (DRY_RUN=false, live_mode=true, g108_approved=true)
    activity.logger.info(
        f"[LIVE MODE] Submitting paid still generation for {episode_id}/{shot_id}"
    )
    
    # Prompt lint gate: refuses before provider init, estimate, reserve
    enforce_prompt_lint(episode_id, prompt, shot_meta, live=True)
    
    # Refs gate: a shot never runs without its planned refs, and refs are never silently dropped
    if missing_refs:
        raise ValueError(
            f"Shot {shot_id} is held at G2.01: refs not on disk yet: {sorted(missing_refs)}"
        )
    still_cap = max_refs_for(still_model_path())
    if refs and len(refs) > still_cap:
        raise ValueError(
            f"Shot {shot_id} has {len(refs)} refs, over the {still_model_path()} limit of {still_cap}; "
            "refusing instead of silently dropping refs"
        )
    
    # Generate job_id (harness identifier)
    job_id = f"still-{episode_id}-{shot_id}-v{version}"
    
    # Initialize provider
    provider = HiggsfieldStillProvider()
    
    # Initialize ledgers (but don't reserve yet)
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    # Track what we've reserved for cleanup
    credits_reserved = False
    budget_reserved = False
    budget_line_name = None
    budget_amount = 0
    
    try:
        # Upload local reference images to Higgsfield and get URLs
        uploaded_refs = []
        if refs:
            for ref_path in refs:
                # Check if this is a local file path (not already a URL)
                if not ref_path.startswith(("http://", "https://")):
                    # Get ref version from sidecar (not shot version!)
                    ref_version = _get_ref_version_from_sidecar(ref_path)
                    
                    # Upload the local file and get the actual URL from provider
                    upload_result = await provider.upload_reference(ref_path, ref_version)
                    uploaded_url = upload_result["public_url"]
                    uploaded_refs.append(uploaded_url)
                    activity.logger.info(
                        f"Uploaded reference {ref_path} v{ref_version} -> {uploaded_url}"
                    )
                else:
                    # Already a URL, use as-is
                    uploaded_refs.append(ref_path)
        
        # Estimate cost in integer micro-dollars and credits (provider estimate's values)
        estimated_cost, estimated_credits = await provider.estimate_usd_and_credits(
            prompt=prompt,
            resolution=resolution,
            quality=quality,
            aspect_ratio=aspect_ratio,
        )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id}: {micros_to_usd_str(estimated_cost)} USD, {estimated_credits} credits"
        )
        
        # Reserve credits AFTER upload/estimate succeed
        credit_result = await credit_ledger.reserve_credits(
            episode_id, job_id, estimated_credits,
            notes=f"still {shot_id} {quality} {resolution}"
        )
        
        if not credit_result.get("reserved"):
            available = credit_result.get("available", 0)
            over_cap = credit_result.get("over_cap", False)
            
            if over_cap:
                from hfvg.errors import InsufficientCreditsError
                raise InsufficientCreditsError(
                    f"Credit hard cap reached for {job_id}. "
                    f"Available: {available} credits. Requested: {estimated_credits} credits. "
                    "Raise cap via raise_credit_cap signal to resume.",
                    required=float(estimated_credits),
                    available=float(available)
                )
            else:
                raise ValueError(
                    f"Credit reserve failed for {job_id}. "
                    f"Available: {available} credits. Requested: {estimated_credits} credits."
                )
        
        credits_reserved = True
        
        # Determine budget line based on quality
        if quality == "high" and resolution == "2k":
            line_name = "L3_final_stills"
            default_tier = "final"
        elif quality == "high":
            line_name = "L1_refs"
            default_tier = "final"
        else:
            line_name = "L2_drafts"
            default_tier = "draft"
        tier = tier or default_tier
        revision = tier == "revision"
        if revision:
            line_name = REVISION_LINE
        
        # Reserve budget
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount_usd_micros=estimated_cost,
            reason=f"Still {shot_id} v{version}",
            job_id=job_id,
            revision=revision,
            require_balance=True,
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {micros_to_usd_str(estimated_cost)} USD."
            )
        
        budget_reserved = True
        budget_line_name = line_name
        budget_amount = estimated_cost
        
        activity.logger.info(
            f"Reserved {micros_to_usd_str(estimated_cost)} USD from {line_name} for {shot_id}"
        )
        
        # Generate idempotency key from hash of actual request with uploaded URLs
        import hashlib
        refs_str = ",".join(sorted(uploaded_refs)) if uploaded_refs else ""
        key_input = f"{model_path}|{prompt}|{resolution}|{quality}|{aspect_ratio or ''}|{refs_str}"
        key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        idempotency_key = f"{job_id}-{key_hash}"
        
        # Submit to provider
        provider_job_id = await provider.submit_image(
            prompt=prompt,
            resolution=resolution,
            quality=quality,
            image_urls=uploaded_refs,
            idempotency_key=idempotency_key,
            aspect_ratio=aspect_ratio,
        )
        
        activity.logger.info(
            f"Submitted still {job_id} to provider (provider_job_id={provider_job_id})"
        )
        
        # Persist the harness→provider mapping
        await credit_ledger.persist_provider_job(job_id, provider_job_id, provider="higgsfield")
        
        # Record job in ledger
        try:
            await ledger.record_job(
                episode_id, job_id, shot_id, provider.model_path, tier, budget_line_name,
                budget_amount,
            )
        except aiosqlite.Error as ledger_err:
            # The provider already accepted (and will charge) this job: NEVER release the hold
            # because the bookkeeping row failed. The reservation stays held.
            activity.logger.error(
                f"Job {job_id} (provider {provider_job_id}) submitted but per-job ledger row failed: {ledger_err}. "
                f"Reservation HELD."
            )
        
        # Return success
        return {
            "job_id": job_id,
            "provider_job_id": provider_job_id,
            "line_name": budget_line_name,
            "reserved_amount": budget_amount,
        }
    
    except httpx.HTTPStatusError as e:
        # Capture provider error details (especially 422 idempotency conflicts)
        error_detail = f"Provider {e.response.status_code}: {e.response.text}"
        activity.logger.error(f"Provider error for {job_id}: {error_detail}")
        # Release whatever has been reserved
        if budget_reserved:
            await ledger.release(episode_id, budget_line_name, budget_amount, f"Provider error: {error_detail}")
        if credits_reserved:
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Provider error: {error_detail}")
        raise ValueError(error_detail) from e
    except Exception as e:
        # Release whatever has been reserved on any other error
        if budget_reserved:
            await ledger.release(episode_id, budget_line_name, budget_amount, f"Submit failed: {str(e)}")
        if credits_reserved:
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Submit failed: {str(e)}")
        raise
    finally:
        await provider.close()


@activity.defn
async def submit_clip_job_enforced(
    episode_id: str,
    shot_id: str,
    start_image_url: str,
    prompt: str,
    duration: float = 5.0,
    version: int = 1,
    tier: str | None = None,
    shot_meta: dict[str, Any] | None = None,
    model: str = "kling",
    tags: list[str] | None = None,
    end_image_url: str | None = None,
    bitrate_mode: str | None = None,
    missing_refs: list[str] | None = None,
) -> dict[str, Any]:
    """
    Submit a video generation job (Kling 3.0 Pro or Seedance 2.5) with full enforcement.
    
    Args:
        model: explicit routing, "kling" (default) or "seedance". The planner refuses Seedance
            for shots tagged ``wet`` or shorter than 4 s (RoutingError, fail closed).
        tier: ledger tier draft/final/retry/revision. For Seedance draft -> 480p, anything else
            -> 1080p (a fresh submit; draft_job_id finalize is not implemented).
        end_image_url: optional last frame (Kling: sent as the documented ``last_image_url``).
    
    Enforcement:
    - Routing plan (fail closed) and prompt lint gate (live: refuse before estimate/reserve)
    - Episode in live mode (fail if not)
    - G1.08 approved (fail if not)
    - Budget reserve succeeds (fail if at stop)
    - Idempotency-Key (deterministic)
    - Cost estimate before submit
    
    Returns:
        dict with job_id, line_name, reserved_amount for polling
    
    Raises:
        ValueError: If enforcement checks fail
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Check DB for live mode and G1.08
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    # Decision logic: DRY_RUN env can force dry, but never force live
    # Only go live if: DRY_RUN=false AND DB live_mode=true AND DB g108_approved=true
    use_live = not dry_run_env and live_mode and g108_approved
    
    # Extract animal count from shot metadata (characters set)
    characters = (shot_meta or {}).get("characters", set())
    if isinstance(characters, (list, set)):
        animal_count = len(characters)
    else:
        animal_count = 1
    
    # Explicit routing plan (fail closed in every mode)
    route = plan_route({
        "model": model,
        "tier": "draft" if tier == "draft" else "final",
        "duration": duration,
        "tags": tags or [],
        "count": animal_count,
    })
    if route.model == "seedance" and duration != int(duration):
        raise ValueError(f"Seedance duration must be a whole number of seconds, got {duration}")
    
    # Prompt lint gate: live refuses before provider init/estimate/reserve; dry only warns
    enforce_prompt_lint(episode_id, prompt, shot_meta, live=use_live)
    
    if use_live:
        # A shot never runs without its planned refs or its END frame
        if missing_refs:
            raise ValueError(
                f"Shot {shot_id} is held at G2.01: refs not on disk yet: {sorted(missing_refs)}"
            )
        if (shot_meta or {}).get("end_frame") and not end_image_url:
            raise ValueError(
                f"Shot {shot_id} is a start+end shot but no end_image_url was given; refusing a start-only clip"
            )
    
    if not use_live:
        # Dry run mode: use fake provider
        job_id = f"clip-dry-{uuid.uuid4().hex[:12]}"
        reason = []
        if dry_run_env:
            reason.append("DRY_RUN=true")
        if not live_mode:
            reason.append("DB live_mode=false")
        if not g108_approved:
            reason.append("DB g108_approved=false")
        
        activity.logger.info(
            f"[DRY-RUN] Submitted clip {job_id} for {episode_id}/{shot_id} "
            f"({', '.join(reason)})"
        )
        # Return dict with job info for polling
        return {
            "job_id": job_id,
            "line_name": "L4_video",
            "reserved_amount": clip_estimate_usd_micros(route.model, tier, duration),
        }
    
    # Live mode: all checks passed (DRY_RUN=false, live_mode=true, g108_approved=true)
    activity.logger.info(
        f"[LIVE MODE] Submitting paid clip generation for {episode_id}/{shot_id}"
    )
    
    # Gate: Smoke test (live mode only)
    from hfvg.studio_db import enforce_smoke_test_gate
    await enforce_smoke_test_gate(db_path, episode_id, "submit_clip_job_enforced")
    
    # Double-check: these should never happen due to use_live logic, but fail-safe
    if not live_mode:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve credit plan before generating."
        )
    
    # Generate harness job_id
    job_id = f"clip-{episode_id}-{shot_id}-v{version}"
    
    # Generate idempotency key from hash of actual request parameters
    # This ensures identical requests dedupe but different requests don't collide
    import hashlib
    key_input = f"{route.model}|{start_image_url}|{end_image_url or ''}|{prompt}|{duration}"
    key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
    idempotency_key = f"{job_id}-{key_hash}"
    
    # Initialize provider
    provider = SeedanceVideoProvider() if route.model == "seedance" else KlingVideoProvider()
    
    try:
        # Estimate cost in micro-dollars and credits
        if route.model == "seedance":
            # Seedance: configurable per-second USD rate table (list price before discount)
            estimated_cost, estimated_credits = await provider.estimate_usd_and_credits(int(duration), route.resolution)
        else:
            # Kling 3.0 Pro: estimate's USD and credits, with fallback to rate table
            estimated_cost, estimated_credits = await provider.estimate_usd_and_credits(
                image_url=start_image_url,
                prompt=prompt,
                duration=int(duration),
                end_image_url=end_image_url,
            )
        
        activity.logger.info(
            f"Estimated cost for {episode_id}/{shot_id} clip: "
            f"{micros_to_usd_str(estimated_cost)} USD, {estimated_credits} credits ({duration}s)"
        )
        
        # Reserve budget from L4_video (revision tier draws on the revision reserve)
        tier = tier or "final"
        revision = tier == "revision"
        line_name = REVISION_LINE if revision else "L4_video"
        
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        
        reserved = await ledger.reserve(
            episode_id=episode_id,
            line_name=line_name,
            amount_usd_micros=estimated_cost,
            reason=f"Clip {shot_id} v{version} ({duration}s)",
            job_id=job_id,
            revision=revision,
            require_balance=True,
        )
        
        if not reserved:
            raise ValueError(
                f"Budget reserve failed for {line_name}. "
                f"At or over 80% stop threshold. Requested: {micros_to_usd_str(estimated_cost)} USD."
            )
        
        activity.logger.info(
            f"Reserved {micros_to_usd_str(estimated_cost)} USD from {line_name} for {shot_id}"
        )
        
        # Reserve credits from credit ledger
        credit_ledger = CreditLedger(db_path)
        await credit_ledger.init_db()
        
        credit_result = await credit_ledger.reserve_credits(
            episode_id, job_id, estimated_credits,
            notes=f"clip {shot_id} {model} {duration}s"
        )
        
        if not credit_result.get("reserved"):
            available = credit_result.get("available", 0)
            over_cap = credit_result.get("over_cap", False)
            
            # Release budget reservation
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=estimated_cost,
                reason=f"Credit reserve failed: available={available}"
            )
            
            if over_cap:
                # Hard cap hit - pause workflow (non-retryable, wait for cap raise signal)
                from hfvg.errors import InsufficientCreditsError
                raise InsufficientCreditsError(
                    f"Credit hard cap reached for {job_id}. "
                    f"Available: {available} credits. Requested: {estimated_credits} credits. "
                    "Raise cap via raise_credit_cap signal to resume.",
                    required=float(estimated_credits),
                    available=float(available)
                )
            else:
                raise ValueError(
                    f"Credit reserve failed for {job_id}. "
                    f"Available: {available} credits. Requested: {estimated_credits} credits."
                )
        
        # Validate and upload start_image if it's a local path
        if not start_image_url or not start_image_url.startswith(("http://", "https://", "/")):
            # Release both budget and credit reservations on validation failure
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=estimated_cost,
                reason=f"Invalid start_image: {start_image_url}"
            )
            await credit_ledger.release_credits(episode_id, job_id, notes=f"invalid start_image")
            raise ValueError(f"Invalid start_image URL: {start_image_url}")
        
        # Upload local paths to Higgsfield CDN (wrap in try/except to release on upload failure)
        try:
            if start_image_url.startswith("/"):
                activity.logger.info(f"Uploading local start image: {start_image_url}")
                # Get ref version from sidecar (not shot version!)
                ref_version = _get_ref_version_from_sidecar(start_image_url)
                upload_result = await provider.upload_reference(start_image_url, ref_version)
                if isinstance(upload_result, dict):
                    start_image_url = upload_result["public_url"]
                else:
                    start_image_url = upload_result
                activity.logger.info(f"Uploaded start image v{ref_version} -> {start_image_url}")
            
            if end_image_url and end_image_url.startswith("/"):
                activity.logger.info(f"Uploading local end image: {end_image_url}")
                # Get ref version from sidecar (not shot version!)
                ref_version = _get_ref_version_from_sidecar(end_image_url)
                upload_result = await provider.upload_reference(end_image_url, ref_version)
                if isinstance(upload_result, dict):
                    end_image_url = upload_result["public_url"]
                else:
                    end_image_url = upload_result
                activity.logger.info(f"Uploaded end image v{ref_version} -> {end_image_url}")
        except Exception as e:
            # Release both reservations on upload failure
            await ledger.release(episode_id, line_name, estimated_cost, f"Upload failed: {str(e)}")
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Upload failed: {str(e)}")
            raise
        
        # Generate idempotency key from hash of actual request with uploaded URLs
        import hashlib
        key_input = f"{route.model}|{start_image_url}|{end_image_url or ''}|{prompt}|{duration}"
        key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        idempotency_key = f"{job_id}-{key_hash}"
        
        # Submit to provider - wrapped to release reservations on ANY error
        try:
            if route.model == "seedance":
                provider_job_id = await provider.submit_video(
                    image_url=start_image_url,
                    prompt=prompt,
                    duration=int(duration),
                    resolution=route.resolution,
                    bitrate_mode=bitrate_mode,
                    end_image_url=end_image_url,
                    idempotency_key=idempotency_key,
                )
            else:
                provider_job_id = await provider.submit_video(
                    image_url=start_image_url,
                    prompt=prompt,
                    duration=int(duration),  # Must be integer
                    idempotency_key=idempotency_key,
                    end_image_url=end_image_url,
                )
        except httpx.HTTPStatusError as e:
            # Capture provider error details (especially 422 idempotency conflicts)
            error_detail = f"Provider {e.response.status_code}: {e.response.text}"
            activity.logger.error(f"Provider error for {job_id}: {error_detail}")
            # Release both reservations before re-raising
            await ledger.release(episode_id, line_name, estimated_cost, f"Provider error: {error_detail}")
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Provider error: {error_detail}")
            raise ValueError(error_detail) from e
        except Exception as e:
            # Release both reservations on any other error
            await ledger.release(episode_id, line_name, estimated_cost, f"Submit failed: {str(e)}")
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Submit failed: {str(e)}")
            raise
        
        # Post-submit success handling (provider accepted and will charge)
        # Store the provider job id for polling (not the harness job_id)
        if provider_job_id != job_id:
            activity.logger.info(
                f"Provider returned job_id {provider_job_id}, storing for polling (harness key: {job_id})"
            )
        
        # BUG 2 fix for clips: Persist the harness→provider job mapping
        await credit_ledger.persist_provider_job(job_id, provider_job_id, provider.model_path)
        activity.logger.info(f"Persisted clip job mapping: {job_id} -> {provider_job_id}")
        
        activity.logger.info(
            f"[LIVE] Submitted {route.model} clip {provider_job_id} for {episode_id}/{shot_id} "
            f"(cost: {micros_to_usd_str(estimated_cost)} USD, {duration}s)"
        )
        try:
            await ledger.record_job(
                episode_id, job_id, shot_id, provider.model_path, tier, line_name,
                estimated_cost,
            )
        except aiosqlite.Error as ledger_err:
            # The provider already accepted (and will charge) this job: NEVER release the hold
            # because the bookkeeping row failed. The reservation stays held.
            activity.logger.error(
                f"Job {job_id} (provider {provider_job_id}) submitted but per-job ledger row failed: {ledger_err}. "
                f"Reservation HELD."
            )
        
        # Return dict with job info for polling
        return {
            "job_id": job_id,  # Return harness job_id (not provider's job_id)
            "provider_job_id": provider_job_id,  # Also return provider job_id for polling
            "line_name": line_name,
            "reserved_amount": estimated_cost,
        }
        
    finally:
        await provider.close()


@activity.defn
async def poll_job_status(
    job_id: str,
    job_type: str,
    episode_id: str,
) -> dict[str, Any]:
    """
    Poll job status once (short activity, ≤60s).
    
    This activity is called repeatedly from the workflow with workflow.sleep between calls.
    It does NOT release reservations on timeout - the workflow handles reconciliation.
    
    Args:
        job_id: Harness job ID (will be resolved to provider job ID for polling)
        job_type: "still" or "clip"
        episode_id: Episode ID
    
    Returns:
        dict with status (str), output_url (str|None), error (str|None), cost (float|None)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Resolve harness job_id to provider job_id for polling
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    provider_job_id = await credit_ledger.get_provider_job(job_id)
    
    if not provider_job_id:
        # In live mode, missing mapping is a hard error (Bug 2 fix)
        if not dry_run_env:
            raise ValueError(
                f"No provider job mapping found for {job_id} in live mode. "
                "This indicates a bug - provider job must be persisted after submission."
            )
        # Dry mode: assume job_id is already a provider ID (backward compat)
        provider_job_id = job_id
        activity.logger.warning(
            f"No provider job mapping found for {job_id} in dry mode, assuming it's a provider ID"
        )
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(0.1)
        
        # Create real placeholder file
        output_dir = Path("./data/dry_run_outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Use job_id in filename (it already contains episode and shot info)
        if job_type == "still":
            output_file = output_dir / f"{job_id}.jpg"
            output_path = _create_fake_still(str(output_file))
        else:
            output_file = output_dir / f"{job_id}.mp4"
            output_path = _create_fake_clip(str(output_file))
        
        return {
            "status": "completed",
            "output_url": str(output_path),
            "error": None,
            "cost": None,
        }
    
    # Check DB for live mode
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    if not live_mode:
        raise ValueError(
            f"Episode {episode_id} not in live mode. "
            "Switch to live mode before polling."
        )
    
    if not g108_approved:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve before polling."
        )
    
    # Live mode: poll provider
    if job_type == "still":
        provider = HiggsfieldStillProvider()
    else:
        provider = KlingVideoProvider()
    
    try:
        job_status = await provider.get_job_status(provider_job_id)  # Use provider ID for polling
        
        # Check for terminal failure statuses - fail fast and release reservations
        if job_status.status.value in ("failed", "blocked", "canceled"):
            # Provider job failed - must fail fast, release BOTH credit and USD reservations,
            # and surface the error message
            activity.logger.error(
                f"Job {provider_job_id} (harness {job_id}) {job_status.status.value}: {job_status.error}"
            )
            
            # Release credit reservation using harness job_id
            credit_ledger = CreditLedger(os.getenv("DATABASE_PATH", "./data/studio.db"))
            await credit_ledger.init_db()
            await credit_ledger.release_credits(
                episode_id, job_id, notes=f"{job_status.status.value}: {job_status.error}"
            )
            
            # Note: USD reservation release is handled by the workflow when it sees this status
            # The workflow calls release_job_budget with the job info it stored from submit
            
            # Return terminal failure status immediately
            return {
                "status": job_status.status.value,  # "failed", "blocked", or "canceled"
                "output_url": None,
                "error": job_status.error or f"Job {job_status.status.value}",
                "cost": None,
            }
        
        # Return status without taking any budget actions for non-terminal states
        # The workflow will handle commit/release based on terminal status
        return {
            "status": job_status.status.value,  # "completed", "failed", "blocked", "canceled", "in_progress", "queued"
            "output_url": job_status.output_url if job_status.status.value == "completed" else None,
            "error": job_status.error if job_status.status.value in ("failed", "blocked") else None,
            # Provider status carries credits (not USD); the ledger commits the held estimate
            "cost": None,
        }
    finally:
        await provider.close()


@activity.defn
async def commit_job_credits(episode_id: str, job_id: str, actual_credits: int) -> dict:
    """Commit reserved credits with actual cost."""
    credit_ledger = CreditLedger()
    await credit_ledger.init_db()
    await credit_ledger.commit_credits(episode_id, job_id, actual_credits)
    return {"success": True, "job_id": job_id, "credits": actual_credits}


@activity.defn
async def release_job_credits(episode_id: str, job_id: str) -> dict:
    """Release reserved credits (job failed/canceled)."""
    credit_ledger = CreditLedger()
    await credit_ledger.init_db()
    await credit_ledger.release_credits(episode_id, job_id)
    return {"success": True, "job_id": job_id}


@activity.defn
async def commit_job_budget(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
    line_name: str,
    reserved_amount: int,
    actual_cost: int | None = None,
) -> None:
    """
    Commit reserved budget after successful job completion.
    
    In dry mode, this is a no-op since no reservation was made.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
        line_name: Budget line
        reserved_amount: Amount reserved
        actual_cost: Actual cost in usd_micros (defaults to reserved_amount)
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # In dry mode, submit never reserved, so commit is a no-op
    if dry_run_env:
        activity.logger.info(
            f"[DRY-RUN] Skipping commit for {job_type} {shot_id} (no reservation was made)"
        )
        return
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    cost = actual_cost if actual_cost is not None else reserved_amount
    
    await ledger.commit(
        episode_id=episode_id,
        line_name=line_name,
        reserved_usd_micros=reserved_amount,
        actual_usd_micros=cost,
        job_id=job_id,
        reason=f"{job_type} {shot_id} completed"
    )
    
    activity.logger.info(
        f"Committed {micros_to_usd_str(cost)} USD to {line_name} for {shot_id}"
    )


@activity.defn
async def release_job_budget(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
    line_name: str,
    reserved_amount: int,
    reason: str,
) -> None:
    """
    Release reserved budget after confirmed job failure.
    
    Only called for confirmed terminal failures (failed, blocked, canceled).
    NOT called on timeout or poll exhaustion - those keep the reservation.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
        line_name: Budget line
        reserved_amount: Amount reserved
        reason: Release reason
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    
    await ledger.release(
        episode_id=episode_id,
        line_name=line_name,
        amount_usd_micros=reserved_amount,
        reason=f"{job_type} {shot_id} {reason}",
        job_id=job_id,
    )
    
    activity.logger.info(
        f"Released {micros_to_usd_str(reserved_amount)} USD from {line_name} for {shot_id}: {reason}"
    )


@activity.defn
async def mark_job_pending_reconcile(
    episode_id: str,
    shot_id: str,
    job_id: str,
    job_type: str,
) -> None:
    """
    Mark job as pending_reconcile in DB after poll timeout/exhaustion.
    
    The reservation remains held. Manual intervention required.
    
    Args:
        episode_id: Episode ID
        shot_id: Shot ID
        job_id: Provider job ID
        job_type: "still" or "clip"
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    
    # Per-job ledger: flag the job (reservation stays held)
    await BudgetLedger(db_path).mark_job_status(job_id, "pending_reconcile")
    
    # Record in audit log
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO audit_log (episode_id, action, details, user)
               VALUES (?, ?, ?, ?)""",
            (episode_id, "job_pending_reconcile",
             f"{job_type} {shot_id} job {job_id} exhausted polling window, reservation held",
             "system")
        )
        await db.commit()
    
    activity.logger.error(
        f"Job {job_id} marked pending_reconcile. Reservation held - manual intervention required."
    )


@activity.defn
async def await_job_enforced(
    job_id: str,
    job_type: str,
    episode_id: str,
    shot_id: str,
    line_name: str,
    reserved_amount: int,
) -> dict[str, Any]:
    """
    Poll generation job and commit/release budget based on result.
    
    DEPRECATED: This activity is being phased out in favor of workflow-based polling
    with poll_job_status(). It remains for backward compatibility with existing workflows.
    
    New workflows should use the workflow polling loop pattern instead.
    
    Args:
        job_id: Provider job ID
        job_type: "still" or "clip"
        episode_id: Episode ID
        shot_id: Shot ID
        line_name: Budget line that reserved
        reserved_amount: Amount reserved
    
    Returns:
        dict with status, url, cost
    
    Raises:
        ValueError: If job fails
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    dry_run_env = os.getenv("DRY_RUN", "true").lower() == "true"
    
    # Gate: Smoke test (live mode only, unless override)
    if not dry_run_env:
        from hfvg.studio_db import enforce_smoke_test_gate
        await enforce_smoke_test_gate(db_path, episode_id, "await_job_enforced")
    
    # DRY_RUN env forces dry mode (kill switch)
    if dry_run_env:
        await asyncio.sleep(0.2)
        
        # Create real placeholder file
        output_dir = Path("./data/dry_run_outputs")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # Use job_id in filename (it already contains episode and shot info)
        if job_type == "still":
            output_file = output_dir / f"{job_id}.jpg"
            output_path = _create_fake_still(str(output_file))
        else:
            output_file = output_dir / f"{job_id}.mp4"
            output_path = _create_fake_clip(str(output_file))
        
        result = {
            "status": "completed",
            "url": str(output_path),
            "cost": reserved_amount,
        }
        activity.logger.info(
            f"[DRY-RUN] Job {job_id} completed: {result['url']} (DRY_RUN=true)"
        )
        return result
    
    # Check DB for live mode and G1.08 (DRY_RUN=false means we need these)
    live_mode, g108_approved = await check_live_mode_and_g108(episode_id)
    
    if not live_mode:
        raise ValueError(
            f"Episode {episode_id} not in live mode. "
            "Switch to live mode before polling."
        )
    
    if not g108_approved:
        raise ValueError(
            f"G1.08 credit plan not approved for {episode_id}. "
            "Approve before polling."
        )
    
    # Live mode: poll provider
    if job_type == "still":
        provider = HiggsfieldStillProvider()
    else:
        provider = KlingVideoProvider()
    
    # Resolve harness job_id to provider job_id
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    provider_job_id = await credit_ledger.get_provider_job(job_id)
    if not provider_job_id:
        raise ValueError(
            f"No provider job mapping found for harness job {job_id}. "
            f"Job may not have been submitted successfully."
        )
    
    activity.logger.info(f"Polling provider job {provider_job_id} (harness: {job_id})")
    
    released = False  # Track if we've already released to prevent double release
    
    try:
        # Phase 1: Active polling (5s intervals, up to 500s ~100 polls)
        max_active_polls = 100
        active_poll_interval = 5.0
        retries_5xx = 0
        max_retries_5xx = 3
        
        for i in range(max_active_polls):
            activity.heartbeat({"job_id": job_id, "provider_job_id": provider_job_id, "poll": i, "phase": "active"})
            
            try:
                job_status = await provider.get_job_status(provider_job_id)
                retries_5xx = 0  # Reset counter on success
            except httpx.HTTPStatusError as http_err:
                # Retry on 5xx errors instead of releasing (job may still be running)
                if http_err.response.status_code >= 500 and retries_5xx < max_retries_5xx:
                    retries_5xx += 1
                    activity.logger.warning(
                        f"5xx error polling {job_id} (attempt {retries_5xx}/{max_retries_5xx}): {http_err}. Retrying..."
                    )
                    await asyncio.sleep(active_poll_interval)  # Wait before retry
                    continue
                # Non-5xx or max retries reached: release and raise
                activity.logger.error(
                    f"Max retries reached or non-5xx error for {job_id}: {http_err}"
                )
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount_usd_micros=reserved_amount,
                    reason=f"{job_type} {shot_id} poll error: {http_err}"
                )
                released = True
                raise
            
            if job_status.status.value == "completed":
                # Success - commit budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                actual_cost = reserved_amount  # status carries credits, not USD: commit the hold
                
                await ledger.commit(
                    episode_id=episode_id,
                    line_name=line_name,
                    reserved_usd_micros=reserved_amount,
                    actual_usd_micros=actual_cost,
                    job_id=job_id,
                    reason=f"{job_type} {shot_id} completed"
                )
                
                activity.logger.info(
                    f"Committed {micros_to_usd_str(actual_cost)} USD to {line_name} for {shot_id}"
                )
                
                return {
                    "status": "completed",
                    "url": job_status.output_url,
                    "cost": actual_cost,
                }
            
            elif job_status.status.value in ("failed", "blocked"):
                # Failed - release budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount_usd_micros=reserved_amount,
                    reason=f"{job_type} {shot_id} {job_status.status.value}"
                )
                released = True  # Mark as released
                
                activity.logger.error(
                    f"Job {job_id} {job_status.status.value}: {job_status.error}"
                )
                
                raise ValueError(
                    f"Generation {job_status.status.value}: {job_status.error}"
                )
            
            # Still processing
            await asyncio.sleep(active_poll_interval)
        
        # Phase 2: Reconciliation polling (longer intervals, up to 30 min total)
        # DO NOT release on timeout - keep polling until we get a terminal status
        activity.logger.warning(
            f"Job {job_id} exceeded active polling window (~{max_active_polls * active_poll_interval}s). "
            f"Entering reconciliation phase with longer backoff (reservation held)."
        )
        
        max_reconciliation_polls = 180  # ~30 min at 10s intervals
        reconciliation_interval = 10.0
        
        for i in range(max_reconciliation_polls):
            activity.heartbeat({"job_id": job_id, "provider_job_id": provider_job_id, "poll": i, "phase": "reconciliation"})
            
            try:
                job_status = await provider.get_job_status(provider_job_id)
            except httpx.HTTPStatusError as http_err:
                # In reconciliation phase, log but keep polling
                activity.logger.warning(
                    f"Poll error in reconciliation phase for {job_id}: {http_err}. Continuing..."
                )
                await asyncio.sleep(reconciliation_interval)
                continue
            
            if job_status.status.value == "completed":
                # Success - commit budget with actual cost
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                actual_cost = reserved_amount  # status carries credits, not USD: commit the hold
                
                await ledger.commit(
                    episode_id=episode_id,
                    line_name=line_name,
                    reserved_usd_micros=reserved_amount,
                    actual_usd_micros=actual_cost,
                    job_id=job_id,
                    reason=f"{job_type} {shot_id} completed (reconciled)"
                )
                
                activity.logger.info(
                    f"[RECONCILED] Committed {micros_to_usd_str(actual_cost)} USD to {line_name} for {shot_id}"
                )
                
                return {
                    "status": "completed",
                    "url": job_status.output_url,
                    "cost": actual_cost,
                }
            
            elif job_status.status.value in ("failed", "blocked", "canceled"):
                # Confirmed terminal failure - release budget
                ledger = BudgetLedger(db_path)
                await ledger.init_db()
                
                await ledger.release(
                    episode_id=episode_id,
                    line_name=line_name,
                    amount_usd_micros=reserved_amount,
                    reason=f"{job_type} {shot_id} {job_status.status.value} (reconciled)"
                )
                released = True
                
                activity.logger.error(
                    f"[RECONCILED] Job {job_id} {job_status.status.value}: {job_status.error}"
                )
                
                raise ValueError(
                    f"Generation {job_status.status.value}: {job_status.error}"
                )
            
            # Still processing - continue reconciliation
            await asyncio.sleep(reconciliation_interval)
        
        # Final timeout after reconciliation phase - reservation STAYS HELD
        # This is a safety net; manual intervention required to release
        activity.logger.error(
            f"Job {job_id} exhausted reconciliation window (total ~{max_active_polls * active_poll_interval + max_reconciliation_polls * reconciliation_interval}s). "
            f"Reservation for {micros_to_usd_str(reserved_amount)} USD on {line_name} remains HELD. "
            f"Manual reconciliation required via provider dashboard."
        )
        
        raise ValueError(
            f"Job {job_id} exhausted polling window. Reservation held - manual reconciliation required."
        )

    
    except asyncio.CancelledError:
        # Activity cancelled: release reservation if not already released
        if not released:
            activity.logger.warning(f"Activity cancelled for job {job_id}, releasing reservation")
            ledger = BudgetLedger(db_path)
            await ledger.init_db()
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=reserved_amount,
                reason=f"{job_type} {job_id} cancelled"
            )
        if 'provider' in locals():
            await provider.close()
        raise
    
    except Exception as poll_error:
        # Poll exception: release reservation if not already released
        if not released:
            activity.logger.error(f"Poll failed for job {job_id}: {poll_error}")
            ledger = BudgetLedger(db_path)
            await ledger.init_db()
            await ledger.release(
                episode_id=episode_id,
                line_name=line_name,
                amount_usd_micros=reserved_amount,
                reason=f"{job_type} {job_id} poll error: {str(poll_error)[:100]}"
            )
        if 'provider' in locals():
            await provider.close()
        raise
        
    finally:
        if 'provider' in locals():
            await provider.close()
