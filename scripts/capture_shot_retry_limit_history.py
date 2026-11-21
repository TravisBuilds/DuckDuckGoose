"""Record the EpisodeWorkflowV2 history behind tests/test_shot_retry_limit_replay.py.

A run parked at G2 whose history holds a retry refused at MAX_SHOT_RETRIES=1 by a worker on the code
before the retry_shot validator, then the deploy: a worker on this code with MAX_SHOT_RETRIES=2. Run
from the repo root (this checkout, committed) with a checkout of main c62f654 for the old worker:

    git worktree add /tmp/hfvg-old c62f654
    python scripts/capture_shot_retry_limit_history.py /tmp/hfvg-old \\
        tests/fixtures/replay/shot_retry_refused_then_limit_raised.json

Real local Temporal dev server, the dry-run harness of the workflow tests (tests.test_moderation_recovery /
tests.test_picture_assembly_round3), real ShotWorkflow children, real ffmpeg, the production data
converter. Each worker runs in its own process, so each reads its own MAX_SHOT_RETRIES at import:
  1. old worker (the old checkout, MAX_SHOT_RETRIES unset = 1): the episode runs to G2 and A01's still
     lands; retry_shot A01 is accepted (retry1) and its still lands; the next retry_shot A01 is refused
     by the handler with "Max retries (1) exceeded for shot A01", which the history records. The worker
     stops: the run is parked at G2 (the first ``parked_events`` events).
  2. deploy: a worker on this checkout with MAX_SHOT_RETRIES=2 replays the parked run (get_state),
     accepts retry_shot A01 (retry2) and its still lands; the next retry_shot A01 is refused by the
     validator ("Max retries (2) exceeded", nothing recorded); G2 is approved, the clip is made, the
     assembly QC passes, the final cut is approved and the run completes.
Worker identities, the host name and machine paths (also inside the JSON payloads) are replaced (scrub).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent

EPISODE = "ep-retry-limit"
PROMPT = "A duck waddles through the snow"
# update ids of the four retry_shot calls, in order
RETRY_1 = "owner-retry-1"
RETRY_2_REFUSED = "owner-retry-2-refused-at-limit-1"
RETRY_2_AFTER_DEPLOY = "owner-retry-2-after-deploy"
RETRY_3_REFUSED = "owner-retry-3-refused-at-limit-2"
PHASE_RESULT = "PHASE-RESULT "


def _use_checkout(code_root: Path) -> None:
    """Import hfvg and tests from ``code_root`` (the editable install points at this checkout)."""
    sys.path.insert(0, str(code_root))
    import hfvg
    import tests
    for module in (hfvg, tests):
        assert Path(module.__file__).resolve().is_relative_to(code_root.resolve()), module.__file__


async def _still_landed(client, workflow_id: str, timeout: float = 60.0) -> None:
    from temporalio.service import RPCError
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        try:
            if (await client.get_workflow_handle(workflow_id).query("get_state")).get("still_url"):
                return
        except RPCError:
            pass  # not started yet
        await asyncio.sleep(0.1)
    raise AssertionError(f"{workflow_id}: still never landed")


async def _refusal(handle, update_id: str) -> str:
    from temporalio.client import WorkflowUpdateFailedError
    try:
        await handle.execute_update("retry_shot", "A01", id=update_id)
    except WorkflowUpdateFailedError as e:
        return str(e.cause)
    raise AssertionError(f"retry_shot {update_id} was accepted")


async def _retry(client, handle, update_id: str, n: int) -> None:
    from tests.test_moderation_recovery import _wait
    result = await handle.execute_update("retry_shot", "A01", id=update_id)
    assert result["workflow_id"] == f"{EPISODE}-shot-A01-retry{n}", result
    await _wait(handle, lambda s: not s["pending_retries"], what=f"retry {n} landed")
    await _still_landed(client, f"{EPISODE}-shot-A01-retry{n}")


async def old_worker_phase(target: str, base: Path) -> dict:
    from temporalio.client import Client

    from hfvg.budget import BudgetLedger
    from hfvg.config import config
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.temporal_converter import temporal_data_converter
    from tests.lint_helpers import lint_ok, write_kit
    from tests.test_moderation_recovery import Harness, _wait
    from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, _worker, _write_episode

    assert config.MAX_SHOT_RETRIES == 1 and not hasattr(config, "LEGACY_MAX_SHOT_RETRIES"), "not the old code"
    write_kit(Path(os.environ["PROMPT_LINT_KIT_PATH"]))
    db_path = os.environ["DATABASE_PATH"]
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    await CreditLedger(db_path).init_db()
    client = await Client.connect(target, data_converter=temporal_data_converter)
    async with _worker(SimpleNamespace(client=client), Harness({}), ClipFactory(base, [MOVING])):
        beatmap = _write_episode(base, EPISODE, lint_ok(PROMPT))
        handle = await client.start_workflow(
            "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None],
            id=EPISODE, task_queue=TASK_QUEUE)
        await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
        await handle.execute_update("approve_g1_shot_plan")
        await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
        await _still_landed(client, f"{EPISODE}-shot-A01")
        await _retry(client, handle, RETRY_1, 1)
        refusal = await _refusal(handle, RETRY_2_REFUSED)
        assert refusal == "Max retries (1) exceeded for shot A01", refusal
        parked_events = len((await handle.fetch_history()).events)
    return {"parked_events": parked_events, "refusal": refusal}


async def new_worker_phase(target: str, base: Path) -> dict:
    from temporalio.client import Client

    from hfvg.config import config
    from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
    from hfvg.temporal_converter import temporal_data_converter
    from tests.test_moderation_recovery import Harness, _child_result, _wait
    from tests.test_picture_assembly_round3 import MOVING, ClipFactory, _worker

    assert (config.MAX_SHOT_RETRIES, config.LEGACY_MAX_SHOT_RETRIES) == (2, 1), "not the deployed config"
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    client = await Client.connect(target, data_converter=temporal_data_converter)
    env = SimpleNamespace(client=client)
    async with _worker(env, Harness({}), ClipFactory(base, [MOVING])):
        handle = client.get_workflow_handle(EPISODE)
        state = await handle.query("get_state")  # this worker replays the parked run first
        assert state["current_gate"] == "G2_SCENE_STILLS" and state["shot_attempts"] == {"A01": 2}, state
        assert not state["pending_retries"], state
        await _retry(client, handle, RETRY_2_AFTER_DEPLOY, 2)
        refusal = await _refusal(handle, RETRY_3_REFUSED)
        assert refusal == "Max retries (2) exceeded for shot A01", refusal
        await handle.execute_update("approve_g2_scene_stills")
        outcome = await _child_result(env, f"{EPISODE}-shot-A01-retry2", timeout=120)
        assert outcome["status"] == "completed", outcome
        state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                            and (s["assembly"]["qc"] or {}).get("verdict"), timeout=180, what="G_FINAL_CUT")
        assert state["assembly"]["qc"]["verdict"] == "PASS", state["assembly"]["qc"]
        await handle.signal("approve_g_final_cut")
        await asyncio.wait_for(handle.result(), timeout=120)
    return {"refusal": refusal}


def _run_phase(phase: str, target: str, base: Path, code_root: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MAX_SHOT_RETRIES", "LEGACY_MAX_SHOT_RETRIES", "MAX_PAID_RETRIES_PER_SHOT_PER_ROUND",
                        "MODERATION_FALLBACK_MODELS", "HFVG_QC_JUDGES_ENABLED")}
    env.update({
        "PYTHONPATH": str(code_root), "DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db"),
        "EPISODES_DIR": str(base / "data" / "episodes"),
        "PROMPT_LINT_KIT_PATH": str(base / "synthetic_lint_kit.json"),
    })
    if phase == "new":
        env["MAX_SHOT_RETRIES"] = "2"
    print(f"{phase} worker phase ({code_root})", flush=True)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--phase", phase, target, str(base),
                           str(code_root)], env=env, cwd=base, stdout=subprocess.PIPE, text=True, timeout=900)
    results = [line[len(PHASE_RESULT):] for line in proc.stdout.splitlines() if line.startswith(PHASE_RESULT)]
    if proc.returncode != 0 or not results:
        sys.stdout.write(proc.stdout)
        raise SystemExit(f"{phase} worker phase failed (exit {proc.returncode})")
    return json.loads(results[-1])


def _commit(checkout: Path) -> str:
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "hfvg"], cwd=checkout,
                           capture_output=True, text=True, check=True).stdout.strip()
    assert not dirty, f"{checkout}: uncommitted changes under hfvg/ (the fixture names the commit):\n{dirty}"
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True,
                          check=True).stdout.strip()


async def capture(old_checkout: Path, out_path: Path, base: Path) -> None:
    import temporalio
    from temporalio.testing import WorkflowEnvironment

    sys.path.insert(0, str(ROOT / "scripts"))
    from capture_open_run_history import machine_names, scrub

    commits = {"old_worker_commit": _commit(old_checkout), "new_worker_commit": _commit(ROOT)}
    async with await WorkflowEnvironment.start_local() as env:
        target = env.client.service_client.config.target_host
        old = _run_phase("old", target, base, old_checkout)
        print("old worker:", old, flush=True)
        new = _run_phase("new", target, base, ROOT)
        print("new worker:", new, flush=True)
        history = await env.client.get_workflow_handle(EPISODE).fetch_history()

    names = [(str(old_checkout), "/repo")] + machine_names(base)
    doc = {
        "workflow_id": EPISODE,
        "workflow_type": "EpisodeWorkflowV2",
        **commits,
        "temporalio": temporalio.__version__,
        "parked_events": old["parked_events"],
        "retry_update_ids": [RETRY_1, RETRY_2_REFUSED, RETRY_2_AFTER_DEPLOY, RETRY_3_REFUSED],
        "scenario": "old worker (MAX_SHOT_RETRIES=1): G2, retry_shot A01 accepted (retry1), the next one "
                    "refused in the handler 'Max retries (1) exceeded for shot A01' (recorded); parked at G2. "
                    "Deploy: new worker (MAX_SHOT_RETRIES=2) replays, accepts retry_shot A01 (retry2), the "
                    "validator refuses the next one (not recorded); G2 approved, clip, final cut approved, "
                    "completed",
        "history": scrub(json.loads(history.to_json()), names),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(doc['history']['events'])} events, "
          f"parked at {doc['parked_events']})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--phase", choices=("old", "new"), help=argparse.SUPPRESS)
    parser.add_argument("args", nargs="+")
    ns = parser.parse_args()
    if ns.phase:
        target, base, code_root = ns.args
        _use_checkout(Path(code_root))
        os.chdir(base)
        phase = old_worker_phase if ns.phase == "old" else new_worker_phase
        print(PHASE_RESULT + json.dumps(asyncio.run(phase(target, Path(base)))), flush=True)
        return
    old_checkout = Path(ns.args[0]).resolve()
    out = Path(ns.args[1]).resolve() if len(ns.args) > 1 else \
        ROOT / "tests" / "fixtures" / "replay" / "shot_retry_refused_then_limit_raised.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(old_checkout, out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
