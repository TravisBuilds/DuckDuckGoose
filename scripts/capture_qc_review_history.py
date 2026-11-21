"""Record the ShotWorkflow history behind tests/test_qc_judges_replay.py.

Run on the commit BEFORE the QC judges' workflow patches (main fd6d557), from the repo root of a
checkout of that commit:

    python scripts/capture_qc_review_history.py tests/fixtures/replay/qc_still_review_parked.json

Real local Temporal dev server, the dry-run harness of the workflow tests (tests.test_moderation_recovery
/ tests.test_picture_assembly_round3), the production activity list and real ffmpeg, with review_still
answering as a live review without a judge does (escalate to a person):
  1. the episode passes G1; the shot's still is generated, its review escalates, the shot records the
     still as needs_review and waits for G2 with its 24 h review timer (the state of a run parked at
     still review when the QC judges deploy);
  2. G2 is approved: the shot generates its clip, the clip review passes and the shot completes.
The fixture holds the shot's whole history and how many events it had while parked (a history only
grows, so the parked history is that prefix; the capture checks it). Worker identities, the host
name and machine paths (also inside the JSON payloads) are replaced (scrub).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EPISODE = "ep-qc-review-park"
SHOT = f"{EPISODE}-shot-A01"
ESCALATED = {"passed": False, "issues": ["Real QC agent not implemented - escalate to human review"],
             "escalate": True}


def _activity(event: dict) -> str | None:
    return event.get("activityTaskScheduledEventAttributes", {}).get("activityType", {}).get("name")


async def _parked(shot, timeout: float = 60.0) -> list[dict]:
    """The shot's events once it waits at still review: review_still escalated, needs_review recorded,
    no activity running, the review timer started, and nothing new over two polls."""
    loop = asyncio.get_running_loop()
    end, last = loop.time() + timeout, None
    while loop.time() < end:
        events = json.loads((await shot.fetch_history()).to_json())["events"]
        kinds = [e["eventType"] for e in events]
        names = [_activity(e) for e in events]
        review = names.index("review_still") if "review_still" in names else None
        recorded = review is not None and "record_shot_result" in names[review:]
        running = kinds.count("EVENT_TYPE_ACTIVITY_TASK_SCHEDULED") - kinds.count("EVENT_TYPE_ACTIVITY_TASK_COMPLETED")
        if recorded and running == 0 and kinds[-1] == "EVENT_TYPE_TIMER_STARTED":
            if last == len(events):
                return events
            last = len(events)
        await asyncio.sleep(0.5)
    raise AssertionError("the shot never parked at still review")


async def capture(out_path: Path, base: Path) -> None:
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from scripts.capture_open_run_history import machine_names, scrub
    from tests.lint_helpers import lint_ok, write_kit
    from tests.test_moderation_recovery import Harness, _child_result, _until, _wait
    from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, _write_episode

    assert shutil.which("ffmpeg"), "ffmpeg is required"
    os.environ.update({
        "DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db"),
        "EPISODES_DIR": str(base / "data" / "episodes"),
        "PROMPT_LINT_KIT_PATH": str(write_kit(base / "synthetic_lint_kit.json")),
    })
    for key in ("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "MODERATION_FALLBACK_MODELS", "HFVG_QC_JUDGES_ENABLED"):
        os.environ.pop(key, None)
    os.chdir(base)
    await init_studio_db(os.environ["DATABASE_PATH"])
    await BudgetLedger(os.environ["DATABASE_PATH"]).init_db()
    await CreditLedger(os.environ["DATABASE_PATH"]).init_db()

    reviews: list[dict] = []

    @activity.defn(name="review_still")
    async def review_still(asset_url, rubric, episode_id=None, shot_id=None):
        reviews.append(rubric)
        return dict(ESCALATED)

    def name(a):
        return getattr(a, "__temporal_activity_definition").name

    h = Harness({})
    clips = ClipFactory(base, [MOVING])
    acts = [a for a in h.activities("kling") if name(a) not in ("save_asset_locally", "review_still")]
    acts += [clips.activity(), review_still]
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=acts):
            beatmap = _write_episode(base, EPISODE, lint_ok("A duck sits on a bench by a pond"), None)
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None],
                id=EPISODE, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: reviews, what="still review")
            shot = env.client.get_workflow_handle(SHOT)
            parked = await _parked(shot)
            print(f"parked at still review after {len(parked)} events (rubric {reviews[0]!r})", flush=True)

            await handle.execute_update("approve_g2_scene_stills")
            result = await _child_result(env, SHOT, timeout=120)
            assert result["status"] == "completed", result
            history = json.loads((await shot.fetch_history()).to_json())
            assert history["events"][:len(parked)] == parked, "the parked history is not a prefix"
            await handle.terminate("capture done: the shot history is recorded")

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    import temporalio
    doc = {
        "workflow_id": SHOT,
        "workflow_type": "ShotWorkflow",
        "captured_on_commit": commit,
        "temporalio": temporalio.__version__,
        "scenario": "still review escalates (live review without a judge), the shot records needs_review and "
                    "parks at review with its 24 h timer (the first parked_events events); then G2 approved -> "
                    "clip generated -> clip review PASS -> completed",
        "parked_events": len(parked),
        "history": scrub(history, machine_names(base)),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(doc['history']['events'])} events)")


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        ROOT / "tests" / "fixtures" / "replay" / "qc_still_review_parked.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
