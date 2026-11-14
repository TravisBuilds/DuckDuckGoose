"""CastSheetWorkflow: generate the images of one character sheet request, one guarded paid call at a time.

Input (built by the API from hfvg.cast_sheets.plan_jobs after the creator approved the estimate):
``{"episode_id", "character_id", "version", "stage", "jobs": [job, ...]}``. Jobs run in order; the
first failure or refusal (budget stop, balance, price above the approved estimate, provider error)
stops the request: the remaining jobs are marked skipped and nothing else is spent. A new workflow
type: open episode runs (EpisodeWorkflowV2) never see it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from hfvg.activities.cast_sheet import finish_cast_sheet_jobs_activity, generate_cast_sheet_image_activity

# A paid call is never retried automatically (a retry could pay twice); the activity resumes a
# submitted provider job by its persisted id when the request is made again.
PAID = RetryPolicy(maximum_attempts=1)
QUICK = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))


def cast_sheet_workflow_id(first_job_id: str) -> str:
    return f"cast-sheet-{first_job_id}"


@workflow.defn
class CastSheetWorkflow:
    def __init__(self) -> None:
        self.results: list[dict] = []

    @workflow.query
    def get_results(self) -> list[dict]:
        return self.results

    @workflow.run
    async def run(self, request: dict) -> dict:
        jobs = list(request.get("jobs") or [])
        for i, job in enumerate(jobs):
            try:
                result = await workflow.execute_activity(
                    generate_cast_sheet_image_activity, job,
                    start_to_close_timeout=timedelta(minutes=12),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=PAID,
                )
            except ActivityError as e:
                error = str(e.cause or e)
                self.results.append({"job_id": job["job_id"], "status": "failed", "error": error})
                rest = [j["job_id"] for j in jobs[i + 1:]]
                if rest:
                    await workflow.execute_activity(
                        finish_cast_sheet_jobs_activity,
                        args=[rest, "skipped", f"stopped after {job['job_id']} failed: {error[:200]}"],
                        start_to_close_timeout=timedelta(minutes=1), retry_policy=QUICK,
                    )
                    self.results.extend({"job_id": j, "status": "skipped"} for j in rest)
                return {"status": "failed", "results": self.results}
            self.results.append(result)
        return {"status": "completed", "results": self.results}
