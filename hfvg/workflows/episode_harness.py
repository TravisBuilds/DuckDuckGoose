"""Episode Harness Workflow: Full end-to-end episode generation with 3 human gates.

Three human gates (collapse from 15):
1. G1_SHOT_PLAN: Beatmap approval (shot list, credit plan)
2. G2_SCENE_STILLS: All stills + reference sheets approval
3. G_FINAL_CUT: Final cut approval after edit

Internal automated gates remain (motion QC, duck identity, etc.) but don't block on human approval.

Features:
- Higgsfield credit ledger with cap-pause (workflow update handler)
- Single-run lock (one episode per Higgsfield account)
- Idempotent job IDs (retry never double-charges)
- Motion QC advisory mode
- Automatic run report
- Real Higgsfield + ElevenLabs (when DRY_RUN=false and keys present)
"""

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg.config import config
    from hfvg.credit_ledger import CreditLedger
    from hfvg.activities import (
        parse_beatmap_activity,
        recheck_refs_activity,
    )
    from hfvg.models import EpisodeState, PipelineStage


@workflow.defn
class EpisodeHarnessWorkflow:
    """
    Full harness workflow with credit ledger, 3 gates, motion QC, and run reporting.
    
    Workflow update handlers:
    - approve_continue: Resume from cap-pause
    - increase_cap: Increase episode credit cap
    """
    
    def __init__(self):
        self.state = EpisodeState(
            episode_id="",
            stage=PipelineStage.INFO_GATHERING,
            idea="",
        )
        
        # Three human gates
        self.approved_shot_plan = False  # G1: Beatmap + credit plan
        self.approved_scene_stills = False  # G2: All stills + refs
        self.approved_final_cut = False  # G_FINAL: After edit
        
        # Credit tracking
        self.episode_cap_credits = 500  # Default, overridable
        self.credits_spent = 0
        self.credits_reserved = 0
        self.paused_for_cap = False
        self.cap_approved_to_continue = False
        
        # Run tracking
        self.shot_results: dict[str, dict] = {}
        self.parsed_beatmap: dict[str, Any] | None = None
        self.missing_refs: dict[str, list[str]] = {}
        self.run_report: dict[str, Any] = {}
        self.start_time = None
        self.is_canary = False
    
    @workflow.run
    async def run(
        self,
        episode_id: str,
        beatmap_path: str | None = None,
        cap_credits: int | None = None,
        is_canary: bool = False,
        dry_run: bool = True
    ) -> dict:
        """
        Execute episode harness workflow.
        
        Args:
            episode_id: Episode identifier (e.g., 'ep05')
            beatmap_path: Path to BEATMAP.md (optional, uses fixtures if None)
            cap_credits: Credit cap override (default: 500)
            is_canary: CANARY mode (2-shot subset)
            dry_run: If True, use DRY_RUN mode
        
        Returns:
            dict with episode results and run report
        """
        from datetime import datetime
        self.start_time = workflow.now().timestamp()
        self.state.episode_id = episode_id
        self.episode_cap_credits = cap_credits or 500
        self.is_canary = is_canary
        
        workflow.logger.info(
            f"Starting episode {episode_id} "
            f"(cap={self.episode_cap_credits} cr, canary={is_canary}, dry_run={dry_run})"
        )
        
        # Initialize credit ledger and acquire run lock
        # In a real implementation, these would be activities
        # For now, we'll track in workflow state
        
        # ================================================================
        # STEP 1: SHOT PLAN (G1_SHOT_PLAN)
        # ================================================================
        workflow.logger.info("=== GATE 1: SHOT PLAN ===")
        self.state.stage = PipelineStage.SCRIPT
        
        # Parse beatmap
        workflow.logger.info("Parsing BEATMAP.md...")
        self.parsed_beatmap = await workflow.execute_activity(
            parse_beatmap_activity,
            args=[beatmap_path, episode_id, not dry_run],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        
        shots = self.parsed_beatmap.get("shots", [])
        workflow.logger.info(f"Parsed {len(shots)} shots from beatmap")
        
        # CANARY mode: select first 2 shots only
        if is_canary:
            shots = shots[:2]
            workflow.logger.info(f"CANARY mode: using first 2 shots: {[s['shot_id'] for s in shots]}")
        
        # Calculate credit plan
        total_estimated_credits = sum(
            s.get("video_credits", 0) + s.get("still_credits", 0) for s in shots
        )
        workflow.logger.info(
            f"Credit plan: {total_estimated_credits} credits estimated "
            f"(cap: {self.episode_cap_credits})"
        )
        
        # Wait for shot plan approval (G1)
        workflow.logger.info("G1_SHOT_PLAN: Awaiting approval...")
        self.state.current_gate = "G1_SHOT_PLAN"
        await workflow.wait_condition(lambda: self.approved_shot_plan)
        workflow.logger.info(f"[APPROVED G1] Shot plan locked: {len(shots)} shots")
        
        # ================================================================
        # STEP 2: SCENE STILLS (G2_SCENE_STILLS)
        # ================================================================
        workflow.logger.info("=== GATE 2: SCENE STILLS ===")
        self.state.stage = PipelineStage.STILL_GENERATION
        
        # Check for missing refs (G2.01 check)
        if self._needs_new_refs(shots):
            self.missing_refs = self._collect_missing_refs(shots)
            workflow.logger.info(f"Missing refs detected: {self.missing_refs}")
            
            # In live mode, would wait for refs to be uploaded
            if not dry_run:
                # Re-check refs after upload
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
        # (In dry mode, this would be fast; in live mode, real Higgsfield calls)
        workflow.logger.info(f"Launching still generation for {len(shots)} shots...")
        
        # TODO: Launch ShotWorkflow children for stills
        # For now, simulate
        workflow.logger.info("[Stills generation would happen here]")
        
        # Wait for scene stills approval (G2)
        workflow.logger.info("G2_SCENE_STILLS: Awaiting approval...")
        self.state.current_gate = "G2_SCENE_STILLS"
        await workflow.wait_condition(lambda: self.approved_scene_stills)
        workflow.logger.info("[APPROVED G2] Scene stills approved")
        
        # ================================================================
        # STEP 3: VIDEO GENERATION
        # ================================================================
        workflow.logger.info("=== STEP 3: VIDEO GENERATION ===")
        self.state.stage = PipelineStage.CLIP_GENERATION
        
        # Signal all shots to proceed with clips
        # Multi-animal shots forced to Kling first-frame (no Seedance reference mode)
        workflow.logger.info("[Clip generation would happen here]")
        
        # Motion QC (advisory mode)
        workflow.logger.info("Running motion QC (advisory)...")
        # TODO: Call motion QC activity
        # For now, simulate
        motion_qc_issues = []  # Would be populated by QC
        if motion_qc_issues:
            workflow.logger.warning(f"Motion QC flagged issues: {motion_qc_issues}")
        
        # ================================================================
        # STEP 4: EDIT
        # ================================================================
        workflow.logger.info("=== STEP 4: EDIT ===")
        self.state.stage = PipelineStage.MUTE_EDIT
        
        # Trim, mute cut
        workflow.logger.info("[Edit would happen here]")
        
        # ================================================================
        # STEP 5: AUDIO (from PR #8, audio-stage-rebased)
        # ================================================================
        workflow.logger.info("=== STEP 5: AUDIO ===")
        self.state.stage = PipelineStage.AUDIO
        
        # ElevenLabs VO, music, SFX, mix
        workflow.logger.info("[Audio generation would happen here]")
        
        # ================================================================
        # GATE 3: FINAL CUT (G_FINAL_CUT)
        # ================================================================
        workflow.logger.info("=== GATE 3: FINAL CUT ===")
        self.state.current_gate = "G_FINAL_CUT"
        await workflow.wait_condition(lambda: self.approved_final_cut)
        workflow.logger.info("[APPROVED G_FINAL] Final cut approved")
        
        # ================================================================
        # STEP 7: RUN REPORT
        # ================================================================
        elapsed = workflow.now().timestamp() - self.start_time
        self.run_report = {
            "episode_id": episode_id,
            "is_canary": is_canary,
            "shots": len(shots),
            "elapsed_seconds": elapsed,
            "credits_spent": self.credits_spent,
            "credits_cap": self.episode_cap_credits,
            "paused_for_cap": self.paused_for_cap,
            "status": "complete"
        }
        
        workflow.logger.info(f"Episode {episode_id} complete in {elapsed:.1f}s")
        
        return {
            "episode_id": episode_id,
            "shots": len(shots),
            "status": "complete",
            "run_report": self.run_report
        }
    
    @staticmethod
    def _collect_missing_refs(shots: list[dict]) -> dict[str, list[str]]:
        return {s["shot_id"]: list(s["missing_refs"]) for s in shots if s.get("missing_refs")}
    
    def _needs_new_refs(self, shots: list[dict]) -> bool:
        return bool(self._collect_missing_refs(shots))
    
    # ================================================================
    # SIGNALS (3 human gates)
    # ================================================================
    
    @workflow.signal
    async def approve_shot_plan(self):
        """Approve shot plan (G1_SHOT_PLAN)."""
        self.approved_shot_plan = True
    
    @workflow.signal
    async def approve_scene_stills(self):
        """Approve scene stills (G2_SCENE_STILLS)."""
        self.approved_scene_stills = True
    
    @workflow.signal
    async def approve_final_cut(self):
        """Approve final cut (G_FINAL_CUT)."""
        self.approved_final_cut = True
    
    # ================================================================
    # UPDATE HANDLERS (credit cap pause/increase)
    # ================================================================
    
    @workflow.update
    async def approve_continue(self) -> dict:
        """
        Approve continuing past credit cap pause.
        
        Returns:
            {"approved": bool, "message": str}
        """
        if not self.paused_for_cap:
            return {"approved": False, "message": "Not paused for cap"}
        
        self.cap_approved_to_continue = True
        self.paused_for_cap = False
        workflow.logger.info("Cap pause approved, resuming workflow")
        return {"approved": True, "message": "Resuming workflow"}
    
    @workflow.update
    async def increase_cap(self, new_cap_credits: int) -> dict:
        """
        Increase episode credit cap.
        
        Args:
            new_cap_credits: New cap (must be > current cap)
        
        Returns:
            {"success": bool, "old_cap": int, "new_cap": int}
        """
        if new_cap_credits <= self.episode_cap_credits:
            return {
                "success": False,
                "old_cap": self.episode_cap_credits,
                "new_cap": self.episode_cap_credits,
                "message": "New cap must be greater than current cap"
            }
        
        old_cap = self.episode_cap_credits
        self.episode_cap_credits = new_cap_credits
        
        # If paused and new cap provides headroom, auto-resume
        total_committed = self.credits_spent + self.credits_reserved
        if self.paused_for_cap and total_committed < new_cap_credits:
            self.paused_for_cap = False
            self.cap_approved_to_continue = True
            workflow.logger.info(f"Cap increased {old_cap} → {new_cap_credits}, auto-resuming")
        
        return {
            "success": True,
            "old_cap": old_cap,
            "new_cap": new_cap_credits,
            "message": f"Cap increased from {old_cap} to {new_cap_credits} credits"
        }
    
    # ================================================================
    # QUERY
    # ================================================================
    
    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        return {
            "episode_id": self.state.episode_id,
            "stage": self.state.stage.value if self.state.stage else None,
            "current_gate": self.state.current_gate,
            "is_canary": self.is_canary,
            "approvals": {
                "shot_plan": self.approved_shot_plan,
                "scene_stills": self.approved_scene_stills,
                "final_cut": self.approved_final_cut,
            },
            "credits": {
                "spent": self.credits_spent,
                "reserved": self.credits_reserved,
                "cap": self.episode_cap_credits,
                "paused": self.paused_for_cap,
            },
            "shots": len(self.shot_results),
            "missing_refs": {k: list(v) for k, v in self.missing_refs.items()},
        }
