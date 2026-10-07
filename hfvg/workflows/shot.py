"""ShotWorkflow: handles still → review → clip → QC for one shot."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg import budget, config  # Non-deterministic modules with os.getenv
    from hfvg.activities import (
        await_job_enforced,
        precheck_clip_qc,
        precheck_still_qc,
        record_shot_result,
        review_clip,
        review_still,
        submit_clip_job_enforced,
        submit_still_job_enforced,
    )
    from hfvg.errors import ContentBlockError, InsufficientCreditsError
    from hfvg.models import Asset, GenerationRequest
    
    import os


@workflow.defn
class ShotWorkflow:
    """Workflow for a single shot: still → review → clip → QC."""

    def __init__(self):
        self.shot_id: str = ""
        self.episode_id: str = ""
        self.still_asset: Asset | None = None
        self.clip_asset: Asset | None = None
        self.version = 1
        self.max_retries = 3
        self.stills_approved = False  # Wait for parent to approve this scene's stills
        self.human_approved_still = False  # Track if human approved an escalated still

    @workflow.signal
    def stills_approved(self):
        """Signal from parent that this scene's stills have been approved."""
        self.stills_approved = True
    
    @workflow.signal
    def clip_approved(self):
        """Signal from API that this shot's clip has been approved."""
        pass  # Just for audit trail; workflow completes after clip generation

    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """
        Execute shot workflow: generate still, review it, generate clip, QC it.

        Returns: dict with still_url, clip_url, and status
        """
        self.episode_id = episode_id
        self.shot_id = shot_plan["shot_id"]

        workflow.logger.info(f"Starting shot {self.shot_id}")

        # NO RETRY after reserve: each retry would resubmit a paid call and leak reservations
        # Provider idempotency-key should dedupe, but we must not rely on that
        retry_policy = RetryPolicy(
            maximum_attempts=1,  # No automatic retries on paid activities
            non_retryable_error_types=[
                "InsufficientCreditsError",
                "ContentBlockError",
            ],
        )

        for attempt in range(self.max_retries):
            try:
                # Extract parameters from shot plan
                refs = shot_plan.get("refs", [])
                params = shot_plan.get("params", {})
                resolution = params.get("resolution", "1k")
                quality = params.get("quality", "medium")

                # PRE-FLIGHT QC: Check before any paid generation
                precheck = await workflow.execute_activity(
                    precheck_still_qc,
                    args=[self.episode_id, self.shot_id, shot_plan["prompt"], params],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                
                if not precheck["passed"]:
                    workflow.logger.error(
                        f"Still pre-flight QC failed for {self.shot_id}: {precheck['issues']}"
                    )
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "error": f"Pre-flight QC failed: {', '.join(precheck['issues'])}",
                    }

                # Call enforced activity with individual parameters - returns dict with job info
                job_info = await workflow.execute_activity(
                    submit_still_job_enforced,
                    args=[
                        self.episode_id,
                        self.shot_id,
                        shot_plan["prompt"],
                        self.version,
                        refs,
                        resolution,
                        quality,
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                # Poll for completion with all required args
                self.still_asset = await workflow.execute_activity(
                    await_job_enforced,
                    args=[
                        job_info["job_id"],
                        "still",
                        self.episode_id,
                        self.shot_id,
                        job_info["line_name"],
                        job_info["reserved_amount"],
                    ],
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                workflow.logger.info(f"Still generated: {self.still_asset['url']}")

                review_result = await workflow.execute_activity(
                    review_still,
                    args=[self.still_asset["url"], {}],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if review_result["passed"]:
                    workflow.logger.info(f"Still passed QC on attempt {attempt + 1}")
                    
                    # Record still URL to DB
                    await workflow.execute_activity(
                        record_shot_result,
                        args=[
                            os.getenv("DATABASE_PATH", "./data/studio.db"),
                            self.episode_id,
                            self.shot_id,
                            "still_complete",
                            self.still_asset["url"],
                            None,  # clip_url
                            {"still_qc": review_result},
                            attempt,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    break
                elif review_result.get("escalate"):
                    # QC failed in live mode - escalate to human review, wait for approval
                    workflow.logger.info(
                        f"Still QC escalated to human review, waiting for approval signal: {review_result['issues']}"
                    )
                    
                    # Record still as needs_review
                    await workflow.execute_activity(
                        record_shot_result,
                        args=[
                            os.getenv("DATABASE_PATH", "./data/studio.db"),
                            self.episode_id,
                            self.shot_id,
                            "needs_review",
                            self.still_asset["url"],
                            None,  # clip_url
                            {"still_qc": review_result},
                            attempt,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    # Wait for human approval signal (stills_approved)
                    workflow.logger.info(f"Waiting for human approval of still {self.shot_id}")
                    await workflow.wait_condition(lambda: self.stills_approved, timeout=timedelta(hours=24))
                    workflow.logger.info(f"Still {self.shot_id} approved by human, proceeding to clip")
                    self.human_approved_still = True
                    
                    # Update status to still_complete after approval
                    await workflow.execute_activity(
                        record_shot_result,
                        args=[
                            os.getenv("DATABASE_PATH", "./data/studio.db"),
                            self.episode_id,
                            self.shot_id,
                            "still_complete",
                            self.still_asset["url"],
                            None,  # clip_url
                            {"still_qc": review_result, "human_approved": True},
                            attempt,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    break
                else:
                    # QC failed without escalation (shouldn't happen in live; review always escalates)
                    # Treat as terminal failure to prevent automatic paid retry loop
                    workflow.logger.error(
                        f"Still failed QC without escalation: {review_result['issues']}"
                    )
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "still_url": self.still_asset["url"],
                        "error": f"QC failed: {', '.join(review_result['issues'])}",
                    }

            except ContentBlockError as e:
                workflow.logger.error(f"Content block on still: {e}, routing to recovery")
                return {
                    "shot_id": self.shot_id,
                    "status": "blocked",
                    "error": str(e),
                }

            except InsufficientCreditsError as e:
                workflow.logger.error(f"Insufficient credits: {e}")
                raise

        if not self.still_asset or (not review_result.get("passed") and not self.human_approved_still):
            workflow.logger.error(f"Still failed after {self.max_retries} attempts")
            return {
                "shot_id": self.shot_id,
                "status": "failed",
                "error": "Max retries exceeded",
            }

        # CRITICAL: Wait for parent to approve this scene's stills before generating clip
        workflow.logger.info(f"Still ready, waiting for stills approval before clip generation")
        await workflow.wait_condition(lambda: self.stills_approved)
        workflow.logger.info(f"Stills approved, proceeding to clip generation")

        for attempt in range(self.max_retries):
            try:
                # Extract duration from shot plan params
                params = shot_plan.get("params", {})
                duration = params.get("duration", 5.0)

                # PRE-FLIGHT QC: Check before any paid generation
                precheck = await workflow.execute_activity(
                    precheck_clip_qc,
                    args=[self.episode_id, self.shot_id, shot_plan["prompt"], duration],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                
                if not precheck["passed"]:
                    workflow.logger.error(
                        f"Clip pre-flight QC failed for {self.shot_id}: {precheck['issues']}"
                    )
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "still_url": self.still_asset["url"],
                        "error": f"Clip pre-flight QC failed: {', '.join(precheck['issues'])}",
                    }

                # Call enforced activity with individual parameters - returns dict with job info
                job_info = await workflow.execute_activity(
                    submit_clip_job_enforced,
                    args=[
                        self.episode_id,
                        self.shot_id,
                        self.still_asset["url"],  # start_image_url
                        shot_plan["prompt"],
                        duration,
                        self.version,
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                # Poll for completion with all required args
                self.clip_asset = await workflow.execute_activity(
                    await_job_enforced,
                    args=[
                        job_info["job_id"],
                        "clip",
                        self.episode_id,
                        self.shot_id,
                        job_info["line_name"],
                        job_info["reserved_amount"],
                    ],
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=retry_policy,
                )

                workflow.logger.info(f"Clip generated: {self.clip_asset['url']}")

                clip_qc = await workflow.execute_activity(
                    review_clip,
                    args=[self.clip_asset["url"], {}],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if clip_qc["passed"]:
                    workflow.logger.info(f"Clip passed QC on attempt {attempt + 1}")
                    break
                elif clip_qc.get("escalate"):
                    # QC failed in live mode - escalate to human review, don't retry
                    workflow.logger.error(
                        f"Clip QC failed in live mode, escalating to human review: {clip_qc['issues']}"
                    )
                    return {
                        "shot_id": self.shot_id,
                        "status": "needs_review",
                        "still_url": self.still_asset["url"],
                        "clip_url": self.clip_asset["url"],
                        "error": f"Clip QC failed - escalated to human review: {', '.join(clip_qc['issues'])}",
                    }
                else:
                    # QC failed without escalation (shouldn't happen in live; review always escalates)
                    # Treat as terminal failure to prevent automatic paid retry loop
                    workflow.logger.error(
                        f"Clip failed QC without escalation: {clip_qc['issues']}"
                    )
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "clip_url": self.clip_asset["url"],
                        "error": f"QC failed: {', '.join(clip_qc['issues'])}",
                    }

            except ContentBlockError as e:
                workflow.logger.error(f"Content block on clip: {e}, routing to recovery")
                return {
                    "shot_id": self.shot_id,
                    "status": "blocked",
                    "still_url": self.still_asset["url"],
                    "error": str(e),
                }

            except InsufficientCreditsError as e:
                workflow.logger.error(f"Insufficient credits: {e}")
                raise

        if not self.clip_asset or not clip_qc.get("passed"):
            workflow.logger.error(f"Clip failed after {self.max_retries} attempts")
            return {
                "shot_id": self.shot_id,
                "status": "failed",
                "still_url": self.still_asset["url"],
                "error": "Max clip retries exceeded",
            }

        workflow.logger.info(f"Shot {self.shot_id} completed successfully")
        
        # Record final result to DB
        await workflow.execute_activity(
            record_shot_result,
            args=[
                os.getenv("DATABASE_PATH", "./data/studio.db"),
                self.episode_id,
                self.shot_id,
                "completed",
                self.still_asset["url"],
                self.clip_asset["url"],
                {"still_qc": review_result, "clip_qc": clip_qc},
                self.version - 1,  # Total retries
            ],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        
        return {
            "shot_id": self.shot_id,
            "status": "completed",
            "still_url": self.still_asset["url"],
            "clip_url": self.clip_asset["url"],
        }
