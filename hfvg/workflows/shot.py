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
    from hfvg.activities.credit_activities import (
        commit_credits_activity,
        release_credits_activity,
    )
    from hfvg.activities.save_assets import save_asset_locally
    from hfvg.errors import ContentBlockError, InsufficientCreditsError
    from hfvg.models import Asset, GenerationRequest
    from temporalio.exceptions import ActivityError, ApplicationError
    
    import os


# RuntimeError raised by the poll loop (confirmed provider failure, poll exhaustion) MUST fail the
# workflow. In temporalio-python any non-ApplicationError exception only fails the workflow *task*,
# which the server retries forever, so the workflow would never reach a terminal state.
@workflow.defn(failure_exception_types=[RuntimeError])
class ShotWorkflow:
    """Workflow for a single shot: still → review → clip → QC."""

    def __init__(self):
        self.shot_id: str = ""
        self.episode_id: str = ""
        self.still_asset: Asset | None = None
        self.end_still_asset: Asset | None = None  # END frame still (for end_frame=true shots)
        self.clip_asset: Asset | None = None
        self.version = 1
        self.max_retries = 3
        self.stills_approved = False  # Wait for parent to approve this scene's stills
        self.human_approved_still = False  # Track if human approved an escalated still
        self.end_image_url: str | None = None  # END frame URL (end-frame shots), set by signal
        self.cap_raised = False  # Signal that credit cap was raised
        self.end_still_requested = False  # generate_end_still signal (backfill for runs started before the fix)
        self.end_prompt_override: str | None = None
        self.status = "running"  # Status: running, paused_cap, completed, failed
        
        # Per-shot G2 status tracking (Item 4)
        self.still_status = "pending"  # pending/done/failed
        self.still_url: str | None = None
        self.still_error: str | None = None
        self.clip_status = "pending"  # pending/done/failed
        self.clip_url: str | None = None
        self.clip_error: str | None = None
        self.end_still_status = "pending"  # For end-frame shots
        self.end_still_url: str | None = None
        self.end_still_error: str | None = None

    @workflow.signal
    def stills_approved(self):
        """Signal from parent that this scene's stills have been approved."""
        self.stills_approved = True
    
    @workflow.signal
    def set_end_image(self, url: str):
        """Signal: the END-frame image URL for a start+end clip (Kling last_image_url)."""
        self.end_image_url = url
    
    @workflow.signal
    def generate_end_still(self, end_prompt: str | None = None):
        """Backfill: generate the missing _end still while waiting at review (before stills_approved).
        Only acts for end_frame shots whose end still is missing; never touches the start still."""
        self.end_still_requested = True
        if end_prompt:
            self.end_prompt_override = end_prompt

    @workflow.signal
    def resume_after_cap_raise(self):
        """Signal from parent that credit cap was raised, resume paused shot."""
        self.cap_raised = True
    
    @workflow.query
    def get_status(self) -> str:
        """Query current status (running, paused_cap, completed, failed)."""
        return self.status

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
            # Per-shot G2 status (Item 4)
            "still_status": self.still_status,
            "still_url": self.still_url,
            "still_error": self.still_error,
            "clip_status": self.clip_status,
            "clip_url": self.clip_url,
            "clip_error": self.clip_error,
            "end_still_status": self.end_still_status,
            "end_still_url": self.end_still_url,
            "end_still_error": self.end_still_error,
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
            except (ContentBlockError, InsufficientCreditsError):
                raise
            except Exception as e:
                # Activity timeout/cancel/error from poll_job_status: DO NOT release reservation
                workflow.logger.error(f"Poll activity error on attempt {poll_count + 1}: {e}")
                
                # If we've exhausted retries within this poll attempt, continue to next poll
                # The retry_policy will retry the activity, but if all retries fail, we continue
                if poll_count < max_polls - 1:
                    await workflow.sleep(timedelta(seconds=delay_secs))
                    continue
                else:
                    # Exhausted all polls - mark pending reconcile, keep reservation
                    break
            
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
                
                # Commit credits
                await workflow.execute_activity(
                    commit_credits_activity,
                    args=[self.episode_id, job_id],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                
                workflow.logger.info(f"Job {job_id} completed, budget and credits committed")
                
                return {
                    "url": status_result["output_url"],
                    "cost": status_result.get("cost") or reserved_amount,
                    "status": "completed",
                }
            
            elif status in ("failed", "blocked", "canceled"):
                # Provider confirmed failure - release reservation
                error_details = status_result.get('error', 'no details')
                reason = f"{status}: {error_details}"
                
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
                
                # Release credits
                await workflow.execute_activity(
                    release_credits_activity,
                    args=[self.episode_id, job_id, reason],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=retry_policy,
                )
                
                workflow.logger.error(f"Job {job_id} {status}, reservation and credits released")
                
                # Raise appropriate error with job_id and raw error
                if status == "blocked":
                    raise ContentBlockError(f"Provider blocked job {job_id}: {error_details}")
                else:
                    raise RuntimeError(f"Job {job_id} {status}: {error_details}")
            
            # Non-terminal: continue polling
            workflow.logger.info(f"Job {job_id} status: {status}, poll {poll_count + 1}/{max_polls}")
            await workflow.sleep(timedelta(seconds=delay_secs))
        
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

    async def _generate_end_still(
        self,
        shot_plan: dict,
        refs: list[str],
        resolution: str,
        quality: str,
        aspect_ratio: str,
        retry_policy: RetryPolicy,
    ) -> dict:
        """
        Generate end-frame still for shots with end_frame=true.
        Called after start still passes QC, before Gate 2.
        
        Returns:
            dict with url and cost
        """
        # end_prompt (signal override > shot plan) if provided, otherwise the start frame prompt
        end_prompt = self.end_prompt_override or shot_plan.get("end_prompt") or shot_plan["prompt"]
        
        workflow.logger.info(f"Generating end still for {self.shot_id}")
        
        # Retry loop for credit cap raises
        while True:
            try:
                job_info = await workflow.execute_activity(
                    submit_still_job_enforced,
                    args=[
                        self.episode_id,
                        f"{self.shot_id}_end",  # Distinct shot_id for end frame
                        end_prompt,
                        self.version,
                        refs,
                        resolution,
                        quality,
                        aspect_ratio,
                        None,  # tier
                        shot_plan.get("shot_meta"),
                        shot_plan.get("missing_refs", []),
                        self.version,  # attempt
                        self.still_url,  # lead_ref: the approved/landed start still goes first
                    ],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )
                break  # Success
            except ActivityError as e:
                if e.cause and isinstance(e.cause, ApplicationError) and e.cause.type == "InsufficientCreditsError":
                    workflow.logger.warning(f"Credit cap hit for end still {self.shot_id}, pausing")
                    self.status = "paused_cap"
                    await workflow.wait_condition(lambda: self.cap_raised)
                    workflow.logger.info(f"Cap raised, retrying end still {self.shot_id}")
                    self.status = "running"
                    self.cap_raised = False
                else:
                    raise
        
        # Poll for completion
        poll_result = await self._poll_job_to_completion(
            job_id=job_info["job_id"],
            job_type="still",
            line_name=job_info["line_name"],
            reserved_amount=job_info["reserved_amount"],
        )
        
        return {
            "url": poll_result["url"],
            "cost": poll_result["cost"],
        }
    
    async def _produce_end_still(
        self, shot_plan: dict, refs, resolution: str, quality: str, aspect_ratio: str,
        retry_policy: RetryPolicy,
    ) -> dict | None:
        """Generate, record and save the _end still. Returns a failure dict (and records the error
        in G2 state) if it failed, None on success."""
        workflow.logger.info(f"{self.shot_id} requires end frame, generating now")
        try:
            end_still_result = await self._generate_end_still(
                shot_plan, refs, resolution, quality, aspect_ratio, retry_policy
            )
        except (RuntimeError, ActivityError) as e:
            err = str(e.cause) if isinstance(e, ActivityError) and e.cause else str(e)
            self.end_still_status = "failed"
            self.end_still_error = err
            workflow.logger.error(f"End-frame still failed for {self.shot_id}: {err}")
            return {"shot_id": self.shot_id, "status": "failed", "error": f"End still failed: {err}"}
        self.end_still_asset = end_still_result
        self.end_image_url = end_still_result["url"]
        self.end_still_status = "done"
        self.end_still_url = end_still_result["url"]
        self.end_still_error = None
        workflow.logger.info(f"End-frame still generated: {self.end_image_url}")
        try:
            end_local = await workflow.execute_activity(
                save_asset_locally,
                args=[self.episode_id, f"{self.shot_id}_end", end_still_result["url"], "still", self.version],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=retry_policy,
            )
            workflow.logger.info(f"End still saved locally to {end_local}")
        except Exception as e:
            workflow.logger.warning(f"Failed to save end still locally: {e}, continuing")
        return None

    def _end_still_backfill_due(self, params: dict) -> bool:
        return bool(self.end_still_requested and params.get("end_frame") and not self.end_image_url)

    async def _notify_parent_retry_still_ready(self, shot_plan: dict) -> None:
        """Tell the episode workflow this RETRY child's still(s) have landed (QC'd + recorded),
        so it can clear pending_retries and let G2 (the human review) be approved.
        Only retries (version > 1) with a known parent; the parent ignores unknown shots.
        Must run BEFORE waiting for stills_approved, otherwise G2 deadlocks."""
        parent_id = shot_plan.get("parent_workflow_id")
        if not parent_id or self.version <= 1:
            return
        try:
            await workflow.get_external_workflow_handle(parent_id).signal(
                "retry_still_ready", args=[self.shot_id, self.version]
            )
        except ApplicationError as e:
            workflow.logger.warning(f"Could not send retry_still_ready to {parent_id}: {e}")

    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """
        Execute shot workflow: generate still, review it, generate clip, QC it.

        Returns: dict with still_url, clip_url, and status
        """
        self.episode_id = episode_id
        self.shot_id = shot_plan["shot_id"]
        
        # Use attempt number if provided (for retries), otherwise default to 0
        # Version is separate: original=1, retry1=2, retry2=3, etc.
        attempt = shot_plan.get("attempt", 0)
        self.version = shot_plan.get("version", 1)

        workflow.logger.info(f"Starting shot {self.shot_id} (attempt {attempt})")

        # NO RETRY after reserve: each retry would resubmit a paid call and leak reservations
        # Provider idempotency-key should dedupe, but we must not rely on that
        # InsufficientCreditsError is now handled with explicit pause/resume, not in retry policy
        retry_policy = RetryPolicy(
            maximum_attempts=1,  # No automatic retries on paid activities
            non_retryable_error_types=[
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

                # End-only backfill (EpisodeWorkflowV2.retry_end_still): the start still for this
                # attempt already landed and is reused as-is - NO start submit, no new start spend.
                # Input-driven (not patched): histories without reuse_start_still replay unchanged.
                reuse = shot_plan.get("reuse_start_still")
                if reuse:
                    workflow.logger.info(
                        f"Reusing landed start still for {self.shot_id} (job {reuse.get('job_id')}): {reuse['url']}"
                    )
                    poll_result = {"url": reuse["url"], "cost": 0}
                else:
                    # Call enforced activity with individual parameters - returns dict with job info
                    # Retry loop for credit cap raises
                    while True:
                        try:
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
                                    None,  # tier (activity default)
                                    shot_plan.get("shot_meta"),  # prompt lint metadata
                                    shot_plan.get("missing_refs", []),  # planned refs not on disk: live refuses
                                    self.version,  # attempt
                                ],
                                start_to_close_timeout=timedelta(minutes=2),
                                retry_policy=retry_policy,
                            )
                            break  # Success, exit retry loop
                        except ActivityError as e:
                            # Check if wrapped error is InsufficientCreditsError
                            if e.cause and isinstance(e.cause, ApplicationError) and e.cause.type == "InsufficientCreditsError":
                                # Hard cap hit - pause and wait for cap raise
                                workflow.logger.warning(f"Credit cap hit for {self.shot_id}, pausing")
                                self.status = "paused_cap"
                            
                                # Wait for parent to raise cap and signal us
                                await workflow.wait_condition(lambda: self.cap_raised)
                            
                                workflow.logger.info(f"Cap raised, retrying {self.shot_id}")
                                self.status = "running"
                                self.cap_raised = False  # Reset for potential future pauses
                                # Loop will retry with same job_id (idempotent)
                            else:
                                # Other activity error - set failed status with cause
                                self.still_status = "failed"
                                error_message = str(e.cause) if e.cause else str(e)
                                self.still_error = error_message
                                workflow.logger.error(f"Still submit failed: {error_message}")
                                return {
                                    "shot_id": self.shot_id,
                                    "status": "failed",
                                    "error": error_message,
                                }

                    # Poll for completion using workflow-level polling loop (R1)
                    try:
                        poll_result = await self._poll_job_to_completion(
                            job_id=job_info["job_id"],
                            job_type="still",
                            line_name=job_info["line_name"],
                            reserved_amount=job_info["reserved_amount"],
                        )
                    except RuntimeError as e:
                        # Provider failure or polling exhaustion - update G2 status and return failure
                        self.still_status = "failed"
                        self.still_error = str(e)
                        workflow.logger.error(f"Still generation failed: {e}")
                        return {
                            "shot_id": self.shot_id,
                            "status": "failed",
                            "error": str(e),
                        }
                
                self.still_asset = {
                    "url": poll_result["url"],
                    "cost": poll_result["cost"],
                }
                
                # Update G2 status (Item 4)
                self.still_status = "done"
                self.still_url = poll_result["url"]
                self.still_error = None

                workflow.logger.info(f"Still generated: {self.still_asset['url']}")
                
                # Save still locally
                try:
                    still_local_path = await workflow.execute_activity(
                        save_asset_locally,
                        args=[self.episode_id, self.shot_id, poll_result["url"], "still", attempt + 1],
                        start_to_close_timeout=timedelta(minutes=5),
                        retry_policy=retry_policy,
                    )
                    workflow.logger.info(f"Still saved locally to {still_local_path}")
                except Exception as e:
                    workflow.logger.warning(f"Failed to save still locally: {e}, continuing")

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
                    
                    # Generate end-frame still if required (before Gate 2)
                    if params.get("end_frame"):
                        end_fail = await self._produce_end_still(
                            shot_plan, refs, resolution, quality, aspect_ratio, retry_policy
                        )
                        if end_fail:
                            return end_fail
                    
                    break
                elif review_result.get("escalate"):
                    # QC failed in live mode - escalate to human review, wait for approval
                    workflow.logger.info(
                        f"Still QC escalated to human review, waiting for approval signal: {review_result['issues']}"
                    )
                    
                    # Both review paths produce the _end still BEFORE the human-review/G2 wait (and
                    # before retry_still_ready). patched(): runs already parked at review replay unchanged.
                    if params.get("end_frame") and workflow.patched("end-still-before-human-review"):
                        end_fail = await self._produce_end_still(
                            shot_plan, refs, resolution, quality, aspect_ratio, retry_policy
                        )
                        if end_fail:
                            return end_fail
                    
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
                    
                    # Retry still landed (recorded as needs_review): unblock G2 before waiting on it
                    await self._notify_parent_retry_still_ready(shot_plan)

                    # Wait for human approval signal (stills_approved)
                    workflow.logger.info(f"Waiting for human approval of still {self.shot_id}")
                    review_wait = lambda: self.stills_approved or self._end_still_backfill_due(params)
                    await workflow.wait_condition(review_wait, timeout=timedelta(hours=24))
                    # Backfill (generate_end_still signal): only for runs parked here without an _end still
                    while not self.stills_approved and self._end_still_backfill_due(params):
                        self.end_still_requested = False
                        await self._produce_end_still(
                            shot_plan, refs, resolution, quality, aspect_ratio, retry_policy
                        )  # on failure the error is in G2 state; keep waiting (start still preserved)
                        await workflow.wait_condition(review_wait, timeout=timedelta(hours=24))
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
                    # Update G2 status
                    self.still_status = "failed"
                    self.still_error = f"QC failed: {', '.join(review_result['issues'])}"
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "still_url": self.still_asset["url"],
                        "error": f"QC failed: {', '.join(review_result['issues'])}",
                    }

            except ContentBlockError as e:
                workflow.logger.error(f"Content block on still: {e}, routing to recovery")
                # Update G2 status
                self.still_status = "failed"
                self.still_error = f"Content blocked: {str(e)}"
                return {
                    "shot_id": self.shot_id,
                    "status": "blocked",
                    "error": str(e),
                }

            # InsufficientCreditsError is now handled inside the submit loop with pause/resume

        if not self.still_asset or (not review_result.get("passed") and not self.human_approved_still):
            workflow.logger.error(f"Still failed after {self.max_retries} attempts")
            # Update G2 status
            self.still_status = "failed"
            self.still_error = "Max retries exceeded"
            return {
                "shot_id": self.shot_id,
                "status": "failed",
                "error": "Max retries exceeded",
            }

        # Retry still(s) landed (start + end frame, QC passed, recorded): unblock G2.
        # (Escalated retries already notified above, before their own wait.)
        if not self.human_approved_still:
            await self._notify_parent_retry_still_ready(shot_plan)

        # CRITICAL: Wait for parent to approve this scene's stills before generating clip
        workflow.logger.info(f"Still ready, waiting for stills approval before clip generation")
        await workflow.wait_condition(lambda: self.stills_approved)
        workflow.logger.info(f"Stills approved, proceeding to clip generation")

        for attempt in range(self.max_retries):
            try:
                # Extract duration from shot plan params
                params = shot_plan.get("params", {})
                duration = params.get("duration", 5.0)
                # Use the end_image_url that was generated before Gate 2
                end_image_url = self.end_image_url if params.get("end_frame") else None

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
                # Retry loop for credit cap raises
                while True:
                    try:
                        job_info = await workflow.execute_activity(
                            submit_clip_job_enforced,
                            args=[
                                self.episode_id,
                                self.shot_id,
                                self.still_asset["url"],  # start_image_url
                                shot_plan["prompt"],
                                duration,
                                self.version,
                                None,  # tier (activity default)
                                shot_plan.get("shot_meta"),  # prompt lint metadata
                                params.get("model", "kling"),  # explicit routing
                                shot_plan.get("tags", []),
                                end_image_url,
                                None,  # bitrate_mode (activity default)
                                shot_plan.get("missing_refs", []),
                            ],
                            start_to_close_timeout=timedelta(minutes=2),
                            retry_policy=retry_policy,
                        )
                        break  # Success, exit retry loop
                    except ActivityError as e:
                        # Check if wrapped error is InsufficientCreditsError
                        if e.cause and isinstance(e.cause, ApplicationError) and e.cause.type == "InsufficientCreditsError":
                            # Hard cap hit - pause and wait for cap raise
                            workflow.logger.warning(f"Credit cap hit for {self.shot_id}, pausing")
                            self.status = "paused_cap"
                            
                            # Wait for parent to raise cap and signal us
                            await workflow.wait_condition(lambda: self.cap_raised)
                            
                            workflow.logger.info(f"Cap raised, retrying {self.shot_id}")
                            self.status = "running"
                            self.cap_raised = False  # Reset for potential future pauses
                            # Loop will retry with same job_id (idempotent)
                        else:
                            # Other activity error - set failed status with cause
                            self.clip_status = "failed"
                            error_message = str(e.cause) if e.cause else str(e)
                            self.clip_error = error_message
                            workflow.logger.error(f"Clip submit failed: {error_message}")
                            return {
                                "shot_id": self.shot_id,
                                "status": "failed",
                                "still_url": self.still_asset["url"],
                                "error": error_message,
                            }

                # Poll for completion using workflow-level polling loop (R1)
                try:
                    poll_result = await self._poll_job_to_completion(
                        job_id=job_info["job_id"],
                        job_type="clip",
                        line_name=job_info["line_name"],
                        reserved_amount=job_info["reserved_amount"],
                    )
                except RuntimeError as e:
                    # Provider failure or polling exhaustion - update clip status and return failure
                    self.clip_status = "failed"
                    self.clip_error = str(e)
                    workflow.logger.error(f"Clip generation failed: {e}")
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "still_url": self.still_asset["url"],
                        "error": str(e),
                    }
                
                self.clip_asset = {
                    "url": poll_result["url"],
                    "cost": poll_result["cost"],
                }

                workflow.logger.info(f"Clip generated: {self.clip_asset['url']}")
                
                # Save clip locally
                try:
                    clip_local_path = await workflow.execute_activity(
                        save_asset_locally,
                        args=[self.episode_id, self.shot_id, poll_result["url"], "clip", attempt + 1],
                        start_to_close_timeout=timedelta(minutes=10),
                        retry_policy=retry_policy,
                    )
                    workflow.logger.info(f"Clip saved locally to {clip_local_path}")
                except Exception as e:
                    workflow.logger.warning(f"Failed to save clip locally: {e}, continuing")

                # Derive expected resolution and aspect from shot params or episode defaults
                # Episode settings take precedence over hard-coded defaults
                shot_aspect = params.get("aspect_ratio")
                shot_resolution = params.get("resolution")
                
                # If not specified in shot params, fail loudly
                if not shot_aspect or not shot_resolution:
                    raise ApplicationError(
                        f"Shot {self.shot_id}: clip resolution/aspect_ratio not specified in shot params. "
                        f"Cannot validate motion QC without expected dimensions.",
                        non_retryable=True
                    )
                
                # Prepare rubric with expected clip parameters for motion QC
                clip_rubric = {
                    "duration": duration,
                    "resolution": shot_resolution,
                    "aspect_ratio": shot_aspect,
                }
                
                clip_qc = await workflow.execute_activity(
                    review_clip,
                    args=[self.clip_asset["url"], clip_rubric, self.episode_id, self.shot_id],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if clip_qc["passed"]:
                    workflow.logger.info(f"Clip passed QC on attempt {attempt + 1}")
                    # Update clip status
                    self.clip_status = "done"
                    self.clip_url = self.clip_asset["url"]
                    self.clip_error = None
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
                    # Update clip status
                    self.clip_status = "failed"
                    self.clip_error = f"QC failed: {', '.join(clip_qc['issues'])}"
                    return {
                        "shot_id": self.shot_id,
                        "status": "failed",
                        "clip_url": self.clip_asset["url"],
                        "error": f"QC failed: {', '.join(clip_qc['issues'])}",
                    }

            except ContentBlockError as e:
                workflow.logger.error(f"Content block on clip: {e}, routing to recovery")
                # Update clip status
                self.clip_status = "failed"
                self.clip_error = f"Content blocked: {str(e)}"
                return {
                    "shot_id": self.shot_id,
                    "status": "blocked",
                    "still_url": self.still_asset["url"],
                    "error": str(e),
                }

            # InsufficientCreditsError is now handled inside the submit loop with pause/resume

        if not self.clip_asset or not clip_qc.get("passed"):
            workflow.logger.error(f"Clip failed after {self.max_retries} attempts")
            # Update clip status
            self.clip_status = "failed"
            self.clip_error = "Max clip retries exceeded"
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
