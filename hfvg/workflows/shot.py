"""ShotWorkflow: handles still → review → clip → QC for one shot."""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from hfvg.activities import (
        await_job,
        review_clip,
        review_still,
        submit_clip_job,
        submit_still_job,
    )
    from hfvg.errors import ContentBlockError, InsufficientCreditsError
    from hfvg.models import Asset, GenerationRequest


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

    @workflow.run
    async def run(self, episode_id: str, shot_plan: dict) -> dict:
        """
        Execute shot workflow: generate still, review it, generate clip, QC it.

        Returns: dict with still_url, clip_url, and status
        """
        self.episode_id = episode_id
        self.shot_id = shot_plan["shot_id"]

        workflow.logger.info(f"Starting shot {self.shot_id}")

        retry_policy = RetryPolicy(
            maximum_attempts=3,
            initial_interval=timedelta(seconds=1),
            maximum_interval=timedelta(seconds=10),
            backoff_coefficient=2.0,
            non_retryable_error_types=[
                "InsufficientCreditsError",
                "ContentBlockError",
            ],
        )

        for attempt in range(self.max_retries):
            try:
                still_req = GenerationRequest(
                    shot_id=self.shot_id,
                    version=self.version,
                    prompt=shot_plan["prompt"],
                    refs=shot_plan.get("refs", []),
                    params={"type": "still"},
                    estimated_cost=6.5,
                )

                job_id = await workflow.execute_activity(
                    submit_still_job,
                    args=[self.episode_id, still_req],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                self.still_asset = await workflow.execute_activity(
                    await_job,
                    args=[job_id, "still"],
                    start_to_close_timeout=timedelta(minutes=10),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                workflow.logger.info(f"Still generated: {self.still_asset.url}")

                review_result = await workflow.execute_activity(
                    review_still,
                    args=[self.still_asset.url, {}],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if review_result["passed"]:
                    workflow.logger.info(f"Still passed QC on attempt {attempt + 1}")
                    break
                else:
                    workflow.logger.warning(
                        f"Still failed QC: {review_result['issues']}, retrying..."
                    )
                    self.version += 1

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

        if not self.still_asset or not review_result.get("passed"):
            workflow.logger.error(f"Still failed after {self.max_retries} attempts")
            return {
                "shot_id": self.shot_id,
                "status": "failed",
                "error": "Max retries exceeded",
            }

        for attempt in range(self.max_retries):
            try:
                clip_req = GenerationRequest(
                    shot_id=self.shot_id,
                    version=self.version,
                    prompt=shot_plan["prompt"],
                    refs=[self.still_asset.url] + shot_plan.get("refs", []),
                    params={"type": "clip", "duration": 5},
                    estimated_cost=28.0,
                )

                job_id = await workflow.execute_activity(
                    submit_clip_job,
                    args=[self.episode_id, clip_req],
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=retry_policy,
                )

                self.clip_asset = await workflow.execute_activity(
                    await_job,
                    args=[job_id, "clip"],
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=3),
                    retry_policy=retry_policy,
                )

                workflow.logger.info(f"Clip generated: {self.clip_asset.url}")

                clip_qc = await workflow.execute_activity(
                    review_clip,
                    args=[self.clip_asset.url, {}],
                    start_to_close_timeout=timedelta(minutes=5),
                    heartbeat_timeout=timedelta(minutes=1),
                    retry_policy=retry_policy,
                )

                if clip_qc["passed"]:
                    workflow.logger.info(f"Clip passed QC on attempt {attempt + 1}")
                    break
                else:
                    workflow.logger.warning(f"Clip failed QC: {clip_qc['issues']}, retrying...")
                    self.version += 1

            except ContentBlockError as e:
                workflow.logger.error(f"Content block on clip: {e}, routing to recovery")
                return {
                    "shot_id": self.shot_id,
                    "status": "blocked",
                    "still_url": self.still_asset.url,
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
                "still_url": self.still_asset.url,
                "error": "Max clip retries exceeded",
            }

        workflow.logger.info(f"Shot {self.shot_id} completed successfully")
        return {
            "shot_id": self.shot_id,
            "status": "completed",
            "still_url": self.still_asset.url,
            "clip_url": self.clip_asset.url,
        }
