"""EpisodeWorkflow V2: Handbook-compliant workflow with steps 0-7 and Travis approval points."""

from datetime import timedelta
from typing import Any

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from hfvg import budget, config  # Non-deterministic modules with os.getenv
    from hfvg.activities import (
        generate_music,
        generate_sfx,
        generate_voiceover,
        load_gate_policy_activity,
        mix_audio,
        parse_beatmap_activity,
        render_edit,
        trim_clips,
    )
    from hfvg.models import EpisodeState, PipelineStage
    from hfvg.workflows.posting import PostingWorkflow
    from hfvg.workflows.shot import ShotWorkflow


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
        
        # Travis approval flags (15 points)
        self.approved_gx01 = False  # External actions (default HOLD)
        self.approved_gc02 = False  # Budget tracking OK
        self.approved_gc06 = False  # Line move reported
        self.approved_g101 = False  # Pitch pick
        self.approved_g103 = False  # Beatmap
        self.approved_g108 = False  # Credit plan
        self.approved_g201 = False  # New refs approved
        self.approved_g212 = False  # Still strip approved
        self.approved_g406 = False  # Cut-for-story flags reviewed
        self.approved_g408 = False  # Mute notes logged
        self.approved_g409 = False  # Picture lock
        self.approved_g501 = False  # VO script + music lock
        self.approved_g610 = False  # Final audio approval
        self.approved_g702 = False  # Drive upload OK
        self.approved_g703 = False  # Handoff package OK
        
        self.shot_results: dict[str, dict] = {}
        self.parsed_beatmap: dict[str, Any] | None = None
    
    @workflow.run
    async def run(self, episode_id: str, beatmap_path: str | None = None, 
                 dry_run: bool = True) -> dict:
        """
        Execute full episode pipeline following handbook steps.
        
        Args:
            episode_id: Episode identifier (e.g., 'ep04')
            beatmap_path: Path to BEATMAP.md (optional, uses fixture if None)
            dry_run: If True, use DRY_RUN mode (default True for testing)
        
        Returns:
            dict with episode results
        """
        self.state.episode_id = episode_id
        
        # Load gate policy via activity (not deterministic in workflow)
        policy_data = await workflow.execute_activity(
            load_gate_policy_activity,
            args=["mid-mountain-rest"],
            start_to_close_timeout=timedelta(seconds=10),
        )
        # Store policy data as dict (we only need the data, not GatePolicy methods)
        self.policy = policy_data
        
        workflow.logger.info(f"Starting episode {episode_id} (dry_run={dry_run})")
        
        # Step 0: Bible (asset registry, playbook)
        workflow.logger.info("=== STEP 0: BIBLE ===")
        self.state.stage = PipelineStage.CHARACTER_LOCKS
        workflow.logger.info("Loading asset registry and playbook...")
        # TODO: Wire AssetRegistry and PlaybookValidator
        
        # Step 1: Story (pitch → beatmap → credit plan)
        workflow.logger.info("=== STEP 1: STORY ===")
        
        # G1.01: Pitch pick
        workflow.logger.info("G1.01: Awaiting pitch pick...")
        await workflow.wait_condition(lambda: self.approved_g101)
        workflow.logger.info(f"[APPROVED G1.01] Pitch picked for {episode_id}")
        
        # Parse beatmap via activity (filesystem I/O not allowed in workflow)
        workflow.logger.info("Parsing BEATMAP.md...")
        self.state.stage = PipelineStage.SCRIPT
        
        self.parsed_beatmap = await workflow.execute_activity(
            parse_beatmap_activity,
            args=[beatmap_path],
            start_to_close_timeout=timedelta(seconds=30),
        )
        
        shots = self.parsed_beatmap.get("shots", [])
        workflow.logger.info(f"Parsed {len(shots)} shots from beatmap")
        
        # G1.03: Beatmap approval
        workflow.logger.info("G1.03: Awaiting beatmap approval...")
        await workflow.wait_condition(lambda: self.approved_g103)
        workflow.logger.info(f"[APPROVED G1.03] Beatmap locked: {len(shots)} shots")
        
        # G1.08: Credit plan approval (check DB for single source of truth)
        workflow.logger.info("G1.08: Awaiting credit plan approval...")
        await workflow.wait_condition(lambda: self.approved_g108)
        
        # Verify approval in DB before proceeding (fail-closed)
        from hfvg.activities.studio_generation import check_live_mode_and_g108
        _, g108_db = await workflow.execute_activity(
            check_live_mode_and_g108,
            args=[self.state.episode_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        if not g108_db:
            raise RuntimeError("G1.08 approval signal received but DB shows not approved (fail-closed)")
        
        workflow.logger.info("[APPROVED G1.08] Credit plan locked (DB verified)")
        
        # GC.02: Budget tracking check
        workflow.logger.info("GC.02: Awaiting budget tracking confirmation...")
        await workflow.wait_condition(lambda: self.approved_gc02)
        workflow.logger.info("[APPROVED GC.02] Budget tracking active")
        
        # Step 2: Stills generation
        workflow.logger.info("=== STEP 2: STILLS ===")
        self.state.stage = PipelineStage.STILL_GENERATION
        
        # G2.01: New refs approval (if needed)
        if self._needs_new_refs(shots):
            workflow.logger.info("G2.01: Awaiting new reference approval...")
            await workflow.wait_condition(lambda: self.approved_g201)
            workflow.logger.info("[APPROVED G2.01] New refs approved")
        
        # Launch still generation for all shots
        workflow.logger.info(f"Launching still generation for {len(shots)} shots...")
        shot_workflow_handles = []
        for shot in shots:
            handle = await workflow.start_child_workflow(
                ShotWorkflow.run,
                args=[episode_id, shot],  # Fixed: 2 args not 3
                id=f"{episode_id}-shot-{shot['shot_id']}",
                task_queue=workflow.info().task_queue,
            )
            shot_workflow_handles.append(handle)
            self.shot_results[shot["shot_id"]] = handle
        
        workflow.logger.info(f"Launched {len(shot_workflow_handles)} shot workflows")
        
        # G2.12: Still strip approval
        workflow.logger.info("G2.12: Awaiting still strip approval...")
        await workflow.wait_condition(lambda: self.approved_g212)
        workflow.logger.info("[APPROVED G2.12] Still strip approved")
        
        # Step 3: Clips generation
        workflow.logger.info("=== STEP 3: VIDEO ===")
        self.state.stage = PipelineStage.CLIP_GENERATION
        
        # Signal all shot workflows to proceed with clips
        for shot in shots:
            handle = self.shot_results.get(shot["shot_id"])
            if handle:
                await handle.signal("stills_approved")
        
        workflow.logger.info("Awaiting all clip completions...")
        results = []
        for handle in shot_workflow_handles:
            result = await handle
            results.append(result)
        
        workflow.logger.info(f"All {len(results)} clips completed")
        
        # Step 4: Mute (trim, QC, picture lock)
        workflow.logger.info("=== STEP 4: MUTE ===")
        self.state.stage = PipelineStage.VIDEO_EDITING
        
        # G4.06: Cut-for-story flags
        workflow.logger.info("G4.06: Awaiting cut-for-story review...")
        await workflow.wait_condition(lambda: self.approved_g406)
        workflow.logger.info("[APPROVED G4.06] Cut-for-story reviewed")
        
        # Render mute cut
        workflow.logger.info("Rendering mute cut...")
        if dry_run:
            mute_path = f"/tmp/{episode_id}_mute_dry.mp4"
            workflow.logger.info(f"[DRY-RUN] Mute would be at {mute_path}")
        else:
            # Real mute rendering
            raise NotImplementedError("Real mute rendering not implemented")
        
        # G4.08: Mute review notes
        workflow.logger.info("G4.08: Awaiting mute review notes...")
        await workflow.wait_condition(lambda: self.approved_g408)
        workflow.logger.info("[APPROVED G4.08] Mute notes logged")
        
        # G4.09: Picture lock
        workflow.logger.info("G4.09: Awaiting picture lock...")
        await workflow.wait_condition(lambda: self.approved_g409)
        workflow.logger.info("[APPROVED G4.09] ⚠️ PICTURE LOCK - no more picture changes")
        
        # Step 5: Script lock
        workflow.logger.info("=== STEP 5: SCRIPT ===")
        self.state.stage = PipelineStage.SCRIPT
        
        # G5.01: VO script + music lock
        workflow.logger.info("G5.01: Awaiting VO script and music lock...")
        await workflow.wait_condition(lambda: self.approved_g501)
        workflow.logger.info("[APPROVED G5.01] VO script and music locked")
        
        # Step 6: Audio (VO, music, SFX, mix)
        workflow.logger.info("=== STEP 6: AUDIO ===")
        self.state.stage = PipelineStage.AUDIO_GENERATION
        
        workflow.logger.info("Audio generation (stubbed for Slice 2)...")
        if not dry_run:
            raise NotImplementedError("ElevenLabs integration not implemented")
        
        # G6.10: Final audio approval
        workflow.logger.info("G6.10: Awaiting final audio approval...")
        await workflow.wait_condition(lambda: self.approved_g610)
        workflow.logger.info("[APPROVED G6.10] Final audio approved")
        
        # Step 7: Delivery (HOLD by default per GX.01)
        workflow.logger.info("=== STEP 7: DELIVERY ===")
        self.state.stage = PipelineStage.POSTING
        
        # GX.01: External actions HOLD by default
        workflow.logger.info("GX.01: Episode ready for delivery (HOLD - never auto-post)")
        workflow.logger.info("Awaiting explicit GX.01 approval for external actions...")
        await workflow.wait_condition(lambda: self.approved_gx01)
        workflow.logger.info("[APPROVED GX.01] External actions authorized")
        
        # G7.02: Drive upload
        workflow.logger.info("G7.02: Awaiting Drive upload confirmation...")
        await workflow.wait_condition(lambda: self.approved_g702)
        workflow.logger.info("[APPROVED G7.02] Drive upload complete")
        
        # G7.03: Grower handoff package
        workflow.logger.info("G7.03: Preparing Grower handoff package...")
        handoff_stub = {
            "episode_id": episode_id,
            "final_path": f"/tmp/{episode_id}_final.mp4",
            "review_copy_path": f"/tmp/{episode_id}_review.mp4",
            "subs_en_path": f"/tmp/{episode_id}_en.srt",
            "subs_jp_path": f"/tmp/{episode_id}_jp.srt",
            "status": "HOLD - awaiting Grower pickup"
        }
        
        workflow.logger.info("G7.03: Awaiting handoff package approval...")
        await workflow.wait_condition(lambda: self.approved_g703)
        workflow.logger.info("[APPROVED G7.03] Grower handoff package ready")
        
        workflow.logger.info(f"✅ Episode {episode_id} complete (HOLD state)")
        
        return {
            "episode_id": episode_id,
            "shots": len(shots),
            "status": "complete",
            "handoff": handoff_stub,
        }
    
    def _needs_new_refs(self, shots: list[dict]) -> bool:
        """Check if episode needs new reference images (simplified)."""
        # In real impl, check if any shot has new characters or props
        return False  # For now, assume no new refs needed
    
    # Signal handlers for Travis approvals
    @workflow.signal
    async def approve_gx01(self):
        """Approve external actions (GX.01)."""
        self.approved_gx01 = True
    
    @workflow.signal
    async def approve_gc02(self):
        """Confirm budget tracking (GC.02)."""
        self.approved_gc02 = True
    
    @workflow.signal
    async def approve_gc06(self):
        """Acknowledge line move report (GC.06)."""
        self.approved_gc06 = True
    
    @workflow.signal
    async def approve_g101(self):
        """Approve pitch pick (G1.01)."""
        self.approved_g101 = True
    
    @workflow.signal
    async def approve_g103(self):
        """Approve beatmap (G1.03)."""
        self.approved_g103 = True
    
    @workflow.signal
    async def approve_g108(self):
        """Approve credit plan (G1.08)."""
        self.approved_g108 = True
    
    @workflow.signal
    async def approve_g201(self):
        """Approve new refs (G2.01)."""
        self.approved_g201 = True
    
    @workflow.signal
    async def approve_g212(self):
        """Approve still strip (G2.12)."""
        self.approved_g212 = True
    
    @workflow.signal
    async def approve_g406(self):
        """Approve cut-for-story review (G4.06)."""
        self.approved_g406 = True
    
    @workflow.signal
    async def approve_g408(self):
        """Log mute review notes (G4.08)."""
        self.approved_g408 = True
    
    @workflow.signal
    async def approve_g409(self):
        """Lock picture (G4.09)."""
        self.approved_g409 = True
    
    @workflow.signal
    async def approve_g501(self):
        """Lock VO script and music (G5.01)."""
        self.approved_g501 = True
    
    @workflow.signal
    async def approve_g610(self):
        """Approve final audio (G6.10)."""
        self.approved_g610 = True
    
    @workflow.signal
    async def approve_g702(self):
        """Confirm Drive upload (G7.02)."""
        self.approved_g702 = True
    
    @workflow.signal
    async def approve_g703(self):
        """Approve handoff package (G7.03)."""
        self.approved_g703 = True
    
    @workflow.query
    def get_state(self) -> dict:
        """Get current workflow state."""
        return {
            "episode_id": self.state.episode_id,
            "stage": self.state.stage.value if self.state.stage else None,
            "approvals": {
                "gx01": self.approved_gx01,
                "gc02": self.approved_gc02,
                "g101": self.approved_g101,
                "g103": self.approved_g103,
                "g108": self.approved_g108,
                "g201": self.approved_g201,
                "g212": self.approved_g212,
                "g406": self.approved_g406,
                "g408": self.approved_g408,
                "g409": self.approved_g409,
                "g501": self.approved_g501,
                "g610": self.approved_g610,
                "g702": self.approved_g702,
                "g703": self.approved_g703,
            },
            "shots": len(self.shot_results),
        }
