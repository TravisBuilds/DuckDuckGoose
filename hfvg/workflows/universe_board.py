"""UniverseBoardWorkflow: make the pictures of one live mood board, one guarded paid call at a time.

Input (built by api/world_routes.py after the owner approved the estimate):
``{"request_id", "series_id", "jobs": [job, ...]}`` (hfvg.universe_boards.plan_jobs with each job's
share of the approved total). Jobs run in order; the first failure or refusal (price above the
approval, balance, budget stop, provider error) stops the board and nothing else is spent. Then
finish_universe_board_activity turns the pictures into the next board version, or records why the
board failed.

It runs on its own task queue (universe_task_queue): the API starts a board only while a worker
polls that queue, so a worker without this workflow (an older image mid deploy) gets the owner a
plain 409 instead of a board nobody makes. A new workflow type: open episode runs never see it.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from hfvg.activities.universe_board import (
        finish_universe_board_activity,
        generate_universe_board_image_activity,
    )

# A paid call is never retried automatically (a retry could pay twice); the activity resumes a
# submitted provider job by its persisted id when it runs again for the same job.
PAID = RetryPolicy(maximum_attempts=1)
QUICK = RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=1))


def universe_task_queue(task_queue: str) -> str:
    return f"{task_queue}-universe"


def universe_board_workflow_id(request_id: str) -> str:
    return f"universe-board-{request_id}"


async def worker_polls(client, task_queue: str) -> bool:
    """Whether a worker polls the mood board task queue of ``task_queue`` (recently, per the server)."""
    from temporalio.api.enums.v1 import TaskQueueType
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
    response = await client.workflow_service.describe_task_queue(
        DescribeTaskQueueRequest(namespace=client.namespace, task_queue=TaskQueue(name=universe_task_queue(task_queue)),
                                 task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
        timeout=timedelta(seconds=5))
    return len(response.pollers) > 0


@workflow.defn
class UniverseBoardWorkflow:
    def __init__(self) -> None:
        self.results: list[dict] = []

    @workflow.query
    def get_results(self) -> list[dict]:
        return self.results

    @workflow.run
    async def run(self, request: dict) -> dict:
        error = None
        for job in list(request.get("jobs") or []):
            try:
                result = await workflow.execute_activity(
                    generate_universe_board_image_activity, job,
                    start_to_close_timeout=timedelta(minutes=12),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=PAID,
                )
            except ActivityError as e:
                error = str(e.cause or e)
                self.results.append({"job_id": job["job_id"], "status": "failed", "error": error})
                break
            self.results.append(result)
        finished = await workflow.execute_activity(
            finish_universe_board_activity, args=[request["request_id"], error],
            start_to_close_timeout=timedelta(minutes=2), retry_policy=QUICK,
        )
        return {"status": "completed" if finished.get("status") == "done" else "failed", "results": self.results,
                "board_id": finished.get("board_id"), "error": error}
