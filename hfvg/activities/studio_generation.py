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
import json
import asyncio
import uuid
import hashlib
from pathlib import Path
from typing import Any

import aiosqlite
import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from hfvg.budget import (
    BudgetLedger,
    REVISION_LINE,
    ROUND_G2_STILLS,
    ROUND_G3_CLIPS,
    RoundBudgetError,
)
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


from hfvg.still_refs import assemble_still_refs  # noqa: F401  (re-exported; the rule lives in one place)
from hfvg.providers import HiggsfieldStillProvider, KlingVideoProvider, SeedanceVideoProvider
from hfvg.providers.base import ProviderJobStatus
from hfvg.routing import plan_route


LEAD_REF_MAX_BYTES = 50 * 1024 * 1024


async def _localize_lead_ref(lead_ref: str, episode_id: str, shot_id: str) -> tuple[str, str]:
    """Return (local file path, sha256 of its bytes) for an _end frame's lead ref (the shot's start
    still). The sha names the job id (-lead<sha8>), so the id follows the exact start still sent.

    A local path is returned unchanged (it must exist). An http(s) URL - e.g. the provider's output
    link for the start still - is downloaded into <DATA_DIR>/lead_refs/ so it is normalized and
    re-uploaded like any other ref instead of being passed to the provider as-is. Raises ValueError
    on any download problem; this runs before credits/budget are reserved, so a failure costs nothing.
    """
    if not lead_ref.startswith(("http://", "https://")):
        if not Path(lead_ref).is_file():
            raise ValueError(f"Lead ref (start still) for {shot_id} not found on disk: {lead_ref}")
        return lead_ref, hashlib.sha256(Path(lead_ref).read_bytes()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            response = await client.get(lead_ref)
    except httpx.HTTPError as e:
        raise ValueError(f"Cannot download lead ref (start still) for {shot_id} from {lead_ref}: {e}") from e
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    data = response.content
    if response.status_code != 200 or not content_type.startswith("image/") or not data:
        raise ValueError(
            f"Cannot use lead ref (start still) for {shot_id} from {lead_ref}: "
            f"HTTP {response.status_code}, content-type {content_type or 'missing'}, {len(data)} bytes"
        )
    if len(data) > LEAD_REF_MAX_BYTES:
        raise ValueError(f"Lead ref (start still) for {shot_id} is too large: {len(data)} bytes")
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(content_type, ".img")
    out_dir = Path(os.getenv("DATA_DIR", "./data")) / "lead_refs"
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = hashlib.sha256(data).hexdigest()
    out = out_dir / f"{episode_id}-{shot_id}-{sha[:16]}{ext}"
    if not out.exists():
        tmp = out.with_suffix(out.suffix + f".{uuid.uuid4().hex}.part")
        tmp.write_bytes(data)
        tmp.replace(out)
    return str(out), sha


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
    from pathlib import Path
    episodes_dir = Path(os.getenv("EPISODES_DIR", "./data/episodes"))
    shots_json_path = episodes_dir / episode_id / "shots.json"
    
    if not shots_json_path.exists():
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


async def _authorize_paid_retry(ledger: BudgetLedger, episode_id: str, round_id: str, shot_id: str,
                                job_id: str, amount_usd_micros: int, retry_key: str) -> dict:
    """G-BUD-1: authorize one PAID retry job against its review round right before the provider
    submit (per-round 80% stop + max paid retries per shot per round, USD ledger). Refusal is a
    non-retryable ApplicationError(type="RoundBudgetError"); the caller releases its other holds."""
    base_shot = shot_id[:-4] if shot_id.endswith("_end") else shot_id
    try:
        return await ledger.authorize_round_spend(
            episode_id, round_id, base_shot, job_id, amount_usd_micros,
            retry_key=retry_key, reason=f"paid retry {retry_key}: {job_id}",
        )
    except RoundBudgetError as e:
        raise ApplicationError(str(e), type="RoundBudgetError", non_retryable=True) from e


@activity.defn
async def check_round_budget_activity(episode_id: str, round_id: str, shot_id: str, hold_key: str,
                                      model: str, duration: float, retry_key: str) -> dict:
    """Read-only pre-check for EpisodeWorkflowV2.retry_clip (before the retry child starts): would
    this paid clip retry fit the round cap / per-shot retry limit / episode stop? Uses the offline
    rate-table estimate (no provider call). Raises ApplicationError(type="RoundBudgetError")."""
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    ledger = BudgetLedger(db_path)
    amount = clip_estimate_usd_micros(model, None, duration)
    try:
        result = await ledger.authorize_round_spend(
            episode_id, round_id, shot_id, hold_key, amount, retry_key=retry_key,
            reason=f"pre-check {retry_key}", dry_check=True,
        )
    except RoundBudgetError as e:
        raise ApplicationError(str(e), type="RoundBudgetError", non_retryable=True) from e
    return {**result, "estimate_usd_micros": amount}


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
    attempt: int = 1,
    lead_ref: str | None = None,
    moderation_retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Submit still generation job with full enforcement.

    moderation_retry: set by the moderation recovery ladder ({"n": rung number, "step": ...}) when
    a content-blocked still is resubmitted with a neutral reword (the caller passes the reworded
    prompt). The job id gets a deterministic ``-m{n}`` suffix (never colliding with the blocked
    job, -r or -c ids) and, live, the paid resubmit is authorized against the G2 still round with
    retry key ``m{n}`` right before the provider call (G-BUD-1 per-round rule).

    lead_ref: optional reference placed FIRST, e.g. the shot's start still when generating its _end
    frame. Adds -lead<sha8 of the start still> to the job id. When the lead plus anchor plus planned
    refs exceed the still model's cap, the anchor is dropped first (and its -anchor suffix with it);
    if still over the cap the job is refused, never silently trimmed.
    
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
    
    # Load anchor still if configured for this shot
    planned_refs = list(refs or [])
    from hfvg.studio_db import get_anchor_still
    # Strip _end suffix for end-frame stills (A02_end -> A02) to use same anchor
    base_shot_id = shot_id.replace("_end", "") if shot_id.endswith("_end") else shot_id
    anchor = await get_anchor_still(db_path, episode_id, base_shot_id)
    if anchor:
        anchor_path = anchor["local_path"]
        # Verify anchor still file exists and SHA256 matches (module-level Path/hashlib: a local
        # import here would make them function-locals and break the lead-ref path when no anchor)
        anchor_file = Path(anchor_path)
        if not anchor_file.exists():
            raise ValueError(
                f"Anchor still file not found: {anchor_path}"
            )
        
        # Verify SHA256
        sha256_hash = hashlib.sha256()
        with open(anchor_file, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        computed_sha256 = sha256_hash.hexdigest()
        
        if computed_sha256 != anchor["sha256"]:
            raise ValueError(
                f"Anchor still SHA256 mismatch: expected {anchor['sha256']}, got {computed_sha256}"
            )
        
    # Preflight: validate the still against the model's documented limits BEFORE fetching the lead,
    # any upload, estimate or reserve. Same lead/anchor rule as the assembly below, with the actual
    # lead (None for start stills) and the actual anchor. Raises PreflightError (non-retryable).
    from hfvg import preflight, model_caps
    phase = "end" if shot_id.endswith("_end") else "start"
    still_violations = preflight.preflight_shot(
        {"id": shot_id, "start_prompt": prompt, "end_prompt": prompt, "ref_keys": planned_refs,
         "resolution": resolution, "quality": quality, "aspect_ratio": aspect_ratio},
        {"still_model": still_model_path(), "anchor_ref": anchor_path if anchor else None,
         "lead_ref": lead_ref or None},
        model_caps.get_registry(), phase=phase,
    )
    preflight.raise_on_errors(still_violations, f"{episode_id}/{shot_id}")
    for w in still_violations:
        activity.logger.warning(f"Preflight warning: {preflight.format_violation(w)}")

    # _end frames: the shot's own start still leads so the end frame matches it.
    # Never hand the provider its own output link: fetch the start still, then it goes through
    # the same normalize + upload path as every other ref (fails closed before any reserve).
    lead_local, lead_sha = await _localize_lead_ref(lead_ref, episode_id, shot_id) if lead_ref else (None, None)
    lead_sha8 = lead_sha[:8] if lead_sha else None

    # Assemble in priority order: [lead (start still)] + [anchor] + planned refs (the same rule
    # preflight just applied, so the assembled set is within the cap here).
    still_cap = max_refs_for(still_model_path())
    refs_with_anchor, anchor_used = assemble_still_refs(
        lead=lead_local,
        anchor=anchor_path if anchor else None,
        planned=planned_refs,
        cap=still_cap,
    )
    
    # Log decisions
    if lead_local and anchor and not anchor_used:
        activity.logger.info(
            f"Dropped anchor still from {anchor['anchor_shot_id']} for {shot_id}: lead ref (start still) present "
            f"and {len(refs_with_anchor) + 1} refs > {still_model_path()} cap {still_cap}"
        )
    if lead_local:
        activity.logger.info(f"Added lead ref (start still) first for {shot_id}: {lead_ref} -> {lead_local}")
    if anchor_used:
        activity.logger.info(
            f"Added anchor still from {anchor['anchor_shot_id']} (v{anchor['version']}) to {shot_id} refs"
        )

    # Generate job_id (harness identifier)
    # Original shots: still-{ep}-{shot}-v{version}
    # Retries: still-{ep}-{shot}-r{retry_number} (r1, r2, etc.)
    if attempt > 1:
        # Retry: use -r{N} where N is retry number (attempt-1)
        attempt_suffix = f"-r{attempt - 1}"
    else:
        # Original: use -v{version}
        attempt_suffix = f"-v{version}"
    
    # Use sha256 prefix (first 8 chars) for anchor suffix for uniqueness
    # Suffixes describe the refs actually sent: -lead<sha8 of the start still> when a lead ref leads,
    # -anchor<sha8> only if the anchor is still in the set (a dropped anchor changes the id).
    lead_suffix = f"-lead{lead_sha8}" if lead_sha8 else ""
    anchor_suffix = f"-anchor{anchor['sha256'][:8]}" if anchor_used else ""
    job_id = f"still-{episode_id}-{shot_id}{attempt_suffix}{lead_suffix}{anchor_suffix}"
    if moderation_retry:
        job_id += f"-m{int(moderation_retry['n'])}"
    
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
    round_hold_key = None
    
    try:
        # Upload local reference images to Higgsfield and get URLs
        uploaded_refs = []
        if refs_with_anchor:
            for ref_path in refs_with_anchor:
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
        
        model_path = still_model_path()
        
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
        refs_str = ",".join(sorted(uploaded_refs)) if uploaded_refs else ""
        anchor_str = f"anchor:{anchor['sha256']}" if anchor_used else ""
        key_input = f"{model_path}|{prompt}|{resolution}|{quality}|{aspect_ratio or ''}|{refs_str}|{anchor_str}|attempt:{attempt}"
        key_hash = hashlib.sha256(key_input.encode()).hexdigest()[:16]
        idempotency_key = f"{job_id}-{key_hash}"
        
        # G-BUD-1: a paid still RETRY (attempt > 1) must fit its G2 review round, before submit
        if attempt > 1:
            await _authorize_paid_retry(ledger, episode_id, ROUND_G2_STILLS, shot_id, job_id,
                                        budget_amount, f"r{attempt - 1}")
            round_hold_key = job_id
        elif moderation_retry:
            # G-REC-1: a paid moderation resubmit is a paid retry of this shot in its G2 round
            await _authorize_paid_retry(ledger, episode_id, ROUND_G2_STILLS, shot_id, job_id,
                                        budget_amount, f"m{int(moderation_retry['n'])}")
            round_hold_key = job_id

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
        if round_hold_key:
            await ledger.release_round_hold(round_hold_key)
        raise ValueError(error_detail) from e
    except Exception as e:
        # Release whatever has been reserved on any other error
        if budget_reserved:
            await ledger.release(episode_id, budget_line_name, budget_amount, f"Submit failed: {str(e)}")
        if credits_reserved:
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Submit failed: {str(e)}")
        if round_hold_key:
            await ledger.release_round_hold(round_hold_key)
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
    clip_retry: dict[str, Any] | None = None,
    moderation_retry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Submit a video generation job (Kling 3.0 Pro or Seedance 2.5) with full enforcement.

    moderation_retry: set by the moderation recovery ladder ({"n": rung number, "step":
        "reword"|"fallback"}) when a content-blocked clip is resubmitted (the caller passes the
        reworded prompt / fallback model). Adds a deterministic ``-m{n}`` job id suffix and, live,
        authorizes the paid resubmit against the G3 clip round with retry key ``m{n}`` before the
        provider call. (Not combined with clip_retry's authorization: one paid retry, one hold.)

    clip_retry: set by EpisodeWorkflowV2.retry_clip ({"n": retry number, ...}). The job id gets a
        deterministic ``-c{n}`` suffix (idempotent per retry, never colliding with the original
        clip) and, live, the paid submit is authorized against the G3 clip round first (G-BUD-1).
    
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
    # Clip lint runs on the FINAL submitted clip prompt (clip_prompt or reword), with the motion rules
    enforce_prompt_lint(episode_id, prompt, shot_meta, live=use_live, kind="clip")
    
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

        # Preflight: validate the clip against the model's documented limits BEFORE any upload,
        # estimate or reserve. The prompt parameter is the FINAL clip prompt exactly as submitted:
        # the shot's clip_prompt (which REPLACES the still prompt), the still prompt when there is no
        # clip_prompt, or a moderation reword of it. So the model limit (Kling: 2500 chars) is checked
        # on the string the provider receives.
        # Raises PreflightError (non-retryable).
        from hfvg import preflight, model_caps
        clip_violations = preflight.preflight_shot(
            {"id": shot_id, "clip_prompt": prompt, "duration": duration,
             "start_frame": start_image_url or None, "end_frame": end_image_url or None},
            {"clip_model": preflight.clip_model_path(route.model)},
            model_caps.get_registry(), phase="clip",
        )
        preflight.raise_on_errors(clip_violations, f"{episode_id}/{shot_id} clip")
        for w in clip_violations:
            activity.logger.warning(f"Preflight warning: {preflight.format_violation(w)}")

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
    if clip_retry:
        job_id += f"-c{int(clip_retry['n'])}"
    if moderation_retry:
        job_id += f"-m{int(moderation_retry['n'])}"
    
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
        
        # G-BUD-1: a paid clip RETRY must fit the G3 clip round (cap + per-shot limit), before submit
        round_hold_key = None
        paid_retry_key = None
        if moderation_retry:
            paid_retry_key = f"m{int(moderation_retry['n'])}"
        elif clip_retry:
            paid_retry_key = f"c{int(clip_retry['n'])}"
        if paid_retry_key:
            try:
                await _authorize_paid_retry(ledger, episode_id, ROUND_G3_CLIPS, shot_id, job_id,
                                            estimated_cost, paid_retry_key)
            except ApplicationError:
                await ledger.release(episode_id, line_name, estimated_cost, "Round budget refused")
                await credit_ledger.release_credits(episode_id, job_id, notes="Round budget refused")
                raise
            round_hold_key = job_id

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
            if round_hold_key:
                await ledger.release_round_hold(round_hold_key)
            raise ValueError(error_detail) from e
        except Exception as e:
            # Release both reservations on any other error
            await ledger.release(episode_id, line_name, estimated_cost, f"Submit failed: {str(e)}")
            await credit_ledger.release_credits(episode_id, job_id, notes=f"Submit failed: {str(e)}")
            if round_hold_key:
                await ledger.release_round_hold(round_hold_key)
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
                episode_id, job_id, shot_id, provider.model_path, "retry" if clip_retry else tier,
                line_name, estimated_cost,
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
async def resolve_landed_start_still(episode_id: str, shot_id: str, attempt: int, version: int) -> dict[str, Any]:
    """Find the start still that already LANDED for this shot attempt, without generating anything.

    Used by EpisodeWorkflowV2.retry_end_still to backfill only the _end still. Free and read-only:
    job_ledger (committed start job for exactly this attempt) -> provider_jobs (provider id) ->
    GET /requests/{id}/status (no charge) -> images[0].url. Refuses (non-retryable) when the start
    did not land, is ambiguous, or an _end with a lead ref was already submitted for this attempt
    (so the backfill can never become a silent paid retry).
    """
    db_path = os.getenv("DATABASE_PATH", "./data/studio.db")
    suffix = f"-r{attempt - 1}" if attempt > 1 else f"-v{version}"
    start_prefix = f"still-{episode_id}-{shot_id}{suffix}"
    end_lead_prefix = f"still-{episode_id}-{shot_id}_end{suffix}-lead"

    def refuse(msg: str):
        raise ApplicationError(msg, type="EndStillBackfillRefused", non_retryable=True)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT job_id, status FROM job_ledger WHERE episode_id = ? AND shot_id = ?", (episode_id, shot_id)
        ) as cur:
            rows = [r for r in await cur.fetchall()
                    if r[0] == start_prefix or r[0].startswith(start_prefix + "-")]
        async with db.execute("SELECT job_id FROM provider_jobs") as cur:
            prior_end = [r[0] for r in await cur.fetchall()
                         if r[0].startswith(end_lead_prefix)]
    if prior_end:
        refuse(f"{shot_id}_end was already submitted with a lead ref for this attempt ({prior_end}); "
               "refusing to resubmit (no retries)")
    committed = [r[0] for r in rows if r[1] == "committed"]
    if len(committed) != 1:
        refuse(f"Need exactly one committed start still {start_prefix}* for {episode_id}/{shot_id}, "
               f"found {rows}")
    start_job_id = committed[0]
    credit_ledger = CreditLedger(db_path)
    provider_job_id = await credit_ledger.get_provider_job(start_job_id)
    if not provider_job_id:
        refuse(f"No provider job mapping for landed start still {start_job_id}")
    provider = HiggsfieldStillProvider()
    try:
        job = await provider.get_job_status(provider_job_id)   # GET status: free
    finally:
        await provider.close()
    if job.status != ProviderJobStatus.COMPLETED or not job.output_url:
        refuse(f"Start still {start_job_id} ({provider_job_id}) is not completed with an image: "
               f"status={job.status} error={job.error}")
    activity.logger.info(
        f"Resolved landed start still for {episode_id}/{shot_id}: {start_job_id} -> {provider_job_id} -> {job.output_url}"
    )
    return {"url": job.output_url, "job_id": start_job_id, "provider_job_id": provider_job_id}


def failure_class(status: str) -> str | None:
    """Classify a terminal provider status. ``content_block`` (provider nsfw/blocked) is the only
    class that enters the moderation recovery ladder; ``canceled`` and ``provider_failure`` never do."""
    return {"blocked": "content_block", "canceled": "canceled", "failed": "provider_failure"}.get(status)


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
            raw = getattr(job_status, "raw", None)
            hdrs = getattr(job_status, "response_headers", None) or {}
            cid = hdrs.get("x-correlation-id")
            activity.logger.error(
                f"Job {provider_job_id} (harness {job_id}) {job_status.status.value}: {job_status.error} "
                f"| raw status body: {json.dumps(raw)} | response headers: {json.dumps(hdrs)}"
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
            error = job_status.error or f"Job {job_status.status.value}"
            if cid:
                error = f"{error} (x-correlation-id {cid})"
            return {
                "status": job_status.status.value,  # "failed", "blocked", or "canceled"
                "output_url": None,
                "error": error,
                "cost": None,
                "provider_raw": raw,
                "provider_headers": hdrs,
                # Distinct classification: only a moderation block enters the recovery ladder
                "failure_class": failure_class(job_status.status.value),
                # The provider's exact message (unmodified), for audit + human review
                "provider_message": job_status.error,
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
    # In dry mode submit never reserved (mirror of commit_job_budget): nothing to release
    if os.getenv("DRY_RUN", "true").lower() == "true":
        activity.logger.info(f"[DRY-RUN] Skipping release for {job_type} {shot_id} (no reservation was made)")
        return
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
        # Phase 1: Active polling (injected interval for tests, default 5s, up to 500s ~100 polls)
        max_active_polls = 100
        active_poll_interval = float(os.getenv("ACTIVE_POLL_INTERVAL_SECONDS", "5.0"))
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
            
            elif job_status.status.value in ("failed", "blocked", "canceled"):
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
                
                hdrs = getattr(job_status, "response_headers", None) or {}
                activity.logger.error(
                    f"Job {job_id} {job_status.status.value}: {job_status.error} "
                    f"| raw status body: {json.dumps(getattr(job_status, 'raw', None))} "
                    f"| response headers: {json.dumps(hdrs)}"
                )
                
                raise ValueError(
                    f"Generation {job_status.status.value}: {job_status.error}"
                    + (f" (x-correlation-id {hdrs['x-correlation-id']})" if hdrs.get("x-correlation-id") else "")
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
