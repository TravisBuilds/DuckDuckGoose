"""Record the EpisodeWorkflowV2 history behind tests/test_recompose_flags_replay.py.

A run parked at G_FINAL_CUT after a recompose recorded by a worker on the code before show profiles
(a recompose's shot_meta had no "flags"; no recompose-meta-flags patch), then the deploy: a worker on
this code replays it and runs a recompose whose shot_meta carries a show's shot flags. Run from the repo
root (this checkout, committed) with a checkout of main 76d22d1 for the old worker:

    git worktree add /tmp/hfvg-old 76d22d1
    python scripts/capture_recompose_flags_history.py /tmp/hfvg-old \\
        tests/fixtures/replay/recompose_with_flags.json

Real local Temporal dev server, the dry-run harness of the workflow tests (tests.test_moderation_recovery /
tests.test_picture_assembly_round3), real ShotWorkflow children, real ffmpeg, the production data
converter. Each worker runs in its own process:
  1. old worker (the old checkout): the episode runs to G_FINAL_CUT; recompose_shot A01 with shot_meta
     {"count": 2} (recompose1); its still parks and recompose_decision rejects it. The worker stops: the
     run is parked at G_FINAL_CUT (the first ``parked_events`` events).
  2. deploy: a worker on this checkout replays the parked run (get_state), runs recompose_shot A01 with
     shot_meta {"count": 1, "flags": FLAGS} (recompose2: the patch marker is recorded and the child's plan
     carries the flags); its still parks and is rejected; the final cut is approved and the run completes.
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

EPISODE = "ep-recompose-flags"
PROMPT = "A figure walks through the snow"
TWO = "Low close-up of two figures"
ONE = "Wide shot of one figure on the path"
FLAGS = {"on_camera": True, "crowd": "none"}
PATCH = "recompose-meta-flags"
# update ids of the recompose calls and their decisions, in order
RECOMPOSE_1, DECISION_1 = "owner-recompose-1", "owner-recompose-1-reject"
RECOMPOSE_2, DECISION_2 = "owner-recompose-2-flags", "owner-recompose-2-reject"
PHASE_RESULT = "PHASE-RESULT "


def _use_checkout(code_root: Path) -> None:
    """Import hfvg and tests from ``code_root`` (the editable install points at this checkout)."""
    sys.path.insert(0, str(code_root))
    import hfvg
    import tests
    for module in (hfvg, tests):
        assert Path(module.__file__).resolve().is_relative_to(code_root.resolve()), module.__file__


async def _settled(handle, timeout: float = 60.0) -> int:
    """The number of events once nothing runs and nothing new arrives over two polls."""
    loop = asyncio.get_running_loop()
    end, last = loop.time() + timeout, None
    while loop.time() < end:
        events = json.loads((await handle.fetch_history()).to_json())["events"]
        kinds = [e["eventType"] for e in events]
        open_tasks = kinds.count("EVENT_TYPE_WORKFLOW_TASK_SCHEDULED") - kinds.count("EVENT_TYPE_WORKFLOW_TASK_COMPLETED")
        if open_tasks == 0 and last == len(events):
            return len(events)
        last = len(events)
        await asyncio.sleep(0.5)
    raise AssertionError("the episode never settled")


async def _recompose_rejected(env, handle, n: int, update_id: str, decision_id: str, prompt: str,
                              shot_meta: dict, ref: Path) -> dict:
    """recompose_shot A01 (the n-th), wait for its still to park, reject it; the planned shot_meta."""
    from tests.test_moderation_recovery import _child_result, _wait
    out = await handle.execute_update(
        "recompose_shot", args=["A01", f"Studio: recompose {n}", prompt, [str(ref)], ["character"], None, shot_meta],
        id=update_id)
    assert out["workflow_id"] == f"{EPISODE}-shot-A01-recompose{n}", out
    await _wait(handle, lambda s: (s["recompose"]["pending"].get("A01") or {}).get("still_ready"),
                timeout=90, what=f"recompose {n} still parked")
    planned = (await handle.query("recompose_still_plans"))["A01"]["shot_meta"]
    await handle.execute_update("recompose_decision", args=["A01", False, f"recompose {n} rejected"], id=decision_id)
    rejected = await _child_result(env, f"{EPISODE}-shot-A01-recompose{n}", timeout=60)
    assert rejected["status"] == "rejected", rejected
    await _wait(handle, lambda s: not s["recompose"]["pending"], what=f"recompose {n} closed")
    return planned


async def _init_db() -> None:
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    db_path = os.environ["DATABASE_PATH"]
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    await CreditLedger(db_path).init_db()


async def old_worker_phase(target: str, base: Path) -> dict:
    from temporalio.client import Client

    from hfvg.compat.species_counter import FakeAnimalCounter, set_animal_counter
    from hfvg.temporal_converter import temporal_data_converter
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from tests.lint_helpers import lint_ok, write_kit
    from tests.test_moderation_recovery import Harness, _until, _wait
    from tests.test_picture_assembly_round3 import (
        MOVING,
        TASK_QUEUE,
        ClipFactory,
        _worker,
        _write_episode,
    )

    assert "flags" not in EpisodeWorkflowV2.RECOMPOSE_META_KEYS, "not the old code"
    write_kit(Path(os.environ["PROMPT_LINT_KIT_PATH"]))
    await _init_db()
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    ref = base / "character.png"
    ref.write_bytes(b"png")
    h = Harness({})
    client = await Client.connect(target, data_converter=temporal_data_converter)
    env = SimpleNamespace(client=client)
    async with _worker(env, h, ClipFactory(base, [MOVING])):
        beatmap = _write_episode(base, EPISODE, lint_ok(PROMPT))
        handle = await client.start_workflow(
            "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None], id=EPISODE, task_queue=TASK_QUEUE)
        await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
        await handle.execute_update("approve_g1_shot_plan")
        await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
        await _until(lambda: h.still_calls, what="still submitted")
        await asyncio.sleep(0.5)
        await handle.execute_update("approve_g2_scene_stills")
        state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                            and (s["assembly"]["qc"] or {}).get("verdict"), timeout=180, what="G_FINAL_CUT")
        assert state["assembly"]["qc"]["verdict"] == "PASS", state["assembly"]["qc"]
        planned = await _recompose_rejected(env, handle, 1, RECOMPOSE_1, DECISION_1, lint_ok(TWO, count=2),
                                            {"count": 2}, ref)
        assert planned["count"] == 2 and "flags" not in planned, planned
        parked_events = await _settled(handle)
    return {"parked_events": parked_events, "planned_shot_meta": planned}


async def new_worker_phase(target: str, base: Path) -> dict:
    from temporalio.client import Client

    from hfvg.compat.species_counter import FakeAnimalCounter, set_animal_counter
    from hfvg.temporal_converter import temporal_data_converter
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from tests.lint_helpers import lint_ok
    from tests.test_moderation_recovery import Harness, _wait
    from tests.test_picture_assembly_round3 import MOVING, ClipFactory, _worker

    assert "flags" in EpisodeWorkflowV2.RECOMPOSE_META_KEYS, "not the new code"
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    client = await Client.connect(target, data_converter=temporal_data_converter)
    env = SimpleNamespace(client=client)
    async with _worker(env, Harness({}), ClipFactory(base, [MOVING])):
        handle = client.get_workflow_handle(EPISODE)
        state = await handle.query("get_state")  # this worker replays the parked run first
        assert state["current_gate"] == "G_FINAL_CUT" and not state["recompose"]["pending"], state["recompose"]
        planned = await _recompose_rejected(env, handle, 2, RECOMPOSE_2, DECISION_2, lint_ok(ONE),
                                            {"count": 1, "flags": FLAGS}, base / "character.png")
        assert planned["count"] == 1 and planned["flags"] == FLAGS, planned
        await _wait(handle, lambda s: (s["assembly"]["qc"] or {}).get("verdict") == "PASS", what="edit QC")
        await handle.signal("approve_g_final_cut")
        await asyncio.wait_for(handle.result(), timeout=120)
    return {"planned_shot_meta": planned}


def _run_phase(phase: str, target: str, base: Path, code_root: Path) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("MAX_SHOT_RETRIES", "LEGACY_MAX_SHOT_RETRIES", "MAX_PAID_RETRIES_PER_SHOT_PER_ROUND",
                        "MODERATION_FALLBACK_MODELS", "HFVG_QC_JUDGES_ENABLED", "HFVG_SHOW_PROFILE_PATH")}
    env.update({
        "PYTHONPATH": str(code_root), "DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db"),
        "EPISODES_DIR": str(base / "data" / "episodes"),
        "PROMPT_LINT_KIT_PATH": str(base / "synthetic_lint_kit.json"),
    })
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
        "patch": PATCH,
        "flags": FLAGS,
        "update_ids": [RECOMPOSE_1, DECISION_1, RECOMPOSE_2, DECISION_2],
        "scenario": "old worker: G_FINAL_CUT, recompose_shot A01 with shot_meta {count: 2} (recompose1), its "
                    "still parked and rejected; parked at G_FINAL_CUT. Deploy: new worker replays, "
                    "recompose_shot A01 with shot_meta {count: 1, flags} (recompose2, patch marker recorded, "
                    "the child's plan carries the flags), its still parked and rejected; final cut approved, "
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
        ROOT / "tests" / "fixtures" / "replay" / "recompose_with_flags.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(old_checkout, out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
