"""EpisodeWorkflow: main workflow orchestrating all pipeline stages."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from hfvg.activities import (
        generate_music,
        generate_sfx,
        generate_voiceover,
        mix_audio,
        render_edit,
        trim_clips,
    )
    from hfvg.models import EpisodeState, PipelineStage, ScenePlan, Storyboard
    from hfvg.workflows.posting import PostingWorkflow
    from hfvg.workflows.shot import ShotWorkflow


@workflow.defn
class EpisodeWorkflow:
    """
    Main workflow for a video episode.

    Covers all pipeline stages from idea gathering through posting.
    """

    def __init__(self):
        self.state = EpisodeState(
            episode_id="",
            stage=PipelineStage.INFO_GATHERING,
            idea="",
        )
        self.readback_approved = False
        self.character_locks_approved = False
        self.storyboard_approved = False
        self.scenes_approved: set[int] = set()
        self.final_approved = False
        self.shot_results: dict[str, dict] = {}

    @workflow.run
    async def run(self, episode_id: str, idea: str, platforms: list[str] = None) -> dict:
        """
        Execute full episode pipeline.

        Args:
            episode_id: Unique episode identifier
            idea: User's video idea
            platforms: Target platforms (default: ["instagram"])

        Returns:
            dict with episode results
        """
        self.state.episode_id = episode_id
        self.state.idea = idea
        platforms = platforms or ["instagram"]

        workflow.logger.info(f"Starting episode {episode_id}: {idea}")

        self.state.stage = PipelineStage.INFO_GATHERING
        workflow.logger.info("Stage: Information gathering (stubbed)")

        self.state.stage = PipelineStage.READBACK
        workflow.logger.info("Stage: Awaiting read-back approval")
        await workflow.wait_condition(lambda: self.readback_approved)
        workflow.logger.info("Read-back approved")

        self.state.stage = PipelineStage.SCRIPT
        workflow.logger.info("Stage: Script writing (stubbed)")

        self.state.stage = PipelineStage.CHARACTER_LOCKS
        workflow.logger.info("Stage: Character locks - generating turnarounds (stubbed)")
        
        workflow.logger.info("Awaiting character lock approvals")
        await workflow.wait_condition(lambda: self.character_locks_approved)
        workflow.logger.info("Character locks approved, writing to registry")

        self.state.stage = PipelineStage.STORYBOARD
        workflow.logger.info("Stage: Storyboard drafting (stubbed)")

        storyboard = Storyboard(
            scenes=[
                ScenePlan(
                    scene_id=1,
                    description="Opening scene",
                    shots=[
                        {
                            "shot_id": f"{episode_id}-s01-shot01",
                            "scene_id": 1,
                            "description": "Wide establishing shot",
                            "prompt": "A cozy kitchen with warm lighting",
                            "refs": [],
                            "estimated_cost": 35.0,
                        }
                    ],
                ),
                ScenePlan(
                    scene_id=2,
                    description="Action scene",
                    shots=[
                        {
                            "shot_id": f"{episode_id}-s02-shot01",
                            "scene_id": 2,
                            "description": "Close-up action",
                            "prompt": "Character demonstrating technique",
                            "refs": [],
                            "estimated_cost": 35.0,
                        }
                    ],
                ),
            ],
            total_estimated_cost=140.0,
        )

        self.state.storyboard = storyboard
        workflow.logger.info(f"Storyboard created: {len(storyboard.scenes)} scenes")

        await workflow.wait_condition(lambda: self.storyboard_approved)
        workflow.logger.info("Storyboard approved")

        self.state.stage = PipelineStage.SHOT_PLANNING
        workflow.logger.info("Stage: Shot planning (storyboard → shot plans)")

        all_shots = []
        for scene in storyboard.scenes:
            all_shots.extend(scene.shots)

        workflow.logger.info(f"Total shots to generate: {len(all_shots)}")

        self.state.stage = PipelineStage.STILL_GENERATION
        workflow.logger.info("Stage: Still generation (fan-out to ShotWorkflows)")

        shot_workflow_handles = []
        for shot in all_shots:
            # Handle both dict and Pydantic model formats
            shot_dict = shot if isinstance(shot, dict) else shot.model_dump()
            
            # Start child workflow without awaiting (for parallelism)
            handle = await workflow.start_child_workflow(
                ShotWorkflow.run,
                args=[episode_id, shot_dict],
                id=f"{episode_id}-shot-{shot_dict['shot_id']}",
                task_queue=workflow.info().task_queue,
            )
            shot_workflow_handles.append(handle)
            self.shot_results[shot_dict["shot_id"]] = handle

        workflow.logger.info(f"Launched {len(shot_workflow_handles)} shot workflows")

        scene_ids = {(shot if isinstance(shot, dict) else shot.model_dump())["scene_id"] for shot in all_shots}
        for scene_id in sorted(scene_ids):
            workflow.logger.info(f"Awaiting approval for scene {scene_id}")
            await workflow.wait_condition(lambda sid=scene_id: sid in self.scenes_approved)
            workflow.logger.info(f"Scene {scene_id} approved")

        workflow.logger.info("All scene stills approved")

        self.state.stage = PipelineStage.CLIP_GENERATION
        workflow.logger.info("Stage: Clip generation (handled in ShotWorkflows)")

        # Signal all child ShotWorkflows for their respective scenes to proceed
        for scene_id in sorted(scene_ids):
            # Find all shot workflows for this scene and signal them
            for shot in all_shots:
                shot_dict = shot if isinstance(shot, dict) else shot.model_dump()
                if shot_dict["scene_id"] == scene_id:
                    shot_handle = self.shot_results.get(shot_dict["shot_id"])
                    if shot_handle:
                        await shot_handle.signal("scene_approved")

        # Await all child workflow results (handle is directly awaitable)
        results = []
        for handle in shot_workflow_handles:
            result = await handle
            results.append(result)
        
        workflow.logger.info(f"All shots completed: {len(results)} results")

        completed_shots = [r for r in results if r.get("status") == "completed"]
        workflow.logger.info(f"{len(completed_shots)}/{len(results)} shots succeeded")

        if len(completed_shots) < len(results) * 0.8:
            workflow.logger.error("Too many shot failures, aborting episode")
            return {
                "episode_id": episode_id,
                "status": "failed",
                "error": "Insufficient successful shots",
            }

        self.state.stage = PipelineStage.MUTE_EDIT
        workflow.logger.info("Stage: Mute edit (trim + transitions)")

        clip_urls = [r["clip_url"] for r in completed_shots]

        trimmed_url = await workflow.execute_activity(
            trim_clips,
            args=[clip_urls, {}],
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
        )

        self.state.stage = PipelineStage.PICTURE_LOCK
        workflow.logger.info("Stage: Picture lock (render edit)")

        picture_lock_url = await workflow.execute_activity(
            render_edit,
            args=[trimmed_url, {}],
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
        )

        self.state.stage = PipelineStage.AUDIO
        workflow.logger.info("Stage: Audio generation (VO + SFX + music)")

        vo_url = await workflow.execute_activity(
            generate_voiceover,
            args=["Episode voiceover script", "voice-default"],
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=1),
        )

        sfx_url = await workflow.execute_activity(
            generate_sfx,
            args=["Ambient kitchen sounds"],
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=1),
        )

        music_url = await workflow.execute_activity(
            generate_music,
            args=["upbeat acoustic", 60.0],
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(minutes=1),
        )

        self.state.stage = PipelineStage.FINAL_MIX
        workflow.logger.info("Stage: Final mix")

        final_url = await workflow.execute_activity(
            mix_audio,
            args=[picture_lock_url, [vo_url, sfx_url, music_url]],
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(minutes=2),
        )

        workflow.logger.info(f"Final video ready: {final_url}")

        await workflow.wait_condition(lambda: self.final_approved)
        workflow.logger.info("Final video approved")

        self.state.stage = PipelineStage.POST_APPROVAL
        workflow.logger.info("Stage: Post approval queue")

        posting_result = await workflow.execute_child_workflow(
            PostingWorkflow.run,
            args=[episode_id, final_url, platforms],
            id=f"{episode_id}-posting",
            task_queue=workflow.info().task_queue,
        )

        workflow.logger.info(f"Posting completed: {posting_result}")

        self.state.stage = PipelineStage.COMPLETE
        workflow.logger.info(f"Episode {episode_id} complete")

        return {
            "episode_id": episode_id,
            "status": "completed",
            "final_url": final_url,
            "posts": posting_result.get("posts", {}),
            "shots_completed": len(completed_shots),
            "shots_total": len(all_shots),
        }

    @workflow.update
    async def approve_readback(self) -> str:
        """Approve read-back summary."""
        self.readback_approved = True
        workflow.logger.info("Read-back approved via update")
        return "approved"

    @workflow.update
    async def approve_character_locks(self) -> str:
        """Approve character locks and write to registry."""
        self.character_locks_approved = True
        workflow.logger.info("Character locks approved via update")
        return "approved"

    @workflow.update
    async def approve_storyboard(self) -> str:
        """Approve storyboard and cost estimate."""
        self.storyboard_approved = True
        workflow.logger.info("Storyboard approved via update")
        return "approved"

    @workflow.update
    async def approve_scene_stills(self, scene_id: int) -> str:
        """Approve stills for a scene."""
        self.scenes_approved.add(scene_id)
        workflow.logger.info(f"Scene {scene_id} stills approved via update")
        return "approved"

    @workflow.update
    async def approve_final(self) -> str:
        """Approve final video."""
        self.final_approved = True
        workflow.logger.info("Final video approved via update")
        return "approved"

    @workflow.query
    def get_state(self) -> dict:
        """Query current episode state."""
        return {
            "episode_id": self.state.episode_id,
            "stage": self.state.stage.value,
            "idea": self.state.idea,
            "readback_approved": self.readback_approved,
            "character_locks_approved": self.character_locks_approved,
            "storyboard_approved": self.storyboard_approved,
            "scenes_approved": list(self.scenes_approved),
            "final_approved": self.final_approved,
            "shot_count": len(self.shot_results),
        }

    @workflow.query
    def get_storyboard(self) -> dict:
        """Query storyboard."""
        if self.state.storyboard:
            return self.state.storyboard.model_dump()
        return {}
