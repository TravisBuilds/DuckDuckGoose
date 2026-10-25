"""Temporal worker for HFVG workflows and activities."""

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import Worker

from hfvg import activities
from hfvg.activities.run_report import generate_run_report
from hfvg.activities.credit_activities import (
    reserve_credits_activity,
    commit_credits_activity,
    release_credits_activity,
    update_credit_cap_activity,
    init_episode_credit_line_activity,
    init_episode_budget_lines_activity,
)
from hfvg.activities.run_lock import (
    acquire_run_lock,
    release_run_lock,
)
from hfvg.activities.studio_generation import verify_episode_files_activity
from hfvg.activities.ref_generation import (
    generate_ref_activity,
    load_refs_to_generate_activity,
)
from hfvg.activities.g1_plan import compute_g1_plan_activity
from hfvg.activities.save_assets import save_asset_locally
from hfvg.activities.pre_approved import prepare_pre_approved_still_activity
from hfvg.studio_db import approve_g2_shots, list_anchor_stills
from hfvg.config import config
from hfvg.ledger import Ledger
from hfvg.temporal_converter import temporal_data_converter
from hfvg.workflows import EpisodeWorkflow, PostingWorkflow, ShotWorkflow, ReconcilePendingJobsWorkflow
from hfvg.workflows.episode_v2 import EpisodeWorkflowV2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


PRODUCTION_ACTIVITIES = [
    activities.submit_still_job_enforced,
    activities.submit_clip_job_enforced,
    activities.await_job_enforced,
    activities.poll_job_status,
    activities.resolve_landed_start_still,
    activities.check_round_budget_activity,
    activities.commit_job_budget,
    activities.release_job_budget,
    activities.mark_job_pending_reconcile,
    activities.precheck_still_qc,
    activities.precheck_clip_qc,
    activities.review_still,
    activities.review_clip,
    activities.assemble_mute_edit,
    activities.check_assembly_qc,
    activities.trim_clips,
    activities.render_edit,
    activities.mix_audio,
    activities.generate_voiceover,
    activities.generate_sfx,
    activities.generate_music,
    activities.load_audio_plan,
    activities.mix_and_deliver,
    activities.check_delivery_qc,
    activities.post_to_platform,
    activities.record_shot_result,
    activities.load_gate_policy_activity,
    activities.parse_beatmap_activity,
    activities.preflight_validate_episode_activity,
    activities.recheck_refs_activity,
    activities.upsert_episode_activity,
    activities.approve_g108_activity,
    activities.check_live_mode_and_g108,
    verify_episode_files_activity,
    generate_ref_activity,
    load_refs_to_generate_activity,
    compute_g1_plan_activity,
    save_asset_locally,
    prepare_pre_approved_still_activity,
    approve_g2_shots,
    list_anchor_stills,
    generate_run_report,
    reserve_credits_activity,
    commit_credits_activity,
    release_credits_activity,
    update_credit_cap_activity,
    init_episode_credit_line_activity,
    init_episode_budget_lines_activity,
    acquire_run_lock,
    release_run_lock,
    activities.attempt_moderation_recovery,
    activities.get_pending_reconcile_jobs,
    activities.detect_stalled_jobs,
    activities.land_late_asset,
    activities.record_reconcile_outcome,
]


PRODUCTION_WORKFLOWS = [
    EpisodeWorkflow,
    EpisodeWorkflowV2,
    ShotWorkflow,
    PostingWorkflow,
    ReconcilePendingJobsWorkflow,
]


async def setup_schedules(client, task_queue: str) -> bool:
    """Create/update the reconciler + stall watchdog Temporal Schedule (idempotent).

    Disabled with RECONCILE_SCHEDULE_ENABLED=false (then run ``hfvg reconcile`` yourself).
    Returns True when the schedule was ensured."""
    from hfvg.workflows.reconcile import RECONCILE_SCHEDULE_ID, ensure_reconcile_schedule

    if os.getenv("RECONCILE_SCHEDULE_ENABLED", "true").lower() != "true":
        logger.warning("RECONCILE_SCHEDULE_ENABLED is not true: reconciler schedule NOT created")
        return False
    await ensure_reconcile_schedule(client, task_queue)
    logger.info(f"Reconcile schedule {RECONCILE_SCHEDULE_ID} ensured")
    return True


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
        workflows=PRODUCTION_WORKFLOWS,
        activities=PRODUCTION_ACTIVITIES,
    )

    # Reconciler + stall watchdog run on a Temporal Schedule (RECONCILE_INTERVAL_MINUTES)
    await setup_schedules(client, config.TASK_QUEUE)

    logger.info("Worker running. Press Ctrl+C to exit.")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
