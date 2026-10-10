"""ShotWorkflow: handles still → review → clip → QC for one shot."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg import budget, config  # Non-deterministic modules with os.getenv
    from hfvg.activities import (
        commit_job_budget,
        mark_job_pending_reconcile,
        poll_job_status,
        precheck_clip_qc,
        precheck_still_qc,
        record_shot_result,
        release_job_budget,
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

    @workflow.query
    def get_state(self) -> dict:
        """Query current workflow state."""
        return {
            "shot_id": self.shot_id,
            "episode_id": self.episode_id,
            "has_still": self.still_asset is not None,
            "has_clip": self.clip_asset is not None,
            "stills_approved": self.stills_approved,
            "human_approved_still": self.human_approved_still,
        }
    
    async def _poll_job_to_completion(
        self,
        job_id: str,
        job_type: str,
        line_name: str,
        reserved_amount: float,
    ) -> dict:
        """
        Poll job status with workflow-level retry loop (R1 design).
        
        Each poll is a short activity (≤60s) called in a loop with workflow.sleep backoff.
        Bounded to ~30 minutes total. On exhaustion/timeout/cancel: reservation stays reserved,
        job marked pending_reconcile, never resubmit, never release.
        Release only on confirmed provider failure.
        
        Args:
            job_id: Provider job ID
            job_type: "still" or "clip"
            line_name: Budget line name
            reserved_amount: Reserved amount
            
        Returns:
            dict with url, cost, and status
        """
        retry_policy = RetryPolicy(
            maximum_attempts=3,
            non_retryable_error_types=[
                "InsufficientCreditsError",
                "ContentBlockError",
            ],
        )
        
        # Polling parameters: ~30 min total
        max_polls = 180
        initial_delay_secs = 5
        max_delay_secs = 10
        
        for poll_count in range(max_polls):
            # Exponential backoff with cap
            delay_secs = min(initial_delay_secs * (1.5 ** (poll_count // 10)), max_delay_secs)
            
            try:
                status_result = await workflow.execute_activity(
                    poll_job_status,
                    args=[job_id, job_type, self.episode_id],
                    start_to_close_timeout=timedelta(seconds=60),
                    retry_policy=retry_policy,
                )
                
                status = status_result["status"]
                
                # Terminal statuses
                if status == "completed":
                    # Commit budget
                    await workflow.execute_activity(
                        commit_job_budget,
                        args=[
                            self.episode_id,
                            self.shot_id,
                            job_id,
                            job_type,
                            line_name,
                            reserved_amount,
                            status_result.get("cost"),
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    workflow.logger.info(f"Job {job_id} completed, budget committed")
                    
                    return {
                        "url": status_result["output_url"],
                        "cost": status_result.get("cost", reserved_amount),
                        "status": "completed",
                    }
                
                elif status in ("failed", "blocked", "canceled"):
                    # Provider confirmed failure - release reservation
                    reason = f"{status}: {status_result.get('error', 'no details')}"
                    
                    await workflow.execute_activity(
                        release_job_budget,
                        args=[
                            self.episode_id,
                            self.shot_id,
                            job_id,
                            job_type,
                            line_name,
                            reserved_amount,
                            reason,
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    workflow.logger.error(f"Job {job_id} {status}, reservation released")
                    
                    # Raise appropriate error
                    if status == "blocked":
                        raise ContentBlockError(f"Provider blocked: {status_result.get('error')}")
                    else:
                        raise RuntimeError(f"Job {status}: {status_result.get('error')}")
                
                # Non-terminal: continue polling
                workflow.logger.info(f"Job {job_id} status: {status}, poll {poll_count + 1}/{max_polls}")
                await workflow.sleep(timedelta(seconds=delay_secs))
                
            except Exception as e:
                # Activity timeout/cancel/error: DO NOT release reservation
                workflow.logger.error(f"Poll activity error on attempt {poll_count + 1}: {e}")
                
                # If we've exhausted retries within this poll attempt, continue to next poll
                # The retry_policy will retry the activity, but if all retries fail, we continue
                if poll_count < max_polls - 1:
                    await workflow.sleep(timedelta(seconds=delay_secs))
                    continue
                else:
                    # Exhausted all polls - mark pending reconcile, keep reservation
                    break
        
        # Exhausted polling window - mark pending_reconcile, keep reservation
        workflow.logger.error(
            f"Job {job_id} exhausted {max_polls} polls (~30 min). "
            f"Marking pending_reconcile, reservation held."
        )
        
        await workflow.execute_activity(
            mark_job_pending_reconcile,
            args=[self.episode_id, self.shot_id, job_id, job_type],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=retry_policy,
        )
        
        raise RuntimeError(
            f"Job {job_id} polling exhausted after {max_polls} attempts (~30 min). "
            f"Reservation held, manual reconciliation required."
        )

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
                aspect_ratio = params.get("aspect_ratio", "9:16")  # Default to 9:16

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
                        aspect_ratio,
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                # Poll for completion using workflow-level polling loop (R1)
                poll_result = await self._poll_job_to_completion(
                    job_id=job_info["job_id"],
                    job_type="still",
                    line_name=job_info["line_name"],
                    reserved_amount=job_info["reserved_amount"],
                )
                
                self.still_asset = {
                    "url": poll_result["url"],
                    "cost": poll_result["cost"],
                }

                workflow.logger.info(f"Still generated: {self.still_asset['url']}")

                review_result = await workflow.execute_activity(
                    review_still,
                    args=[self.still_asset["url"], {}, self.episode_id, self.shot_id],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if review_result["passed"]:
                    workflow.logger.info(f"Still passed QC on attempt {attempt + 1}")
                    
                    # Record still URL to DB (clear any stale clip_url from previous runs)
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
                            shot_plan["prompt"],  # Store the composed prompt
                            True,  # clear_clip - clear any stale clip_url from dry runs
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
                    
                    # Record still as needs_review (clear any stale clip_url from previous runs)
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
                            shot_plan["prompt"],  # Store the composed prompt
                            True,  # clear_clip - clear any stale clip_url from dry runs
                        ],
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=retry_policy,
                    )
                    
                    # Wait for human approval signal (stills_approved)
                    workflow.logger.info(f"Waiting for human approval of still {self.shot_id}")
                    await workflow.wait_condition(lambda: self.stills_approved, timeout=timedelta(hours=24))
                    workflow.logger.info(f"Still {self.shot_id} approved by human, proceeding to clip")
                    
                    # Set human_approved_still flag AFTER approval
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
                            shot_plan["prompt"],  # Store the composed prompt
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

                # Poll for completion using workflow-level polling loop (R1)
                poll_result = await self._poll_job_to_completion(
                    job_id=job_info["job_id"],
                    job_type="clip",
                    line_name=job_info["line_name"],
                    reserved_amount=job_info["reserved_amount"],
                )
                
                self.clip_asset = {
                    "url": poll_result["url"],
                    "cost": poll_result["cost"],
                }

                workflow.logger.info(f"Clip generated: {self.clip_asset['url']}")

                clip_qc = await workflow.execute_activity(
                    review_clip,
                    args=[self.clip_asset["url"], {}, self.episode_id, self.shot_id],
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
