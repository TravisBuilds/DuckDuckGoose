"""Temporal worker for HFVG workflows and activities."""

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from hfvg import activities
from hfvg.config import config
from hfvg.ledger import Ledger
from hfvg.temporal_converter import temporal_data_converter
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main():
    """Start the Temporal worker."""
    logger.info("Initializing ledger database...")
    ledger = Ledger()
    await ledger.init_db()

    logger.info(f"Connecting to Temporal at {config.TEMPORAL_HOST}...")
    client = await Client.connect(
        config.TEMPORAL_HOST,
        namespace=config.TEMPORAL_NAMESPACE,
        data_converter=temporal_data_converter,
    )

    logger.info(f"Starting worker on task queue: {config.TASK_QUEUE}")
    worker = Worker(
        client,
        task_queue=config.TASK_QUEUE,
        workflows=[EpisodeWorkflow, EpisodeWorkflowV2, ShotWorkflow, PostingWorkflow],
        activities=[
            # Only enforced activities registered - legacy ungated activities removed
            activities.submit_still_job_enforced,
            activities.submit_clip_job_enforced,
            activities.await_job_enforced,
            activities.review_still,
            activities.review_clip,
            activities.trim_clips,
            activities.render_edit,
            activities.mix_audio,
            activities.generate_voiceover,
            activities.generate_sfx,
            activities.generate_music,
            activities.post_to_platform,
            activities.record_shot_result,
            activities.load_gate_policy_activity,
            activities.parse_beatmap_activity,
        ],
    )

    logger.info("Worker running. Press Ctrl+C to exit.")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
