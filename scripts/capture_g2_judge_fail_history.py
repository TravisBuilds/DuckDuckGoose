"""Record the histories behind tests/test_qc_fail_at_g2_replay.py.

Run on the commit BEFORE a judge's FAIL waits at G2 (main c6cf8c5), from the repo root of a checkout
of that commit:

    python scripts/capture_g2_judge_fail_history.py tests/fixtures/replay/g2_judge_fail_and_reapproved.json

Real local Temporal dev server, the dry-run harness of the gate tests (tests.test_gate_correctness_round1:
two shots, the production activity list, the real studio DB), with review_still answering a judge's
FAIL (escalate False) for A01's first still and PASS for every other:
  1. the episode passes G1; A01's still fails the judge and its shot ends failed (what a judge FAIL
     did before); A02's still passes, is rejected at G2 and then approved (a run like ep61opr at G2
     when this deploys: the first ``parked_events`` events of the episode);
  2. A01 is retried: the new still passes; G2 is approved, both clips are made, the final cut is
     approved and the run completes.
The fixture holds the episode's whole history (with the parked prefix) and the history of A01's
failed shot. Worker identities, the host name and machine paths (also inside the JSON payloads) are
replaced (scrub).
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

EPISODE = "ep-g2-judge-fail"
FAILED_SHOT = f"{EPISODE}-shot-A01"
JUDGE_FAIL = {"passed": False, "issues": ["QC judge: two ducks in frame, the prompt has one"], "escalate": False}


async def _settled(handle, timeout: float = 60.0) -> list[dict]:
    """The episode's events once nothing runs and nothing new arrives over two polls."""
    loop = asyncio.get_running_loop()
    end, last = loop.time() + timeout, None
    while loop.time() < end:
        events = json.loads((await handle.fetch_history()).to_json())["events"]
        kinds = [e["eventType"] for e in events]
        open_tasks = kinds.count("EVENT_TYPE_WORKFLOW_TASK_SCHEDULED") - kinds.count("EVENT_TYPE_WORKFLOW_TASK_COMPLETED")
        if open_tasks == 0 and last == len(events):
            return events
        last = len(events)
        await asyncio.sleep(0.5)
    raise AssertionError("the episode never settled at G2")


async def capture(out_path: Path, base: Path) -> None:
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    import hfvg
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from scripts.capture_open_run_history import machine_names, scrub
    from tests.test_gate_correctness_round1 import TASK_QUEUE, Recorder, _activities, _until, _wait, _write_episode

    assert Path(hfvg.__file__).resolve().is_relative_to(ROOT), f"hfvg imported from {hfvg.__file__}, not {ROOT}"
    os.environ.update({"DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db")})
    for key in ("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "MODERATION_FALLBACK_MODELS", "HFVG_QC_JUDGES_ENABLED"):
        os.environ.pop(key, None)
    os.chdir(base)
    await init_studio_db(os.environ["DATABASE_PATH"])
    await BudgetLedger(os.environ["DATABASE_PATH"]).init_db()
    await CreditLedger(os.environ["DATABASE_PATH"]).init_db()

    reviews: list[str] = []

    @activity.defn(name="review_still")
    async def review_still(asset_url, rubric, episode_id=None, shot_id=None):
        first = shot_id not in reviews
        reviews.append(shot_id)
        return dict(JUDGE_FAIL) if first and shot_id == "A01" else {"passed": True, "issues": []}

    rec = Recorder()
    acts = [a for a in _activities(rec) if getattr(a, "__temporal_activity_definition").name != "review_still"]
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=[*acts, review_still]):
            beatmap = _write_episode(base, EPISODE)
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None], id=EPISODE,
                task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: [f["shot_id"] for f in s["failed_shots"]] == ["A01"], what="A01 failed")
            failed = await asyncio.wait_for(env.client.get_workflow_handle(FAILED_SHOT).result(), timeout=30)
            assert failed["status"] == "failed" and "QC failed" in failed["error"], failed
            await _until(lambda: reviews.count("A02") == 1, what="A02 reviewed")
            await handle.execute_update("reject_shot", args=["A02", "goose off-model"])
            await handle.execute_update("approve_shot", args=["A02"])
            parked = await _settled(handle)
            print(f"parked at G2 after {len(parked)} events", flush=True)

            await handle.execute_update("retry_shot", args=["A01"])
            await _wait(handle, lambda s: s["shot_attempts"].get("A01") == 2 and not s["pending_retries"],
                        what="A01 retried")
            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            assert len(rec.clips_for("A01")) == 1 and len(rec.clips_for("A02")) == 1, rec.clip_calls
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=60)
            history = json.loads((await handle.fetch_history()).to_json())
            assert history["events"][:len(parked)] == parked, "the parked history is not a prefix"
            failed_history = json.loads((await env.client.get_workflow_handle(FAILED_SHOT).fetch_history()).to_json())

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    import temporalio
    names = machine_names(base)
    doc = {
        "workflow_id": EPISODE,
        "workflow_type": "EpisodeWorkflowV2",
        "captured_on_commit": commit,
        "temporalio": temporalio.__version__,
        "scenario": "A01's still fails the QC judge (escalate False) and its shot ends failed; A02's still passes, "
                    "is rejected at G2, then approved (the first parked_events events); then A01 retried (PASS) "
                    "-> G2 approved -> both clips -> final cut approved -> completed",
        "parked_events": len(parked),
        "history": scrub(history, names),
        "failed_shot": {"workflow_id": FAILED_SHOT, "workflow_type": "ShotWorkflow",
                        "history": scrub(failed_history, names)},
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(doc['history']['events'])} + "
          f"{len(doc['failed_shot']['history']['events'])} events)")


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        ROOT / "tests" / "fixtures" / "replay" / "g2_judge_fail_and_reapproved.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
