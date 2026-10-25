"""EpisodeWorkflow V2: Handbook-compliant workflow with steps 0-7 and project owner approval points."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from hfvg import budget
    from hfvg.config import config  # Non-deterministic module with os.getenv
    from hfvg.activities import (
        generate_music,
        generate_sfx,
        generate_voiceover,
        load_gate_policy_activity,
        mix_audio,
        parse_beatmap_activity,
        recheck_refs_activity,
        render_edit,
        trim_clips,
    )
    from hfvg.activities.media import assemble_mute_edit
    from hfvg.activities.qc import check_assembly_qc
    from hfvg.activities.audio import check_delivery_qc, load_audio_plan, mix_and_deliver
    from hfvg.activities.credit_activities import update_credit_cap_activity
    from hfvg.activities.studio_generation import check_live_mode_and_g108
    from hfvg.activities.g1_plan import compute_g1_plan_activity
    from hfvg.studio_db import list_anchor_stills, approve_g2_shots
    from hfvg.models import EpisodeState, PipelineStage
    from hfvg.workflows.posting import PostingWorkflow
    from hfvg.episode_parser import build_prompt_from_shot
    # ShotWorkflow is referenced by name string, not imported


@workflow.defn
class EpisodeWorkflowV2:
    """
    Episode workflow following project handbook v1.1.
    
    Steps 0-7 with 15 project owner approval points:
    0. Bible (asset registry, playbook)
    1. Story (pitch pick, beatmap, credit plan)
    2. Stills (refs, drafts, finals with character locks)
    3. Video (clips with duck identity gate)
    4. Mute (trim, QC, picture lock)
    5. Script (VO script lock)
    6. Audio (VO, music, SFX, mix, final approval)
    7. Delivery (encode, Drive, Grower handoff - HOLD by default)
    """
    
    def __init__(self):
        self.state = EpisodeState(
            episode_id="",
            stage=PipelineStage.INFO_GATHERING,
            idea="",
        )
        self.policy = None
        self.passed_gates: list[str] = []  # Track which gates have been passed
        
        # Three human gates (collapsed from 15)
        self.approved_g1_shot_plan = False  # G1: Beatmap + credit plan
        self.approved_g2_scene_stills = False  # G2: All stills + refs
        self.approved_g_final_cut = False  # G_FINAL: After edit
        
        self.shot_results: dict[str, dict] = {}
        self.parsed_beatmap: dict[str, Any] | None = None
        self.missing_refs: dict[str, list[str]] = {}  # shot_id -> refs not on disk
        self.end_backfills: dict[str, int] = {}  # shot_id -> retry_end_still children started
        self.background_tasks: list = []
        # G-GATE-1 / G-RETRY-1 / G-BUD-1 state
        self.rejection_reasons: dict[str, str] = {}  # shot_id -> reason (G2 reject_shot)
        self.launched_shot_plans: dict[str, dict] = {}  # shot_id -> plan of the latest still child
        self.shot_outcomes: dict[str, dict] = {}  # shot_id -> result of the latest finished child
        self.assembly_shots: list[dict] = []  # shot plans in edit order (set when clips are done)
        self.assembly_sig: list | None = None  # clip outcomes the current mute edit was built from
        self.assembly_version: int = 0
        self.clip_retries: dict[str, int] = {}  # shot_id -> paid clip retries started (G3 round)
        self.pending_clip_retries: dict[str, str] = {}  # shot_id -> clip-retry workflow id
        self.clip_retry_log: list[dict] = []  # audit: one row per clip retry started
        self.final_cut_refusals: list[str] = []  # final-cut approvals ignored (clip retry pending)
        self.final_cut_rejections: list[dict] = []  # G_FINAL_CUT reviewer rejections (reason, versions)
        self.delivery_sig: list | None = None  # picture (edit version + path) the delivery was built from
        self.delivery_version: int = 0
        self.delivery_phase: str | None = None  # audio / final_mix / delivery / done
    
    @workflow.run
    async def run(
        self, 
        episode_id: str, 
        beatmap_path: str | None = None, 
        dry_run: bool = True,
        shots_subset: list[str] | None = None,
        credit_cap: int | None = None,
        attempt_overrides: dict[str, int] | None = None
    ) -> dict:
        """
        Execute full episode pipeline following handbook steps.
        
        Args:
            episode_id: Episode identifier (e.g., 'ep04')
            beatmap_path: Path to BEATMAP.md (optional, uses fixture if None)
            dry_run: If True, use DRY_RUN mode (default True for testing)
            shots_subset: Optional list of shot IDs to run (e.g., ['A01', 'A02'] for canary)
            credit_cap: Optional credit cap for this run (overrides episode line cap)
            attempt_overrides: Optional shot_id -> attempt number mapping for testing retries (e.g., {'A02': 2})
        
        Returns:
            dict with episode results
        """
        self.state.episode_id = episode_id
        
        # Store attempt overrides in state (these don't count against retry budget)
        if attempt_overrides:
            for shot_id, attempt in attempt_overrides.items():
                self.state.shot_attempt_overrides[shot_id] = attempt
        
        # In live mode, check for required files and Higgsfield balance before acquiring lock
        if not dry_run:
            workflow.logger.info(f"Verifying required files in live mode...")
            try:
                await workflow.execute_activity(
                    "verify_episode_files_activity",
                    args=[episode_id],
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
            except Exception as e:
                workflow.logger.error(f"File verification failed: {e}")
                return {
                    "episode_id": episode_id,
                    "status": "failed",
                    "error": f"Missing required files in live mode: {str(e)}",
                }
            
            # Check Higgsfield balance
            workflow.logger.info(f"Checking Higgsfield balance...")
            try:
                balance_info = await workflow.execute_activity(
                    "check_higgsfield_balance_activity",
                    args=[episode_id],
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                workflow.logger.info(f"Balance check passed: ${balance_info['balance_usd']:.2f} USD")
            except Exception as e:
                workflow.logger.error(f"Balance check failed: {e}")
                return {
                    "episode_id": episode_id,
                    "status": "failed",
                    "error": f"Higgsfield balance check failed: {str(e)}",
                }
        
        # Acquire account-level run lock
        workflow.logger.info(f"Acquiring run lock for {episode_id}...")
        try:
            await workflow.execute_activity(
                "acquire_run_lock",
                args=[episode_id, workflow.info().workflow_id, "default"],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as e:
            workflow.logger.error(f"Failed to acquire run lock: {e}")
            return {
                "episode_id": episode_id,
                "status": "failed",
                "error": f"Run lock acquisition failed: {str(e)}",
            }
        
        try:
            result = await self._run_with_lock(
                episode_id, beatmap_path, dry_run, False, shots_subset, credit_cap
            )
            return result
        finally:
            # Always release lock on exit
            workflow.logger.info(f"Releasing run lock for {episode_id}...")
            try:
                await workflow.execute_activity(
                    "release_run_lock",
                    args=[episode_id],
                    start_to_close_timeout=timedelta(seconds=10),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception as e:
                workflow.logger.error(f"Failed to release run lock: {e}")
    
    async def _run_with_lock(
        self, episode_id: str, beatmap_path: str | None, dry_run: bool,
        is_canary: bool, shots_subset: list[str] | None, credit_cap: int | None
    ) -> dict:
        """Execute workflow with run lock held."""
        
        # Upsert episode row (sets live_mode = not dry_run)
        workflow.logger.info(f"Upserting episode {episode_id} (live_mode={not dry_run})")
        await workflow.execute_activity(
            "upsert_episode_activity",
            args=[episode_id, beatmap_path, dry_run],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        
        # Initialize credit ledger line with credit_cap (lower of credit_cap and env cap)
        workflow.logger.info(f"Initializing credit ledger for {episode_id} with cap: {credit_cap}")
        await workflow.execute_activity(
            "init_episode_credit_line_activity",
            args=[episode_id, credit_cap],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        
        # Initialize budget ledger lines (L1/L2/L3/L4/L7) from gate policy
        workflow.logger.info(f"Initializing budget lines for {episode_id}")
        budget_result = await workflow.execute_activity(
            "init_episode_budget_lines_activity",
            args=[episode_id],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        workflow.logger.info(f"Budget lines created: {budget_result.get('lines', [])}")
        
        # Verify all required budget lines exist (fail clearly if missing)
        from hfvg.budget import REQUIRED_BUDGET_LINES
        created_lines = set(budget_result.get('lines', []))
        missing_lines = [line for line in REQUIRED_BUDGET_LINES if line not in created_lines]
        if missing_lines:
            raise ValueError(
                f"Budget initialization failed: missing required lines {missing_lines}. "
                f"Created: {list(created_lines)}"
            )
        
        # Load gate policy via activity (not deterministic in workflow)
        policy_data = await workflow.execute_activity(
            load_gate_policy_activity,
            args=[],
            start_to_close_timeout=timedelta(seconds=10),
        )
        # Store policy data as dict (we only need the data, not GatePolicy methods)
        self.policy = policy_data
        
        workflow.logger.info(f"Starting episode {episode_id} (dry_run={dry_run})")
        
        # ================================================================
        # GATE 1: SHOT PLAN (Beatmap + Credit Plan)
        # ================================================================
        workflow.logger.info("=== GATE 1: SHOT PLAN ===")
        self.state.stage = PipelineStage.SCRIPT
        
        # Parse beatmap via activity (filesystem I/O not allowed in workflow)
        workflow.logger.info("Parsing BEATMAP.md...")
        
        self.parsed_beatmap = await workflow.execute_activity(
            parse_beatmap_activity,
            args=[beatmap_path, episode_id, not dry_run],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        
        shots = self.parsed_beatmap.get("shots", [])
        workflow.logger.info(f"Parsed {len(shots)} shots from beatmap")
        
        # Filter shots by shots_subset if provided
        if shots_subset:
            # Validate all subset IDs exist
            shot_ids = {s["shot_id"] for s in shots}
            unknown_ids = [sid for sid in shots_subset if sid not in shot_ids]
            if unknown_ids:
                raise ValueError(f"Unknown shot IDs in subset: {unknown_ids}")
            
            # Filter to subset
            shots = [s for s in shots if s["shot_id"] in shots_subset]
            workflow.logger.info(f"Filtered to shots_subset: {shots_subset} ({len(shots)} shots)")
        
        # Preflight validation: check ALL inputs before any spend
        workflow.logger.info("Running preflight validation (all inputs)...")
        await workflow.execute_activity(
            "preflight_validate_episode_activity",
            args=[episode_id, shots_subset, not dry_run],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        workflow.logger.info("Preflight validation passed")
        
        # Compute G1 credit plan (per-shot and total estimates)
        workflow.logger.info("Computing G1 credit plan from provider estimates...")
        self.state.g1_plan = await workflow.execute_activity(
            compute_g1_plan_activity,
            args=[episode_id, shots, dry_run],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        workflow.logger.info(
            f"G1 plan computed: {self.state.g1_plan['total_credits']} credits, "
            f"${self.state.g1_plan['total_usd']}"
        )
        if self.state.g1_plan.get("preflight_errors"):
            # An approval sent before the plan existed never covers a plan that breaks model limits
            self.approved_g1_shot_plan = False
            workflow.logger.warning(
                f"G1 preflight: {len(self.state.g1_plan['preflight_errors'])} model-limit error(s); "
                "G1 approval is refused until the shots are fixed"
            )
        
        # Calculate total credits from brief (no separate CREDIT-PLAN needed)
        total_video_credits = sum(s.get("video_credits", 0) for s in shots)
        total_still_credits = sum(s.get("still_credits", 0) for s in shots)
        total_credits = total_video_credits + total_still_credits
        
        workflow.logger.info(
            f"Credit plan: {total_credits} credits estimated "
            f"(video: {total_video_credits}, stills: {total_still_credits})"
        )
        
        # Wait for G1_SHOT_PLAN approval
        workflow.logger.info("G1_SHOT_PLAN: Awaiting approval...")
        self.state.current_gate = "G1_SHOT_PLAN"
        await workflow.wait_condition(lambda: self.approved_g1_shot_plan)
        self.passed_gates.append("G1_SHOT_PLAN")
        workflow.logger.info(f"[APPROVED G1] Shot plan locked: {len(shots)} shots")
        
        # G1 approval implies G1.08 credit plan approval (single source of truth)
        if not dry_run:
            workflow.logger.info("Approving G1.08 credit plan (derived from G1_SHOT_PLAN approval)")
            await workflow.execute_activity(
                "approve_g108_activity",
                args=[episode_id],
                start_to_close_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        
        # ================================================================
        # Generate Reference Images (Character Sheets, Lineup, etc.)
        # ================================================================
        workflow.logger.info("=== REFERENCE GENERATION ===")
        
        # Load refs to generate from refs.json, filtered by shots_subset
        # In live mode, this will fail if refs.json is missing or needed refs unavailable
        refs_to_generate = await workflow.execute_activity(
            "load_refs_to_generate_activity",
            args=[episode_id, shots_subset, not dry_run],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        
        if refs_to_generate:
            workflow.logger.info(f"Generating {len(refs_to_generate)} reference images...")
            
            for ref_spec in refs_to_generate:
                ref_key = ref_spec["key"]
                prompt = ref_spec["prompt"]
                output_path = ref_spec["output_path"]
                version = ref_spec.get("version", 1)
                
                workflow.logger.info(f"Generating ref {ref_key} v{version}")
                
                ref_result = await workflow.execute_activity(
                    "generate_ref_activity",
                    args=[episode_id, ref_key, prompt, output_path, version],
                    start_to_close_timeout=timedelta(minutes=5),
                    retry_policy=RetryPolicy(
                        maximum_attempts=2,
                        non_retryable_error_types=["QCFailedError"],  # QC failures surface to gate
                    ),
                )
                
                workflow.logger.info(
                    f"Ref {ref_key} completed: {ref_result['status']}, "
                    f"credits: {ref_result['credits_used']}"
                )
        else:
            workflow.logger.info("No refs to generate")
        
        # ================================================================
        # Generate Stills (before G2)
        # ================================================================
        workflow.logger.info("=== STILLS GENERATION ===")
        self.state.stage = PipelineStage.STILL_GENERATION
        
        # Check for missing refs (internal check, not a human gate)
        if self._needs_new_refs(shots):
            self.missing_refs = self._collect_missing_refs(shots)
            workflow.logger.info(f"Missing refs detected: {self.missing_refs}")
            
            # In live mode, re-check refs after they're uploaded
            if not dry_run:
                rechecked = await workflow.execute_activity(
                    recheck_refs_activity,
                    args=[episode_id, {s["shot_id"]: s.get("ref_keys", []) for s in shots}],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )
                shots = [
                    {**s, **rechecked[s["shot_id"]]} if s["shot_id"] in rechecked else s
                    for s in shots
                ]
                self.missing_refs = self._collect_missing_refs(shots)
        
        # Get list of pre-approved stills (anchor stills) for shots
        anchor_stills = await workflow.execute_activity(
            list_anchor_stills,
            args=[episode_id],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        # Map: shot_id -> pre-approved still info
        pre_approved_stills = {a["target_shot_id"]: a for a in anchor_stills}
        if pre_approved_stills:
            workflow.logger.info(f"Pre-approved stills for {len(pre_approved_stills)} shots: {sorted(pre_approved_stills.keys())}")
        
        # Launch shot workflows for ALL shots (including pre-approved)
        workflow.logger.info(f"Launching shot generation for {len(shots)} shots ({len(pre_approved_stills)} with pre-approved stills)...")
        shot_workflow_handles = []
        shot_completion_status = {}  # Track completion: {shot_id: {"done": bool, "success": bool, "error": str?}}
        
        # Track background tasks that monitor child completion (store as instance var for retry access)
        import asyncio
        self.background_tasks = []
        
        async def track_child_completion(shot_id: str, handle, is_retry: bool = False):
            """Background task to await child and record result in state."""
            try:
                result = await handle
                self.shot_outcomes[shot_id] = result if isinstance(result, dict) else {"status": "unknown"}
                # Clear pending_retries if this was a retry child
                if is_retry and shot_id in self.state.pending_retries:
                    self.state.pending_retries.pop(shot_id, None)
                    workflow.logger.info(f"Retry child {shot_id} completed, cleared from pending_retries")
                
                # Check if result indicates failure (Issue B: shot returns failed dict instead of raising)
                if isinstance(result, dict) and result.get("status") in ("failed", "blocked"):
                    # "blocked" = content block escalated to human review (G-REC-1): no asset landed
                    error_detail = result.get("error", "Unknown error")
                    shot_completion_status[shot_id] = {"done": True, "success": False, "error": error_detail}
                    self.state.failed_shots.append({
                        "shot_id": shot_id,
                        "status": "needs_review" if result.get("needs_review") else "failed",
                        "reason": error_detail
                    })
                    workflow.logger.error(f"Shot {shot_id} failed: {error_detail}")
                else:
                    shot_completion_status[shot_id] = {"done": True, "success": True, "result": result}
                    workflow.logger.info(f"Shot {shot_id} completed successfully")
            except Exception as e:
                # Child raised an exception
                error_detail = str(e)
                self.shot_outcomes[shot_id] = {"status": "failed", "error": error_detail}
                
                # Clear pending_retries if this was a retry child
                if is_retry and shot_id in self.state.pending_retries:
                    self.state.pending_retries.pop(shot_id, None)
                    workflow.logger.info(f"Retry child {shot_id} failed with exception, cleared from pending_retries")
                
                shot_completion_status[shot_id] = {"done": True, "success": False, "error": error_detail}
                workflow.logger.error(f"Shot {shot_id} failed: {error_detail}")
                # Record in state for API/query exposure
                self.state.failed_shots.append({
                    "shot_id": shot_id,
                    "status": "failed",
                    "reason": error_detail,
                })
        
        for shot in shots:
            shot_plan = shot.copy()
            shot_plan["dry_run"] = dry_run
            if "prompt" not in shot_plan:
                shot_plan["prompt"] = build_prompt_from_shot(shot)
            
            # Apply attempt override if provided (for testing retry behavior)
            shot_id = shot["shot_id"]
            override = self.state.shot_attempt_overrides.get(shot_id, 0)
            
            # Initialize shot_attempts: seed with override if present, else 1 for first-time shots
            if shot_id not in self.state.shot_attempts:
                if override > 0:
                    # Override seeds shot_attempts so retry limits are enforced correctly
                    self.state.shot_attempts[shot_id] = override
                else:
                    self.state.shot_attempts[shot_id] = 1
            
            actual_attempts = self.state.shot_attempts[shot_id]
            
            # Total attempt number for job_id/version
            total_attempt = actual_attempts
            shot_plan["attempt"] = total_attempt
            shot_plan["version"] = total_attempt
            
            # Check if this shot has a pre-approved still
            if shot_id in pre_approved_stills:
                pre_approved = pre_approved_stills[shot_id]
                shot_plan["pre_approved_still"] = {
                    "anchor_shot_id": pre_approved["anchor_shot_id"],
                    "local_path": pre_approved["local_path"],
                    "version": pre_approved["version"],
                    "sha256": pre_approved.get("sha256"),
                }
                workflow.logger.info(
                    f"Starting {shot_id} with pre-approved still from {pre_approved['anchor_shot_id']} "
                    f"(attempt={total_attempt}, still spend skipped)"
                )
            else:
                workflow.logger.info(
                    f"Starting {shot_id} with attempt={total_attempt} (shot_attempts={actual_attempts}, override={override})"
                )
            
            # Add parent workflow ID to shot_plan for retry_still_ready signaling
            shot_plan["parent_workflow_id"] = workflow.info().workflow_id
            
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[episode_id, shot_plan],
                id=f"{episode_id}-shot-{shot['shot_id']}",
                task_queue=workflow.info().task_queue,
            )
            self.launched_shot_plans[shot_id] = shot_plan
            shot_workflow_handles.append(handle)
            self.shot_results[shot["shot_id"]] = handle
            
            # Start background task to track completion (is_retry=False for original shots)
            task = asyncio.create_task(track_child_completion(shot["shot_id"], handle, is_retry=False))
            self.background_tasks.append(task)
        
        workflow.logger.info(f"Launched {len(shot_workflow_handles)} shot workflows with background tracking")
        
        # ================================================================
        # GATE 2: SCENE STILLS (All stills + reference sheets)
        # ================================================================
        workflow.logger.info("=== GATE 2: SCENE STILLS ===")
        self.state.current_gate = "G2_SCENE_STILLS"
        
        # Wait for approval OR any child failure
        # Note: This wait returns quickly when any shot fails
        await workflow.wait_condition(
            lambda: self.approved_g2_scene_stills or
            any(status.get("success") == False for status in shot_completion_status.values())
        )
        
        # If we exited because of failures (not approval), surface them
        if not self.approved_g2_scene_stills and len(self.state.failed_shots) > 0:
            failure_msg = f"Gate 2: {len(self.state.failed_shots)} shot(s) failed (approval still required):\n" + "\n".join(
                f"  - {s['shot_id']}: {s['reason']}" for s in self.state.failed_shots
            )
            workflow.logger.error(failure_msg)
        
        # Track shots waiting on hold (succeeded stills but waiting for G2 approval before clips)
        shots_on_hold = []
        for shot in shots:
            shot_id = shot["shot_id"]
            completion = shot_completion_status.get(shot_id)
            if completion and completion.get("done") and completion.get("success"):
                shots_on_hold.append(shot_id)
        
        self.state.shots_on_hold = shots_on_hold
        workflow.logger.info(f"Gate 2: {len(shots_on_hold)} shot(s) waiting on hold for approval")
        
        # Wait for G2 approval (background tasks will clear pending_retries automatically)
        await workflow.wait_condition(lambda: self.approved_g2_scene_stills)
        
        self.passed_gates.append("G2_SCENE_STILLS")
        workflow.logger.info("[APPROVED G2] Scene stills approved")
        
        # Mark the kept shots as approved in the database (G-GATE-1: rejected shots excluded)
        rejected_ids = sorted(
            sid for sid, decision in self.state.shot_decisions.items() if decision == "rejected"
        )
        approved_count = await workflow.execute_activity(
            approve_g2_shots,
            args=[episode_id] + ([rejected_ids] if rejected_ids else []),
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        workflow.logger.info(f"Marked {approved_count} shot(s) as approved after G2")
        
        # Clear hold state after approval (shots will be released to clips)
        self.state.shots_on_hold = []
        
        # ================================================================
        # Generate Clips
        # ================================================================
        workflow.logger.info("=== CLIP GENERATION ===")
        self.state.stage = PipelineStage.CLIP_GENERATION
        
        # Signal all shot workflows to proceed with clips - except REJECTED stills, which get
        # still_rejected and end without a clip (G-GATE-1: never animated, never charged).
        for shot in shots:
            handle = self.shot_results.get(shot["shot_id"])
            if handle:
                try:
                    if self.state.shot_decisions.get(shot["shot_id"]) != "rejected":
                        await handle.signal("stills_approved")
                    else:
                        await handle.signal(
                            "still_rejected", self.rejection_reasons.get(shot["shot_id"], "rejected at G2")
                        )
                except ApplicationError as e:
                    # Bug #1: If a shot already finished (failed, not retried), signaling it
                    # from inside the workflow fails with ApplicationError of type
                    # "ExternalWorkflowExecutionNotFound" (there is no RPCError in workflow
                    # code). This is normal - just log it. Re-raise anything else.
                    if e.type != "ExternalWorkflowExecutionNotFound":
                        raise
                    workflow.logger.info(
                        f"Could not signal {shot['shot_id']} (already finished): {e}"
                    )
        
        # Safety design: G2 approval validator now refuses while retries are pending,
        # so pending_retries is guaranteed empty here. No 30s timeout wait needed.
        
        workflow.logger.info("Awaiting all clip completions...")
        results = []
        # Use shot_results to get latest handle (includes retries)
        for shot in shots:
            shot_id = shot["shot_id"]
            
            # Rejected shots were sent still_rejected (no clip): no clip result to collect
            if self.state.shot_decisions.get(shot_id) == "rejected":
                workflow.logger.info(f"Skipping clip generation for rejected shot {shot_id}")
                continue
            
            handle = self.shot_results.get(shot["shot_id"])
            if not handle:
                continue
            # Process credit cap raise before each handle check
            await self._process_cap_raise()
            
            result = await handle
            results.append(result)
        
        workflow.logger.info(f"All {len(results)} clips completed")
        
        # ================================================================
        # Picture Assembly (Round 3): mute 9:16 edit of the approved clips + QC
        # ================================================================
        workflow.logger.info("=== PICTURE ASSEMBLY ===")
        self.assembly_shots = list(shots)
        await self._run_picture_assembly(episode_id)
        # Audio + delivery (Round 4): mix under the locked mute picture, deliverables + delivery QC
        if self._delivery_due():
            await self._run_delivery(episode_id)
        
        # ================================================================
        # GATE 3: FINAL CUT
        # ================================================================
        workflow.logger.info("=== GATE 3: FINAL CUT ===")
        # Clip retries (G-RETRY-1) may run while parked here; approval only counts once none is pending,
        # the mute edit is up to date with the latest clips (a landed retry rebuilds it) and its QC is
        # not blocking (FAIL / NEEDS_REVIEW hold the gate until a passing rebuild or a human accept).
        self.state.current_gate = "G_FINAL_CUT"
        while True:
            await workflow.wait_condition(
                lambda: self.state.new_credit_cap > 0 or (not self.pending_clip_retries and (
                    self._assembly_stale() or self._delivery_due() or (
                        self.approved_g_final_cut and not self._assembly_blocking()
                        and not self._delivery_blocking())
                ))
            )
            if self.state.new_credit_cap > 0:
                # A clip retry parked at the credit cap must be resumable here too (else the gate deadlocks)
                await self._process_cap_raise()
                continue
            if self._assembly_stale():
                workflow.logger.info("[PICTURE LOCK] clips changed since the last edit; re-assembling")
                await self._run_picture_assembly(episode_id)
                continue
            if self._delivery_due():
                workflow.logger.info("[DELIVERY] picture changed or was accepted; rebuilding the delivery")
                await self._run_delivery(episode_id)
                continue
            break
        self.passed_gates.append("G_FINAL_CUT")
        workflow.logger.info("[APPROVED G_FINAL] Final cut approved")
        
        # ================================================================
        # Generate Run Report
        # ================================================================
        workflow.logger.info("=== GENERATING RUN REPORT ===")
        end_time = workflow.now()
        
        # Calculate workflow duration
        from datetime import datetime
        start_time_dt = datetime.fromisoformat(workflow.info().start_time.isoformat())
        end_time_dt = datetime.fromisoformat(end_time.isoformat())
        
        # Collect stage info
        stages = [
            {"stage_name": "SCRIPT", "status": "complete"},
            {"stage_name": "STILL_GENERATION", "status": "complete"},
            {"stage_name": "CLIP_GENERATION", "status": "complete"},
            {"stage_name": "MUTE_EDIT", "status": "complete"},
            {"stage_name": "DELIVERY", "status": "complete",
             "verdict": (self.state.delivery_qc or {}).get("verdict")},
        ]
        
        try:
            report_result = await workflow.execute_activity(
                "generate_run_report",
                args=[
                    episode_id,
                    False,  # is_canary
                    start_time_dt.isoformat(),
                    end_time_dt.isoformat(),
                    None,  # credits_spent: read from the credit ledger by the activity (real value)
                    None,  # credits_cap: read from the credit ledger by the activity (real value)
                    False,  # paused_for_cap
                    len(shots),  # shots_count
                    stages,
                    "complete",  # status
                    None,  # error
                    {
                        "clip_retries": dict(self.clip_retries),
                        "clip_retry_log": [dict(r) for r in self.clip_retry_log],
                        "still_attempts": dict(self.state.shot_attempts),
                        "rejected_at_g2": sorted(
                            s for s, d in self.state.shot_decisions.items() if d == "rejected"
                        ),
                        "max_paid_retries_per_shot_per_round": config.MAX_PAID_RETRIES_PER_SHOT_PER_ROUND,
                    },
                ],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
            workflow.logger.info(f"Run report saved: {report_result}")
        except Exception as e:
            workflow.logger.warning(f"Failed to generate run report: {e}")
        
        workflow.logger.info(f"✅ Episode {episode_id} complete")
        
        return {
            "episode_id": episode_id,
            "shots": len(shots),
            "status": "complete",
            "passed_gates": self.passed_gates,
            "approvals": {
                "g1_shot_plan": self.approved_g1_shot_plan,
                "g2_scene_stills": self.approved_g2_scene_stills,
                "g_final_cut": self.approved_g_final_cut,
            },
        }
    
    @staticmethod
    def _collect_missing_refs(shots: list[dict]) -> dict[str, list[str]]:
        return {s["shot_id"]: list(s["missing_refs"]) for s in shots if s.get("missing_refs")}

    def _needs_new_refs(self, shots: list[dict]) -> bool:
        """G2.01 is needed when any shot references a ref that is not on disk (planned new ref)."""
        return bool(self._collect_missing_refs(shots))
    
    # Update handlers for 3 gates
    @workflow.update
    async def approve_g1_shot_plan(self, override_unavailable_estimates: bool = False) -> dict:
        """Approve shot plan (G1_SHOT_PLAN): Beatmap + credit plan.
        
        Args:
            override_unavailable_estimates: If True, allow approval even when estimates_available=False in live mode
            
        Returns:
            dict with status: "approved"
            
        Raises:
            ApplicationError: If approval is refused (estimates unavailable in live mode without override)
        """
        if override_unavailable_estimates and self.state.g1_plan:
            estimates_available = self.state.g1_plan.get("estimates_available", True)
            if not estimates_available:
                workflow.logger.warning(
                    "G1 approved with override_unavailable_estimates=True despite estimates_available=False"
                )
        
        self.approved_g1_shot_plan = True
        return {"status": "approved"}
    
    @approve_g1_shot_plan.validator
    def validate_g1_approval(self, override_unavailable_estimates: bool = False):
        """Validator for G1 approval: refuses while preflight errors exist (no override: a shot that
        breaks a documented model limit must be fixed, not approved), then checks estimates in live mode."""
        preflight_errors = (self.state.g1_plan or {}).get("preflight_errors") or []
        if preflight_errors:
            raise ApplicationError(
                f"Cannot approve G1: {len(preflight_errors)} shot(s) break a model limit. Fix them and "
                "restart the plan:\n" + "\n".join(preflight_errors),
                type="PreflightError",
                non_retryable=True,
            )
        # In live mode, block approval if estimates are unavailable unless explicitly overridden
        # Only check if g1_plan has been computed
        if self.state.g1_plan:
            estimates_available = self.state.g1_plan.get("estimates_available", True)
            dry_run = self.state.g1_plan.get("dry_run", False)  # Default False to be safe
            
            if not dry_run and not estimates_available and not override_unavailable_estimates:
                raise ApplicationError(
                    "Cannot approve G1 in live mode when estimates are unavailable. "
                    "Provider estimate requests failed. Pass override_unavailable_estimates=True to proceed anyway.",
                    non_retryable=True
                )
    
    @workflow.update
    async def approve_g2_scene_stills(self):
        """Approve scene stills (G2_SCENE_STILLS): All stills + refs."""
        self.approved_g2_scene_stills = True
    
    @approve_g2_scene_stills.validator
    def validate_approve_g2_scene_stills(self):
        """Validator for G2 approval - refuses if any retry is still pending."""
        # Safety: unreviewed retry stills must never go to paid clips
        # If a retry is pending, its new still hasn't been produced and reviewed yet
        if self.state.pending_retries:
            pending_list = ", ".join(self.state.pending_retries.keys())
            raise ApplicationError(
                f"Cannot approve G2 while retry is pending for: {pending_list}. "
                f"Wait for the retry still to complete and be reviewed first.",
                non_retryable=True
            )
    
    def _reject_problem(self, shot_id: str) -> str | None:
        """Why reject_shot(shot_id) is not allowed right now (None = allowed). No side effects."""
        if self.state.current_gate != "G2_SCENE_STILLS":
            return f"not at G2 gate (current: {self.state.current_gate})"
        if self.approved_g2_scene_stills or "G2_SCENE_STILLS" in self.passed_gates:
            return "G2 already approved, clips may be in progress"
        if shot_id not in self.shot_results:
            return "unknown shot (not launched in this episode)"
        if shot_id in self.state.pending_retries:
            return "a retry is pending for this shot; review its new still first"
        return None

    @workflow.update
    async def reject_shot(self, shot_id: str, reason: str) -> dict:
        """
        Reject a shot's still at G2 review (G-GATE-1).

        The rejected shot's child is sent ``still_rejected`` instead of ``stills_approved`` when G2
        is approved, so its still is never animated and no clip is paid for. The shot can be
        retried via retry_shot (clears the rejection; the new still is reviewed at G2 again).

        Args:
            shot_id: Shot ID to reject
            reason: Rejection reason
            
        Returns:
            dict with success status and message
        """
        problem = self._reject_problem(shot_id)
        if problem:  # re-check: state may have moved since the validator ran
            raise ApplicationError(f"Cannot reject {shot_id}: {problem}", non_retryable=True)
        self.state.shot_decisions[shot_id] = "rejected"
        self.rejection_reasons[shot_id] = reason or ""
        workflow.logger.info(f"Shot {shot_id} rejected at G2: {reason}")
        
        return {
            "success": True,
            "shot_id": shot_id,
            "action": "rejected",
            "reason": reason,
            "message": f"Shot {shot_id} rejected and will not progress to clips"
        }

    @reject_shot.validator
    def validate_reject_shot(self, shot_id: str, reason: str):
        problem = self._reject_problem(shot_id)
        if problem:
            raise ApplicationError(f"Cannot reject {shot_id}: {problem}", non_retryable=True)

    @workflow.update
    async def approve_shot(self, shot_id: str) -> dict:
        """Per-shot G2 decision "approve" (G-GATE-1 map): record the shot as approved, undoing an earlier
        reject_shot of the same still. Same preconditions as reject_shot (parked at G2, G2 not yet
        approved, no retry pending for the shot). G2 approval itself is still approve_g2_scene_stills."""
        problem = self._reject_problem(shot_id)
        if problem:
            raise ApplicationError(f"Cannot approve {shot_id}: {problem}", non_retryable=True)
        self.state.shot_decisions[shot_id] = "approved"
        self.rejection_reasons.pop(shot_id, None)
        workflow.logger.info(f"Shot {shot_id} approved at G2")
        return {"success": True, "shot_id": shot_id, "action": "approved",
                "message": f"Shot {shot_id} approved (goes to clips when G2 is approved)"}

    @approve_shot.validator
    def validate_approve_shot(self, shot_id: str):
        problem = self._reject_problem(shot_id)
        if problem:
            raise ApplicationError(f"Cannot approve {shot_id}: {problem}", non_retryable=True)
    
    # ---------------------------------------------------------------- picture assembly helpers
    def _assembly_plan(self) -> tuple[list[dict], dict[str, str], list[str], list[dict]]:
        """Edit plan from the LATEST shot outcomes, in shot order: completed, non-rejected shots with
        their trims. Returns (entries, clip_paths, completed shots without a local clip, excluded)."""
        entries: list[dict] = []
        paths: dict[str, str] = {}
        missing: list[str] = []
        excluded: list[dict] = []
        for shot in self.assembly_shots:
            sid = shot["shot_id"]
            outcome = self.shot_outcomes.get(sid) or {}
            if self.state.shot_decisions.get(sid) == "rejected" or outcome.get("status") == "rejected":
                excluded.append({"shot_id": sid, "status": "rejected"})
                continue
            if outcome.get("status") != "completed":
                excluded.append({"shot_id": sid, "status": outcome.get("status") or "missing"})
                continue
            path = outcome.get("clip_path")
            if not path:
                missing.append(sid)
                continue
            params = shot.get("params") or {}
            duration = float(params.get("duration", 5.0))
            trim = shot.get("trim") or {}
            entries.append({
                "shot_id": sid,
                "duration": duration,
                "trim_in": float(trim.get("in", 0.0)),
                "trim_out": float(trim.get("out", duration)),
            })
            paths[sid] = path
        return entries, paths, missing, excluded

    def _assembly_signature(self) -> list:
        sig = []
        for shot in self.assembly_shots:
            o = self.shot_outcomes.get(shot["shot_id"]) or {}
            sig.append([shot["shot_id"], o.get("status"), o.get("clip_url"), o.get("clip_path"),
                        self.state.shot_decisions.get(shot["shot_id"])])
        return sig

    def _assembly_stale(self) -> bool:
        """The clips changed (e.g. a clip retry landed) since the edit was last assembled."""
        return self.assembly_sig is not None and self._assembly_signature() != self.assembly_sig

    def _assembly_blocking(self) -> bool:
        qc = self.state.assembly_qc or {}
        return bool(qc.get("blocking")) and self.state.assembly_acceptance is None

    async def _run_picture_assembly(self, episode_id: str) -> None:
        """Assemble the mute edit from the approved clips, run its QC and record both in state.

        Never silently skipped: a completed shot without a local clip, an assembly error or a QC that
        cannot verify (e.g. no animal checker) end in a blocking NEEDS_REVIEW verdict; a still-only
        shot or an animal count / duck mismatch is a blocking FAIL. WATCH is recorded for the
        reviewer. NO_CLIPS (every shot rejected/failed, each already escalated) and DRY_RUN
        (placeholder clips) are recorded verdicts, not passes on pixels, and do not block."""
        self.state.stage = PipelineStage.MUTE_EDIT
        self.assembly_sig = self._assembly_signature()
        self.assembly_version += 1
        self.state.assembly_acceptance = None  # an acceptance applies to one edit only
        entries, paths, missing, excluded = self._assembly_plan()
        for sid, path in paths.items():
            self.state.output_paths.setdefault(sid, {})["clip"] = path
        qc: dict
        result: dict | None = None
        if missing:
            qc = {"verdict": "NEEDS_REVIEW",
                  "reason": f"completed shot(s) without a local clip file: {', '.join(missing)}"}
        elif not entries:
            # Every shot was rejected or failed (each already escalated per shot in failed_shots):
            # nothing to edit. Recorded and shown at G_FINAL_CUT; not a QC verdict on pixels.
            qc = {"verdict": "NO_CLIPS",
                  "reason": "no approved clips to assemble (all shots rejected/failed; see failed_shots)"}
        else:
            workflow.logger.info(f"Assembling {len(entries)} clips into the mute edit")
            try:
                result = await workflow.execute_activity(
                    assemble_mute_edit,
                    args=[episode_id, entries, paths, None],
                    start_to_close_timeout=timedelta(minutes=30),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                                             maximum_interval=timedelta(seconds=10)),
                )
                self.state.assembly_path = result["assembly_path"]
                self.state.assembly_manifest = result["manifest"]
                metas = {}
                for shot in self.assembly_shots:
                    if shot["shot_id"] in paths:
                        metas[shot["shot_id"]] = shot.get("shot_meta") or {}
                qc = await workflow.execute_activity(
                    check_assembly_qc,
                    args=[result["assembly_path"], result["manifest"], episode_id, metas],
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                                             maximum_interval=timedelta(seconds=10)),
                )
            except ActivityError as e:
                qc = {"verdict": "NEEDS_REVIEW", "reason": f"mute edit / QC failed: {e.cause or e}"}
        qc = dict(qc)
        qc["blocking"] = qc["verdict"] in ("FAIL", "NEEDS_REVIEW")
        qc["escalate"] = qc["verdict"] == "NEEDS_REVIEW" or bool(qc.get("escalate"))
        qc["excluded_shots"] = excluded
        qc["clip_watch_shots"] = sorted(
            sid for sid in paths
            if ((self.shot_outcomes.get(sid) or {}).get("clip_motion") or {}).get("verdict") == "WATCH"
        )
        qc["edit_version"] = self.assembly_version
        self.state.assembly_qc = qc
        if qc["blocking"]:
            workflow.logger.error(f"[PICTURE LOCK] mute edit QC {qc['verdict']}: {qc['reason']} (G_FINAL_CUT held)")
            if self.approved_g_final_cut:  # an approval sent before this verdict does not count
                self.approved_g_final_cut = False
                self.final_cut_refusals.append(
                    f"approval withdrawn: assembly QC {qc['verdict']}: {qc['reason']}")
        else:
            if qc["verdict"] == "WATCH" or qc["clip_watch_shots"]:
                workflow.logger.warning(f"[PICTURE LOCK] mute edit QC WATCH (review): {qc.get('reason')}")
            self.state.stage = PipelineStage.PICTURE_LOCK
            workflow.logger.info(f"[PICTURE LOCK] mute edit QC {qc['verdict']}")

    def _assembly_view(self) -> dict:
        return {
            "path": self.state.assembly_path,
            "manifest": dict(self.state.assembly_manifest or {}),
            "qc": dict(self.state.assembly_qc or {}),
            "acceptance": dict(self.state.assembly_acceptance) if self.state.assembly_acceptance else None,
            "blocking": self._assembly_blocking(),
            "stale": self._assembly_stale(),
            "edit_version": self.assembly_version,
        }

    # ---------------------------------------------------------------- audio + delivery (Round 4)
    def _delivery_signature(self) -> list:
        return [self.assembly_version, self.state.assembly_path]

    def _delivery_due(self) -> bool:
        """A delivery is (re)built for each usable picture: current (not stale), not blocking (or
        accepted by a reviewer), and not already delivered for this edit version."""
        if self.assembly_sig is None or self._assembly_stale() or self._assembly_blocking():
            return False
        return self.delivery_sig != self._delivery_signature()

    def _delivery_blocking(self) -> bool:
        qc = self.state.delivery_qc or {}
        return bool(qc.get("blocking")) and self.state.delivery_acceptance is None

    async def _resolve_audio_inputs(self, episode_id: str, plan: dict, picture_s: float) -> dict:
        """Locked / library files or ElevenLabs generation for every VO line, SFX cue and music bed."""
        policy = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                             maximum_interval=timedelta(seconds=10))
        plan = dict(plan)
        dry_inputs: list[str] = []
        vo = dict(plan.get("vo") or {})
        lines = []
        for line in vo.get("lines") or []:
            res = await workflow.execute_activity(
                "generate_voiceover",
                args=[episode_id, line.get("text") or "", vo.get("voice_id") or "", vo.get("model") or "eleven_v3",
                      line["id"], line.get("path")],
                start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(minutes=1),
                retry_policy=policy,
            )
            if res.get("dry_run"):
                dry_inputs.append(f"vo:{line['id']}")
            lines.append({**line, "path": res["path"]})
        vo["lines"] = lines
        plan["vo"] = vo
        cues = []
        for cue in plan.get("sfx") or []:
            res = await workflow.execute_activity(
                "generate_sfx",
                args=[episode_id, cue.get("prompt") or "", min(30.0, max(0.5, float(cue["dur"]))), cue["id"],
                      cue.get("path")],
                start_to_close_timeout=timedelta(minutes=5), heartbeat_timeout=timedelta(minutes=1),
                retry_policy=policy,
            )
            if res.get("dry_run"):
                dry_inputs.append(f"sfx:{cue['id']}")
            cues.append({**cue, "path": res["path"]})
        plan["sfx"] = cues
        if plan.get("music"):
            music = dict(plan["music"])
            length = min(300.0, max(3.0, picture_s - float(music.get("offset_s", 0.0))))
            res = await workflow.execute_activity(
                "generate_music",
                args=[episode_id, music.get("prompt") or "", length, music.get("model") or "music_v1",
                      music.get("id") or "music", music.get("path")],
                start_to_close_timeout=timedelta(minutes=10), heartbeat_timeout=timedelta(minutes=2),
                retry_policy=policy,
            )
            if res.get("dry_run"):
                dry_inputs.append("music")
            music["path"] = res["path"]
            plan["music"] = music
        plan["dry_inputs"] = dry_inputs
        return plan

    async def _run_delivery(self, episode_id: str) -> None:
        """Audio round + delivery for the current picture, then delivery QC.

        A missing plan / input file, a mix error or an unverifiable QC is a blocking NEEDS_REVIEW;
        a measured miss (loudness, true peak, duration, streams, subs, size) is a blocking FAIL.
        Both hold G_FINAL_CUT until a passing rebuild or a human accept_delivery_qc(reason).
        DRY_RUN (stand-in picture or inputs) is recorded and does not block."""
        # state.stage stays PICTURE_LOCK (the picture is locked while audio is built); the audio
        # round's own phase is reported in get_state()["delivery"]["phase"].
        self.delivery_phase = PipelineStage.AUDIO.value
        self.delivery_sig = self._delivery_signature()
        self.delivery_version += 1
        self.state.delivery_acceptance = None  # an acceptance applies to one delivery only
        self.state.delivery_manifest = {}
        self.state.delivery_paths = {}
        aqc = self.state.assembly_qc or {}
        path = self.state.assembly_path
        qc: dict
        if aqc.get("verdict") == "NO_CLIPS" or not path:
            qc = {"verdict": "NO_PICTURE", "reason": "no assembled picture to deliver (see assembly QC)"}
        else:
            # Calculate picture duration from assembly manifest
            # In dry mode with placeholder clips, compute from shot plan durations
            manifest_duration = float((self.state.assembly_manifest or {}).get("total_duration") or 0.0)
            if manifest_duration > 0:
                picture_s = manifest_duration
            else:
                # Dry mode fallback: sum shot durations from assembly plan
                picture_s = 0.0
                for shot in self.assembly_shots:
                    params = shot.get("params") or {}
                    duration = float(params.get("duration", 5.0))
                    trim = shot.get("trim") or {}
                    trim_in = float(trim.get("in", 0.0))
                    trim_out = float(trim.get("out", duration))
                    picture_s += (trim_out - trim_in)
                if picture_s <= 0:
                    picture_s = 60.0  # Final fallback
                workflow.logger.info(f"Dry mode: calculated picture duration from shot plans: {picture_s}s")
            
            policy = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1),
                                 maximum_interval=timedelta(seconds=10))
            try:
                plan = await workflow.execute_activity(
                    load_audio_plan, args=[episode_id, None],
                    start_to_close_timeout=timedelta(minutes=1), retry_policy=policy,
                )
                plan = await self._resolve_audio_inputs(episode_id, plan, picture_s)
                self.delivery_phase = PipelineStage.FINAL_MIX.value
                manifest = await workflow.execute_activity(
                    mix_and_deliver, args=[episode_id, path, plan, None],
                    start_to_close_timeout=timedelta(minutes=60), heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=policy,
                )
                self.state.delivery_manifest = manifest
                self.state.delivery_paths = dict(manifest.get("paths") or {})
                self.delivery_phase = PipelineStage.DELIVERY.value
                qc = await workflow.execute_activity(
                    check_delivery_qc, args=[episode_id, manifest, plan, None],
                    start_to_close_timeout=timedelta(minutes=15), heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=policy,
                )
            except ActivityError as e:
                qc = {"verdict": "NEEDS_REVIEW", "reason": f"audio / delivery failed: {e.cause or e}"}
        qc = dict(qc)
        self.delivery_phase = "done"
        qc["blocking"] = qc["verdict"] in ("FAIL", "NEEDS_REVIEW")
        qc["delivery_version"] = self.delivery_version
        qc["edit_version"] = self.assembly_version
        qc.setdefault("reason", ", ".join(qc.get("failed") or []) or None)
        self.state.delivery_qc = qc
        for name, out in (self.state.delivery_paths or {}).items():
            if name in ("master_subs", "nosubs", "phone", "mix_wav"):
                self.state.output_paths.setdefault("delivery", {})[name] = out
        if qc["blocking"]:
            workflow.logger.error(f"[DELIVERY] QC {qc['verdict']}: {qc['reason']} (G_FINAL_CUT held)")
            if self.approved_g_final_cut:  # an approval sent before this verdict does not count
                self.approved_g_final_cut = False
                self.final_cut_refusals.append(
                    f"approval withdrawn: delivery QC {qc['verdict']}: {qc['reason']}")
        else:
            workflow.logger.info(f"[DELIVERY] QC {qc['verdict']}")

    def _delivery_view(self) -> dict:
        return {
            "paths": dict(self.state.delivery_paths or {}),
            "manifest": dict(self.state.delivery_manifest or {}),
            "qc": dict(self.state.delivery_qc or {}),
            "acceptance": dict(self.state.delivery_acceptance) if self.state.delivery_acceptance else None,
            "blocking": self._delivery_blocking(),
            "due": self._delivery_due(),
            "delivery_version": self.delivery_version,
            "phase": self.delivery_phase,
        }

    @workflow.signal
    def accept_delivery_qc(self, reason: str):
        """Human review of a blocking delivery QC verdict (FAIL / NEEDS_REVIEW): accept it with a
        reason so G_FINAL_CUT can be approved. Applies to the current delivery only."""
        qc = self.state.delivery_qc or {}
        if not (reason or "").strip():
            self.final_cut_refusals.append("delivery QC acceptance ignored: a reason is required")
            return
        if not qc.get("blocking"):
            self.final_cut_refusals.append("delivery QC acceptance ignored: no blocking verdict to accept")
            return
        self.state.delivery_acceptance = {
            "reason": reason.strip(), "verdict": qc.get("verdict"), "delivery_version": self.delivery_version,
        }
        workflow.logger.warning(f"Delivery QC {qc.get('verdict')} accepted by reviewer: {reason}")

    @workflow.signal
    async def approve_g_final_cut(self):
        """Approve final cut (G_FINAL_CUT): After edit.

        Ignored (and recorded in final_cut_refusals) while a clip retry is still running: the
        reviewer has not seen the replacement clip yet. Send the approval again once it landed.
        """
        if self.pending_clip_retries:
            pending = ", ".join(sorted(self.pending_clip_retries))
            self.final_cut_refusals.append(f"clip retry pending for: {pending}")
            workflow.logger.warning(f"Final cut approval ignored: clip retry pending for {pending}")
            return
        if self._assembly_blocking():
            qc = self.state.assembly_qc
            refusal = f"assembly QC {qc.get('verdict')}: {qc.get('reason')}"
            self.final_cut_refusals.append(refusal)
            workflow.logger.warning(f"Final cut approval ignored: {refusal}")
            return
        if self._delivery_blocking():
            dqc = self.state.delivery_qc
            refusal = f"delivery QC {dqc.get('verdict')}: {dqc.get('reason')}"
            self.final_cut_refusals.append(refusal)
            workflow.logger.warning(f"Final cut approval ignored: {refusal}")
            return
        self.approved_g_final_cut = True

    @workflow.signal
    def reject_g_final_cut(self, reason: str):
        """Reviewer rejects the final cut at G_FINAL_CUT: recorded with the edit / delivery versions it
        applies to; any earlier approval is withdrawn and the gate stays held. The fix is a clip retry
        (rebuilds the edit and the delivery) or a new delivery; then approve again."""
        if self.state.current_gate != "G_FINAL_CUT" or "G_FINAL_CUT" in self.passed_gates:
            self.final_cut_refusals.append("final cut rejection ignored: not parked at G_FINAL_CUT")
            return
        if not (reason or "").strip():
            self.final_cut_refusals.append("final cut rejection ignored: a reason is required")
            return
        self.approved_g_final_cut = False
        self.final_cut_rejections.append({
            "reason": reason.strip(), "edit_version": self.assembly_version,
            "delivery_version": self.delivery_version,
        })
        workflow.logger.warning(f"Final cut rejected by reviewer: {reason}")

    @workflow.signal
    def accept_assembly_qc(self, reason: str):
        """Human review of a blocking mute-edit QC verdict (FAIL / NEEDS_REVIEW): accept it with a
        reason so G_FINAL_CUT can be approved. Applies to the current edit only (a rebuild resets it)."""
        qc = self.state.assembly_qc or {}
        if not (reason or "").strip():
            self.final_cut_refusals.append("assembly QC acceptance ignored: a reason is required")
            return
        if not qc.get("blocking"):
            self.final_cut_refusals.append("assembly QC acceptance ignored: no blocking verdict to accept")
            return
        self.state.assembly_acceptance = {
            "reason": reason.strip(), "verdict": qc.get("verdict"), "edit_version": self.assembly_version,
        }
        workflow.logger.warning(f"Assembly QC {qc.get('verdict')} accepted by reviewer: {reason}")
    
    @workflow.signal
    def raise_credit_cap(self, new_cap: int):
        """
        Raise credit cap and resume paused run.
        
        Signal handler just sets flags - actual work done in main loop.
        """
        self.state.credit_cap_raised = True
        self.state.new_credit_cap = new_cap
    
    async def _process_cap_raise(self) -> None:
        """Apply a pending raise_credit_cap: update the ledger cap, resume every shot child (including
        clip-retry children) parked at the cap. Called from the clip loop and the G_FINAL_CUT loop."""
        while self.state.credit_cap_raised and self.state.new_credit_cap > 0:
            new_cap = self.state.new_credit_cap
            workflow.logger.info(f"Processing credit cap raise to {new_cap}")
            await workflow.execute_activity(
                update_credit_cap_activity,
                args=[self.state.episode_id, new_cap],
                start_to_close_timeout=timedelta(seconds=30),
            )
            handles = dict(self.shot_results)
            for retry_shot_id, retry_wf_id in self.pending_clip_retries.items():
                handles[f"{retry_shot_id}-clipretry"] = workflow.get_external_workflow_handle(retry_wf_id)
            for cap_shot_id, shot_handle in handles.items():
                try:
                    await shot_handle.signal("resume_after_cap_raise")
                    workflow.logger.info(f"Signaled cap raise to shot {cap_shot_id}")
                except Exception as e:
                    workflow.logger.warning(f"Failed to signal shot {cap_shot_id}: {e}")
            if self.state.new_credit_cap == new_cap:  # a newer raise that arrived meanwhile loops again
                self.state.new_credit_cap = 0

    @workflow.signal
    def retry_still_ready(self, shot_id: str, attempt: int):
        """
        Signal from retry child: its still has landed (produced and available for review).
        
        This signal clears pending_retries for this shot, allowing G2 approval.
        The design: G2 approval requires all retry stills to be produced and reviewed.
        The retry child sends this after producing its still(s) and BEFORE waiting for
        stills_approved. G2 approval then counts as reviewing that still.
        
        Args:
            shot_id: Shot ID whose retry still is ready
            attempt: Attempt number (for logging/verification)
        """
        if shot_id in self.state.pending_retries:
            workflow.logger.info(f"Retry still ready for {shot_id} (attempt {attempt}), clearing pending_retries")
            self.state.pending_retries.pop(shot_id, None)
        else:
            workflow.logger.warning(f"retry_still_ready called for {shot_id} but not in pending_retries")
    
    @workflow.update
    async def retry_shot(self, shot_id: str) -> dict:
        """
        Retry a failed shot (only valid while parked at G1/G2 for a shot in failed_shots).
        
        Re-runs only that shot's still child, respects credit/USD cap and smoke gate,
        enforces MAX_SHOT_RETRIES from config, uses NEW idempotency key per attempt,
        moves shot from failed_shots back to running then to done or failed,
        and records attempts in episode state.
        
        Args:
            shot_id: Shot ID to retry
            
        Returns:
            dict with status: "queued" | "refused", reason if refused
        
        Raises:
            ApplicationError: If retry is refused (validation failed)
        """
        # Validate: must be parked at G1 or G2, and G2 must not be approved yet
        if self.state.current_gate not in ("G1_SHOT_PLAN", "G2_SCENE_STILLS"):
            workflow.logger.error(f"Cannot retry {shot_id}: not at G1/G2 gate (current: {self.state.current_gate})")
            raise ApplicationError(
                f"Cannot retry {shot_id}: not at G1 or G2 gate (current: {self.state.current_gate})",
                non_retryable=True
            )
        
        # Block retries once G2 is approved (clips have started)
        if "G2_SCENE_STILLS" in self.passed_gates:
            workflow.logger.error(f"Cannot retry {shot_id}: G2 already approved, clips in progress")
            raise ApplicationError(
                f"Cannot retry {shot_id}: G2 already approved, too late to retry stills",
                non_retryable=True
            )
        
        # Block retries while a retry is already pending for this shot (check immediate duplicates FIRST)
        if shot_id in self.state.pending_retries:
            workflow.logger.error(f"Cannot retry {shot_id}: retry already pending")
            raise ApplicationError(
                f"Cannot retry {shot_id}: retry already in progress",
                non_retryable=True
            )
        
        # Check retry limit (so error message is about max retries when budget is exhausted)
        # shot_attempts tracks total attempts (original + retries)
        # MAX_SHOT_RETRIES is max number of retries (not including original)
        # So max total attempts = MAX_SHOT_RETRIES + 1
        attempt_count = self.state.shot_attempts.get(shot_id, 0)
        max_retries = config.MAX_SHOT_RETRIES
        if attempt_count > max_retries:  # Already exceeded retry limit
            workflow.logger.error(
                f"Cannot retry {shot_id}: already attempted {attempt_count} times (max {max_retries} retries = {max_retries + 1} total attempts)"
            )
            raise ApplicationError(
                f"Max retries ({max_retries}) exceeded for shot {shot_id}",
                non_retryable=True
            )
        
        # Validate: shot must be in failed_shots OR have been retried before
        failed_shot = None
        for fs in self.state.failed_shots:
            if fs["shot_id"] == shot_id:
                failed_shot = fs
                break
        
        if not failed_shot and shot_id not in self.state.shot_attempts:
            workflow.logger.error(f"Cannot retry {shot_id}: not in failed_shots")
            raise ApplicationError(
                f"Cannot retry {shot_id}: shot not in failed_shots",
                non_retryable=True
            )
        
        # Remove from failed_shots and increment attempt count
        self.state.failed_shots = [fs for fs in self.state.failed_shots if fs["shot_id"] != shot_id]
        self.state.shot_attempts[shot_id] = attempt_count + 1
        
        workflow.logger.info(
            f"Retrying {shot_id} (attempt {self.state.shot_attempts[shot_id]}/{max_retries})"
        )
        
        # Find the shot plan from parsed beatmap
        shot_plan = None
        for shot in self.parsed_beatmap.get("shots", []):
            if shot["shot_id"] == shot_id:
                shot_plan = shot.copy()
                break
        
        if not shot_plan:
            workflow.logger.error(f"Cannot retry {shot_id}: not found in beatmap")
            raise ApplicationError(
                f"Cannot retry {shot_id}: shot not found in beatmap",
                non_retryable=True
            )
        
        # Pass attempt number and version into shot_plan
        actual_attempts = self.state.shot_attempts[shot_id]
        shot_plan["attempt"] = actual_attempts
        shot_plan["version"] = actual_attempts
        
        # Build prompt if missing
        if "prompt" not in shot_plan:
            from hfvg.episode_parser import build_prompt_from_shot
            shot_plan["prompt"] = build_prompt_from_shot(shot_plan)
        
        # Launch new still child with NEW idempotency key (include attempt number)
        # Original = attempt 1, first retry = attempt 2 (-r1), second retry = attempt 3 (-r2)
        # Workflow ID uses retry number (attempts - 1): retry1, retry2, etc.
        retry_num = self.state.shot_attempts[shot_id] - 1
        workflow_id = f"{self.state.episode_id}-shot-{shot_id}-retry{retry_num}"
        
        # Add parent workflow ID to shot_plan for retry_still_ready signaling
        shot_plan_with_parent = dict(shot_plan)
        shot_plan_with_parent["parent_workflow_id"] = workflow.info().workflow_id
        
        try:
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[self.state.episode_id, shot_plan_with_parent],
                id=workflow_id,
                task_queue=workflow.info().task_queue,
            )
            
            # Update shot_results to track latest handle
            self.shot_results[shot_id] = handle
            self.launched_shot_plans[shot_id] = shot_plan_with_parent
            # A retried still is a NEW still: any G2 rejection of the old one no longer applies
            self.state.shot_decisions.pop(shot_id, None)
            self.rejection_reasons.pop(shot_id, None)
            
            # Track pending retry in workflow state for deterministic checking
            # Completion will be checked via background task that clears pending_retries
            self.state.pending_retries[shot_id] = workflow_id
            
            # Create background task to track retry completion and clear pending_retries
            # Use the track_child_completion function from the main shot loop (redefine locally)
            import asyncio
            
            async def track_retry_completion():
                """Background task to await retry child and clear pending_retries."""
                try:
                    result = await handle
                    self.shot_outcomes[shot_id] = result if isinstance(result, dict) else {"status": "unknown"}
                    # Clear pending_retries when retry completes
                    self.state.pending_retries.pop(shot_id, None)
                    workflow.logger.info(f"Retry child {shot_id} completed, cleared from pending_retries")
                    
                    # Check if result indicates failure
                    if isinstance(result, dict) and result.get("status") in ("failed", "blocked"):
                        error_detail = result.get("error", "Unknown error")
                        self.state.failed_shots.append({
                            "shot_id": shot_id,
                            "status": "needs_review" if result.get("needs_review") else "failed",
                            "reason": error_detail
                        })
                        workflow.logger.error(f"Retry shot {shot_id} failed: {error_detail}")
                except Exception as e:
                    # Retry child raised an exception
                    error_detail = str(e)
                    self.state.pending_retries.pop(shot_id, None)
                    workflow.logger.info(f"Retry child {shot_id} failed with exception, cleared from pending_retries")
                    self.state.failed_shots.append({
                        "shot_id": shot_id,
                        "status": "failed",
                        "reason": f"Retry child error: {error_detail}"
                    })
                    workflow.logger.error(f"Retry shot {shot_id} failed: {error_detail}")
            
            # Start the background task
            retry_task = asyncio.create_task(track_retry_completion())
            self.background_tasks.append(retry_task)
            
            workflow.logger.info(f"Retry child started for {shot_id}: {workflow_id}")
            
            return {"status": "queued", "workflow_id": workflow_id}
            
        except Exception as e:
            workflow.logger.error(f"Failed to start retry child for {shot_id}: {e}")
            # Re-add to failed_shots if it was there before
            if failed_shot:
                self.state.failed_shots.append(failed_shot)
            raise ApplicationError(
                f"Failed to start retry for {shot_id}: {str(e)}",
                non_retryable=True
            )
    
    def _end_backfill_problem(self, shot_id: str) -> str | None:
        """Why retry_end_still(shot_id) is not allowed right now (None = allowed). No side effects."""
        if self.state.current_gate != "G2_SCENE_STILLS":
            return f"not parked at G2 (current: {self.state.current_gate})"
        if self.approved_g2_scene_stills or "G2_SCENE_STILLS" in self.passed_gates:
            return "G2 already approved"
        if shot_id in self.state.pending_retries:
            return "a retry/backfill is already pending"
        failed = next((fs for fs in self.state.failed_shots if fs["shot_id"] == shot_id), None)
        if not failed:
            return "shot not in failed_shots"
        if not str(failed.get("reason", "")).startswith("End still failed"):
            return f"shot failed for another reason ({failed.get('reason')!r}); use retry_shot"
        plan = next((s for s in (self.parsed_beatmap or {}).get("shots", []) if s["shot_id"] == shot_id), None)
        if not plan:
            return "shot not found in the shot plan"
        if not (plan.get("params") or {}).get("end_frame"):
            return "shot has no end frame"
        return None

    @workflow.update
    async def retry_end_still(self, shot_id: str, end_prompt: str | None = None) -> dict:
        """Backfill ONLY the _end still of a shot whose start still landed but whose _end failed.

        Unlike retry_shot this never resubmits the start: it resolves the start still that already
        landed for the current attempt (free status lookup), then starts a ShotWorkflow child
        {ep}-shot-{shot}-end{n} with the SAME attempt/version and reuse_start_still, so the only paid
        call is the _end still (lead ref = that start still; deterministic job id
        still-{ep}-{shot}_end-r{attempt-1}-lead<sha8>[...]). The shot then goes through the normal
        review -> retry_still_ready -> wait-for-G2 path; G2 stays a human decision. Refused if an _end
        with a lead ref was already submitted for this attempt (no retries).
        """
        problem = self._end_backfill_problem(shot_id)
        if problem:   # re-check: state may have moved since the validator ran
            raise ApplicationError(f"Cannot backfill {shot_id}_end: {problem}", non_retryable=True)
        attempt = self.state.shot_attempts.get(shot_id, 1)
        plan = next(s for s in self.parsed_beatmap.get("shots", []) if s["shot_id"] == shot_id)
        n = self.end_backfills.get(shot_id, 0) + 1
        workflow_id = f"{self.state.episode_id}-shot-{shot_id}-end{n}"
        self.state.pending_retries[shot_id] = workflow_id   # blocks G2 and duplicate calls meanwhile
        try:
            start = await workflow.execute_activity(
                "resolve_landed_start_still",
                args=[self.state.episode_id, shot_id, attempt, attempt],
                start_to_close_timeout=timedelta(seconds=60),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as e:
            self.state.pending_retries.pop(shot_id, None)
            cause = getattr(e, "cause", None) or e
            raise ApplicationError(f"Cannot backfill {shot_id}_end: {cause}", non_retryable=True)

        shot_plan = dict(plan)
        if "prompt" not in shot_plan:
            from hfvg.episode_parser import build_prompt_from_shot
            shot_plan["prompt"] = build_prompt_from_shot(shot_plan)
        if end_prompt:
            shot_plan["end_prompt"] = end_prompt
        shot_plan["attempt"] = attempt
        shot_plan["version"] = attempt
        shot_plan["parent_workflow_id"] = workflow.info().workflow_id
        shot_plan["reuse_start_still"] = {"url": start["url"], "job_id": start["job_id"]}
        failed_before = [fs for fs in self.state.failed_shots if fs["shot_id"] == shot_id]
        try:
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[self.state.episode_id, shot_plan],
                id=workflow_id,
                task_queue=workflow.info().task_queue,
            )
        except Exception as e:
            self.state.pending_retries.pop(shot_id, None)
            raise ApplicationError(f"Failed to start _end backfill for {shot_id}: {e}", non_retryable=True)
        self.end_backfills[shot_id] = n
        self.state.failed_shots = [fs for fs in self.state.failed_shots if fs["shot_id"] != shot_id]
        self.shot_results[shot_id] = handle
        self.launched_shot_plans[shot_id] = shot_plan

        async def track_backfill():
            try:
                result = await handle
                self.shot_outcomes[shot_id] = result if isinstance(result, dict) else {"status": "unknown"}
                self.state.pending_retries.pop(shot_id, None)
                if isinstance(result, dict) and result.get("status") == "failed":
                    self.state.failed_shots.append(
                        {"shot_id": shot_id, "status": "failed", "reason": result.get("error", "Unknown error")})
                    workflow.logger.error(f"_end backfill for {shot_id} failed: {result.get('error')}")
            except Exception as e:
                self.state.pending_retries.pop(shot_id, None)
                self.state.failed_shots.append(
                    {"shot_id": shot_id, "status": "failed", "reason": f"End still failed: backfill child error: {e}"})

        self.background_tasks.append(asyncio.create_task(track_backfill()))
        workflow.logger.info(
            f"_end backfill started for {shot_id}: {workflow_id} (attempt {attempt}, reusing start {start['job_id']}; "
            f"was failed: {failed_before})"
        )
        return {"status": "queued", "workflow_id": workflow_id, "attempt": attempt,
                "start_job_id": start["job_id"], "start_still_url": start["url"]}

    @retry_end_still.validator
    def validate_retry_end_still(self, shot_id: str, end_prompt: str | None = None):
        problem = self._end_backfill_problem(shot_id)
        if problem:
            raise ApplicationError(f"Cannot backfill {shot_id}_end: {problem}", non_retryable=True)

    def _clip_retry_problem(self, shot_id: str, reason: str) -> str | None:
        """Why retry_clip(shot_id) is not allowed right now (None = allowed). No side effects."""
        if not (self.approved_g2_scene_stills and "G2_SCENE_STILLS" in self.passed_gates):
            return "G2 not approved yet (clips not started); use retry_shot for stills"
        if self.approved_g_final_cut:
            return "final cut already approved"
        if not (reason or "").strip():
            return "a reason is required for a paid clip retry"
        if shot_id not in self.launched_shot_plans:
            return "unknown shot (no still launched in this episode)"
        if self.state.shot_decisions.get(shot_id) == "rejected":
            return "still was rejected at G2; a rejected still is never animated"
        if shot_id in self.pending_clip_retries:
            return "a clip retry is already pending for this shot"
        outcome = self.shot_outcomes.get(shot_id)
        if outcome is None:
            return "the shot's clip has not finished yet"
        if outcome.get("status") == "rejected":
            return "still was rejected at G2; a rejected still is never animated"
        if not outcome.get("still_url"):
            return "no approved still to animate (the shot failed before its still landed)"
        limit = config.MAX_PAID_RETRIES_PER_SHOT_PER_ROUND
        if self.clip_retries.get(shot_id, 0) >= limit:
            return f"max paid clip retries per shot per round ({limit}) reached"
        return None

    @workflow.update
    async def retry_clip(self, shot_id: str, reason: str, prompt_override: str | None = None) -> dict:
        """G-RETRY-1: re-run ONLY the clip of a shot after G2, reusing its approved still.

        Bounded: at most MAX_PAID_RETRIES_PER_SHOT_PER_ROUND (default 1) paid clip retries per shot in
        the G3 clip round, and the per-round USD cap / episode stop are checked on the USD ledger
        (check_round_budget_activity) BEFORE the child starts; the paid submit enforces both again.
        The child {ep}-shot-{shot}-clipretry{n} gets a deterministic job id
        clip-{ep}-{shot}-v{version}-c{n}, so a replayed/retried submit is idempotent. Final-cut
        approval is held back while the retry runs.
        """
        problem = self._clip_retry_problem(shot_id, reason)
        if problem:  # re-check: state may have moved since the validator ran
            raise ApplicationError(f"Cannot retry clip for {shot_id}: {problem}", non_retryable=True)
        ep = self.state.episode_id
        n = self.clip_retries.get(shot_id, 0) + 1
        outcome = self.shot_outcomes[shot_id]
        shot_plan = dict(self.launched_shot_plans[shot_id])
        params = shot_plan.get("params") or {}
        version = shot_plan.get("version", shot_plan.get("attempt", 1))
        job_id = f"clip-{ep}-{shot_id}-v{version}-c{n}"
        workflow_id = f"{ep}-shot-{shot_id}-clipretry{n}"
        self.pending_clip_retries[shot_id] = workflow_id  # blocks duplicates + final cut meanwhile
        try:
            check = await workflow.execute_activity(
                "check_round_budget_activity",
                args=[ep, "G3_CLIPS", shot_id, job_id, params.get("model", "kling"),
                      params.get("duration", 5.0), f"c{n}"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except Exception as e:
            self.pending_clip_retries.pop(shot_id, None)
            cause = getattr(e, "cause", None) or e
            raise ApplicationError(
                f"Cannot retry clip for {shot_id}: round budget refused: {cause}",
                type="RoundBudgetError", non_retryable=True,
            )
        if prompt_override:
            shot_plan["prompt"] = prompt_override
            # the override is the prompt for the re-run CLIP: it replaces the shot's clip_prompt too
            # (otherwise clip_prompt would win and the override would be silently ignored)
            shot_plan["clip_prompt"] = prompt_override
        shot_plan["parent_workflow_id"] = workflow.info().workflow_id
        shot_plan["clip_only"] = {
            "n": n,
            "reason": reason,
            "still_url": outcome["still_url"],
            "end_image_url": outcome.get("end_image_url"),
        }
        try:
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[ep, shot_plan],
                id=workflow_id,
                task_queue=workflow.info().task_queue,
            )
        except Exception as e:
            self.pending_clip_retries.pop(shot_id, None)
            raise ApplicationError(f"Failed to start clip retry for {shot_id}: {e}", non_retryable=True)
        self.clip_retries[shot_id] = n
        self.shot_results[shot_id] = handle
        self.clip_retry_log.append({
            "round": "G3_CLIPS", "shot_id": shot_id, "n": n, "reason": reason,
            "workflow_id": workflow_id, "job_id": job_id,
            "estimate_usd_micros": (check or {}).get("estimate_usd_micros"),
        })

        async def track_clip_retry():
            try:
                result = await handle
                self.shot_outcomes[shot_id] = result if isinstance(result, dict) else {"status": "unknown"}
            except Exception as e:
                self.shot_outcomes[shot_id] = {
                    "status": "failed", "error": f"Clip retry child error: {e}",
                    "still_url": outcome["still_url"], "end_image_url": outcome.get("end_image_url"),
                }
            finally:
                self.pending_clip_retries.pop(shot_id, None)

        self.background_tasks.append(asyncio.create_task(track_clip_retry()))
        workflow.logger.info(f"Clip retry {n} started for {shot_id}: {workflow_id} ({reason})")
        return {"status": "queued", "workflow_id": workflow_id, "retry": n, "job_id": job_id,
                "estimate_usd_micros": (check or {}).get("estimate_usd_micros")}

    @retry_clip.validator
    def validate_retry_clip(self, shot_id: str, reason: str, prompt_override: str | None = None):
        problem = self._clip_retry_problem(shot_id, reason)
        if problem:
            raise ApplicationError(f"Cannot retry clip for {shot_id}: {problem}", non_retryable=True)

    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        return {
            "episode_id": self.state.episode_id,
            "stage": self.state.stage.value if self.state.stage else None,
            "current_gate": self.state.current_gate,
            "passed_gates": self.passed_gates.copy(),
            "missing_refs": {k: list(v) for k, v in self.missing_refs.items()},
            "failed_shots": self.state.failed_shots.copy(),
            "shots_on_hold": self.state.shots_on_hold.copy(),
            "shot_attempts": self.state.shot_attempts.copy(),
            "shot_attempt_overrides": self.state.shot_attempt_overrides.copy(),
            "pending_retries": list(self.state.pending_retries),
            "g1_plan": self.state.g1_plan.copy(),
            "output_paths": {k: v.copy() for k, v in self.state.output_paths.items()},
            "approvals": {
                "g1_shot_plan": self.approved_g1_shot_plan,
                "g2_scene_stills": self.approved_g2_scene_stills,
                "g_final_cut": self.approved_g_final_cut,
            },
            "shots": len(self.shot_results),
            "shot_decisions": dict(self.state.shot_decisions),
            "clip_retries": dict(self.clip_retries),
            "pending_clip_retries": list(self.pending_clip_retries),
            "clip_retry_log": [dict(r) for r in self.clip_retry_log],
            "final_cut_refusals": list(self.final_cut_refusals),
            "final_cut_rejections": [dict(r) for r in self.final_cut_rejections],
            "shot_workflow_ids": {k: h.id for k, h in self.shot_results.items()},
            "assembly": self._assembly_view(),
            "delivery": self._delivery_view(),
            "shot_outcomes": {
                k: {"status": v.get("status"), "still_url": v.get("still_url"), "clip_url": v.get("clip_url")}
                for k, v in self.shot_outcomes.items()
            },
        }
