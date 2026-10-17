"""EpisodeWorkflow V2: Handbook-compliant workflow with steps 0-7 and Travis approval points."""

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

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
    Episode workflow following Mid-Mountain Rest HARNESS-HANDBOOK v1.1.
    
    Steps 0-7 with 15 Travis approval points:
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
        
        # In live mode, check for required files before acquiring lock
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
            args=["mid-mountain-rest"],
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
        
        # Get list of anchor shots to skip (they're already approved)
        anchor_stills = await workflow.execute_activity(
            list_anchor_stills,
            args=[episode_id],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        anchor_shot_ids = {a["anchor_shot_id"] for a in anchor_stills}
        if anchor_shot_ids:
            workflow.logger.info(f"Skipping anchor shots (already approved): {sorted(anchor_shot_ids)}")
        
        # Filter out anchor shots
        shots_to_generate = [s for s in shots if s["shot_id"] not in anchor_shot_ids]
        
        # Launch still generation for all shots (except anchors)
        workflow.logger.info(f"Launching still generation for {len(shots_to_generate)} shots (skipped {len(anchor_shot_ids)} anchors)...")
        shot_workflow_handles = []
        shot_completion_status = {}  # Track completion: {shot_id: {"done": bool, "success": bool, "error": str?}}
        
        # Track background tasks that monitor child completion
        import asyncio
        background_tasks = []
        
        async def track_child_completion(shot_id: str, handle):
            """Background task to await child and record result in state."""
            try:
                result = await handle
                # Check if result indicates failure (Issue B: shot returns failed dict instead of raising)
                if isinstance(result, dict) and result.get("status") == "failed":
                    error_detail = result.get("error", "Unknown error")
                    shot_completion_status[shot_id] = {"done": True, "success": False, "error": error_detail}
                    self.state.failed_shots.append({
                        "shot_id": shot_id,
                        "status": "failed",
                        "reason": error_detail
                    })
                    workflow.logger.error(f"Shot {shot_id} failed: {error_detail}")
                else:
                    shot_completion_status[shot_id] = {"done": True, "success": True, "result": result}
                    workflow.logger.info(f"Shot {shot_id} completed successfully")
            except Exception as e:
                # Child raised an exception
                error_detail = str(e)
                
                shot_completion_status[shot_id] = {"done": True, "success": False, "error": error_detail}
                workflow.logger.error(f"Shot {shot_id} failed: {error_detail}")
                # Record in state for API/query exposure
                self.state.failed_shots.append({
                    "shot_id": shot_id,
                    "status": "failed",
                    "reason": error_detail,
                })
        
        for shot in shots_to_generate:
            shot_plan = shot.copy()
            shot_plan["dry_run"] = dry_run
            if "prompt" not in shot_plan:
                shot_plan["prompt"] = build_prompt_from_shot(shot)
            
            # Apply attempt override if provided (for testing retry behavior)
            shot_id = shot["shot_id"]
            override = self.state.shot_attempt_overrides.get(shot_id, 0)
            actual_attempts = self.state.shot_attempts.get(shot_id, 0)
            
            # Initialize shot_attempts for first-time shots
            if shot_id not in self.state.shot_attempts and override == 0:
                self.state.shot_attempts[shot_id] = 1
                actual_attempts = 1
            
            # Total attempt number = override + actual attempts
            # Override doesn't count against budget, only actual_attempts do
            total_attempt = override + actual_attempts
            if total_attempt > 0:
                shot_plan["attempt"] = total_attempt
                shot_plan["version"] = total_attempt
                workflow.logger.info(
                    f"Starting {shot_id} with attempt={total_attempt} (override={override}, actual={actual_attempts})"
                )
            
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[episode_id, shot_plan],
                id=f"{episode_id}-shot-{shot['shot_id']}",
                task_queue=workflow.info().task_queue,
            )
            shot_workflow_handles.append(handle)
            self.shot_results[shot["shot_id"]] = handle
            
            # Start background task to track completion
            task = asyncio.create_task(track_child_completion(shot["shot_id"], handle))
            background_tasks.append(task)
        
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
        
        # Wait for approval with periodic retry completion checking
        while not self.approved_g2_scene_stills:
            await self._check_and_clear_completed_retries()
            try:
                await workflow.wait_condition(
                    lambda: self.approved_g2_scene_stills,
                    timeout=timedelta(seconds=1)
                )
            except asyncio.TimeoutError:
                pass  # Timeout is expected, loop continues
        
        self.passed_gates.append("G2_SCENE_STILLS")
        workflow.logger.info("[APPROVED G2] Scene stills approved")
        
        # Mark all completed shots as approved in the database
        approved_count = await workflow.execute_activity(
            approve_g2_shots,
            args=[episode_id],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=2),
        )
        workflow.logger.info(f"Marked {approved_count} shot(s) as approved after G2")
        
        # Clear hold state after approval (shots will be released to clips)
        self.state.shots_on_hold = []
        
        # Wait (bounded) for any pending retry children to finish before clips
        if self.state.pending_retries:
            try:
                await workflow.wait_condition(
                    lambda: not self.state.pending_retries,
                    timeout=timedelta(seconds=30),
                )
            except asyncio.TimeoutError:
                workflow.logger.warning(
                    f"Timed out waiting for {len(self.state.pending_retries)} retries"
                )
        
        # ================================================================
        # Generate Clips
        # ================================================================
        workflow.logger.info("=== CLIP GENERATION ===")
        self.state.stage = PipelineStage.CLIP_GENERATION
        
        # Signal all shot workflows to proceed with clips
        for shot in shots:
            handle = self.shot_results.get(shot["shot_id"])
            if handle:
                await handle.signal("stills_approved")
        
        workflow.logger.info("Awaiting all clip completions...")
        results = []
        # Use shot_results to get latest handle (includes retries)
        for shot in shots:
            handle = self.shot_results.get(shot["shot_id"])
            if not handle:
                continue
            # Process credit cap raise before each handle check
            while self.state.credit_cap_raised and self.state.new_credit_cap > 0:
                workflow.logger.info(f"Processing credit cap raise to {self.state.new_credit_cap}")
                
                # Update the ledger cap via activity
                await workflow.execute_activity(
                    update_credit_cap_activity,
                    args=[self.episode_id, self.state.new_credit_cap],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                
                # Forward signal to all shot workflows
                for shot_id, shot_handle in self.shot_results.items():
                    try:
                        await shot_handle.signal("resume_after_cap_raise")
                        workflow.logger.info(f"Signaled cap raise to shot {shot_id}")
                    except Exception as e:
                        workflow.logger.warning(f"Failed to signal shot {shot_id}: {e}")
                
                # Clear flag
                self.state.new_credit_cap = 0
            
            result = await handle
            results.append(result)
        
        workflow.logger.info(f"All {len(results)} clips completed")
        
        # ================================================================
        # Edit & Audio (stubbed - from separate PR #8)
        # ================================================================
        workflow.logger.info("=== EDIT & AUDIO ===")
        self.state.stage = PipelineStage.MUTE_EDIT
        
        # Placeholder for edit and audio stages
        workflow.logger.info("[PLACEHOLDER] Edit and audio stages from PR #8")
        
        # ================================================================
        # GATE 3: FINAL CUT
        # ================================================================
        workflow.logger.info("=== GATE 3: FINAL CUT ===")
        self.state.current_gate = "G_FINAL_CUT"
        await workflow.wait_condition(lambda: self.approved_g_final_cut)
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
        ]
        
        try:
            report_result = await workflow.execute_activity(
                "generate_run_report",
                args=[
                    episode_id,
                    False,  # is_canary
                    start_time_dt.isoformat(),
                    end_time_dt.isoformat(),
                    total_credits,  # credits_spent
                    total_credits * 2,  # credits_cap (placeholder)
                    False,  # paused_for_cap
                    len(shots),  # shots_count
                    stages,
                    "complete",  # status
                    None,  # error
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
        """Validator for G1 approval - checks estimates availability in live mode."""
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
    
    @workflow.signal
    async def approve_g2_scene_stills(self):
        """Approve scene stills (G2_SCENE_STILLS): All stills + refs."""
        self.approved_g2_scene_stills = True
    
    @workflow.signal
    async def approve_g_final_cut(self):
        """Approve final cut (G_FINAL_CUT): After edit."""
        self.approved_g_final_cut = True
    
    @workflow.signal
    def raise_credit_cap(self, new_cap: int):
        """
        Raise credit cap and resume paused run.
        
        Signal handler just sets flags - actual work done in main loop.
        """
        self.state.credit_cap_raised = True
        self.state.new_credit_cap = new_cap
    
    async def _check_and_clear_completed_retries(self) -> None:
        """
        Check all pending retries and clear those that completed.
        Called periodically by wait_condition to enable deterministic retry tracking.
        """
        if not self.state.pending_retries:
            return  # Nothing to check
        
        for shot_id in list(self.state.pending_retries.keys()):
            workflow_id = self.state.pending_retries[shot_id]
            handle = workflow.get_external_workflow_handle(workflow_id)
            
            try:
                # Query child to check if still phase is done
                child_state = await handle.query("get_state")
                if child_state.get("still_status") in ("complete", "approved", "failed"):
                    # Still is done, clear pending and record outcome
                    self.state.pending_retries.pop(shot_id, None)
                    workflow.logger.info(f"Retry child {shot_id} still complete, cleared from pending")
                    
                    # Await final result asynchronously and record outcome
                    try:
                        result = await handle
                        if isinstance(result, dict) and result.get("status") == "failed":
                            self.state.failed_shots.append({
                                "shot_id": shot_id,
                                "status": "failed",
                                "reason": result.get("error", "Unknown error"),
                            })
                    except Exception as e:
                        self.state.failed_shots.append({
                            "shot_id": shot_id,
                            "status": "failed",
                            "reason": f"Retry child error: {e}"
                        })
            except:
                # Child query failed, check if workflow terminated
                try:
                    desc = await handle.describe()
                    # If in terminal state, clear and record
                    if desc.status in [2, 3, 4, 5]:  # COMPLETED, FAILED, CANCELED, TERMINATED
                        self.state.pending_retries.pop(shot_id, None)
                        workflow.logger.info(f"Retry child {shot_id} terminated, cleared from pending")
                        self.state.failed_shots.append({
                            "shot_id": shot_id,
                            "status": "failed",
                            "reason": "Retry child terminated"
                        })
                except:
                    pass  # Child might not exist yet
    
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
        
        # Check and clear any completed retries first
        await self._check_and_clear_completed_retries()
        
        # Check retry limit FIRST (so error message is about max retries, not "in progress")
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
        
        # Block retries while a retry is already pending for this shot (after checking max retries)
        if shot_id in self.state.pending_retries:
            workflow.logger.error(f"Cannot retry {shot_id}: retry already pending")
            raise ApplicationError(
                f"Cannot retry {shot_id}: retry already in progress",
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
        # Override doesn't count against retry budget
        override = self.state.shot_attempt_overrides.get(shot_id, 0)
        actual_attempts = self.state.shot_attempts[shot_id]
        total_attempt = override + actual_attempts
        shot_plan["attempt"] = total_attempt
        # Version = shot_attempts (already incremented): attempt 1 -> version 1, retry 1 (attempt 2) -> version 2 (-r1)
        shot_plan["version"] = self.state.shot_attempts[shot_id]
        
        # Build prompt if missing
        if "prompt" not in shot_plan:
            from hfvg.episode_parser import build_prompt_from_shot
            shot_plan["prompt"] = build_prompt_from_shot(shot_plan)
        
        # Launch new still child with NEW idempotency key (include attempt number)
        # Original = attempt 1, first retry = attempt 2 (-r1), second retry = attempt 3 (-r2)
        # Workflow ID uses retry number (attempts - 1): retry1, retry2, etc.
        retry_num = self.state.shot_attempts[shot_id] - 1
        workflow_id = f"{self.state.episode_id}-shot-{shot_id}-retry{retry_num}"
        
        try:
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[self.state.episode_id, shot_plan],
                id=workflow_id,
                task_queue=workflow.info().task_queue,
            )
            
            # Update shot_results to track latest handle
            self.shot_results[shot_id] = handle
            
            # Track pending retry in workflow state for deterministic checking
            # Completion will be checked via _check_and_clear_completed_retries()
            self.state.pending_retries[shot_id] = workflow_id
            
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
        }
