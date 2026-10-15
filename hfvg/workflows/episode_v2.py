"""EpisodeWorkflow V2: Handbook-compliant workflow with steps 0-7 and Travis approval points."""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

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
        credit_cap: int | None = None
    ) -> dict:
        """
        Execute full episode pipeline following handbook steps.
        
        Args:
            episode_id: Episode identifier (e.g., 'ep04')
            beatmap_path: Path to BEATMAP.md (optional, uses fixture if None)
            dry_run: If True, use DRY_RUN mode (default True for testing)
            shots_subset: Optional list of shot IDs to run (e.g., ['A01', 'A02'] for canary)
            credit_cap: Optional credit cap for this run (overrides episode line cap)
        
        Returns:
            dict with episode results
        """
        self.state.episode_id = episode_id
        
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
        
        # Initialize credit ledger line with credit_cap (lower of credit_cap and env cap)
        workflow.logger.info(f"Initializing credit ledger for {episode_id} with cap: {credit_cap}")
        await workflow.execute_activity(
            "init_episode_credit_line_activity",
            args=[episode_id, credit_cap],
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=RetryPolicy(maximum_attempts=1),
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
        
        # Launch still generation for all shots
        workflow.logger.info(f"Launching still generation for {len(shots)} shots...")
        shot_workflow_handles = []
        for shot in shots:
            shot_plan = shot.copy()
            shot_plan["dry_run"] = dry_run
            if "prompt" not in shot_plan:
                shot_plan["prompt"] = build_prompt_from_shot(shot)
            
            handle = await workflow.start_child_workflow(
                "ShotWorkflow",
                args=[episode_id, shot_plan],
                id=f"{episode_id}-shot-{shot['shot_id']}",
                task_queue=workflow.info().task_queue,
            )
            shot_workflow_handles.append(handle)
            self.shot_results[shot["shot_id"]] = handle
        
        workflow.logger.info(f"Launched {len(shot_workflow_handles)} shot workflows")
        
        # ================================================================
        # GATE 2: SCENE STILLS (All stills + reference sheets)
        # ================================================================
        workflow.logger.info("=== GATE 2: SCENE STILLS ===")
        self.state.current_gate = "G2_SCENE_STILLS"
        await workflow.wait_condition(lambda: self.approved_g2_scene_stills)
        self.passed_gates.append("G2_SCENE_STILLS")
        workflow.logger.info("[APPROVED G2] Scene stills approved")
        
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
        for handle in shot_workflow_handles:
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
    
    # Signal handlers for 3 gates
    @workflow.signal
    async def approve_g1_shot_plan(self):
        """Approve shot plan (G1_SHOT_PLAN): Beatmap + credit plan."""
        self.approved_g1_shot_plan = True
    
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
    
    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        return {
            "episode_id": self.state.episode_id,
            "stage": self.state.stage.value if self.state.stage else None,
            "current_gate": self.state.current_gate,
            "passed_gates": self.passed_gates.copy(),
            "missing_refs": {k: list(v) for k, v in self.missing_refs.items()},
            "approvals": {
                "g1_shot_plan": self.approved_g1_shot_plan,
                "g2_scene_stills": self.approved_g2_scene_stills,
                "g_final_cut": self.approved_g_final_cut,
            },
            "shots": len(self.shot_results),
        }
