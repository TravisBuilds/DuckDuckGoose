"""Record the EpisodeWorkflowV2 history behind tests/test_heartbeat_open_run_replay.py.

Run on the commit BEFORE the subprocess heartbeat fix (e1c5181), from the repo root:

    python scripts/capture_open_run_history.py tests/fixtures/replay/open_run_heartbeat_timeout.json

Real local Temporal dev server, the dry-run harness of the workflow tests (tests.test_picture_assembly_round3
/ tests.test_audio_delivery_workflow), the production activity list and real ffmpeg:
  1. the episode runs to G_FINAL_CUT (picture assembled, assembly QC PASS) and the delivery starts;
  2. an ffmpeg shim holds the phone encode (pass 1) longer than mix_and_deliver's 2 min heartbeat
     timeout, so every attempt fails with "activity Heartbeat timeout" and the run parks at G_FINAL_CUT
     with the delivery NEEDS_REVIEW (the production state);
  3. "deploy": the orphaned encodes are killed and the shim is switched off; the delivery rebuild
     signal is sent, the rebuild delivers (QC PASS), G_FINAL_CUT is approved and the run completes.
Worker identities, the host name and machine paths (also inside the JSON payloads) are replaced (scrub).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

EPISODE = "ep-hb-open-run"
# > the 120 s heartbeat timeout of mix_and_deliver, and long enough that no timed-out attempt (which
# keeps running on main) finishes and publishes a delivery that a later attempt would then reuse
SLOW_PASS1_S = 900
ATTEMPT_WAIT_S = 330  # one timed-out attempt: up to a 60 s heartbeat flush + the 120 s timeout + backoff

SHIM = """#!/bin/sh
prev=""
for a in "$@"; do
  if [ "$prev" = "-pass" ] && [ "$a" = "1" ] && [ -f "$HB_CAPTURE_SLOW_FLAG" ]; then
    echo $$ >> "$HB_CAPTURE_PIDS"
    sleep {slow}
  fi
  prev="$a"
done
exec "$HB_CAPTURE_REAL_FFMPEG" "$@"
"""


def machine_names(base: Path) -> list[tuple[str, str]]:
    """Machine-specific strings in a history (worker host, checkout, interpreter, home, temp dir)."""
    import socket
    import sysconfig
    names = [(str(base), "/tmp/hfvg-replay-capture"), (str(ROOT), "/repo"),
             (sysconfig.get_paths()["purelib"], "/venv/site-packages"), (sys.prefix, "/venv"),
             (str(Path.home()), "/home/user"), (socket.gethostname(), "fixture-host")]
    return [(a, b) for a, b in names if a]


def scrub(obj, names: list[tuple[str, str]]):
    if isinstance(obj, dict):
        meta = obj.get("metadata")
        if isinstance(meta, dict) and "data" in obj and base64.b64decode(meta.get("encoding", "")) == b"json/plain":
            # payload bytes are base64 in the history JSON (activity inputs and results carry episode paths)
            text = scrub(base64.b64decode(obj["data"]).decode(), names)
            obj = {**obj, "data": base64.b64encode(text.encode()).decode()}
        return {k: ("fixture-worker" if k == "identity" else scrub(v, names)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, names) for v in obj]
    if isinstance(obj, str):
        for a, b in names:
            obj = obj.replace(a, b)
    return obj


async def capture(out_path: Path, base: Path) -> None:
    from temporalio.testing import WorkflowEnvironment

    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
    from hfvg.studio_db import init_studio_db
    from tests.lint_helpers import lint_ok, write_kit
    from tests.test_audio_delivery_workflow import PROMPT, write_audio
    from tests.test_moderation_recovery import Harness, _child_result, _until, _wait
    from tests.test_picture_assembly_round3 import MOVING, TASK_QUEUE, ClipFactory, _worker, _write_episode

    real_ffmpeg = shutil.which("ffmpeg")
    assert real_ffmpeg, "ffmpeg is required"
    shim_dir = base / "shim"
    shim_dir.mkdir(parents=True)
    (shim_dir / "ffmpeg").write_text(SHIM.replace("{slow}", str(SLOW_PASS1_S)))
    (shim_dir / "ffmpeg").chmod(0o755)
    flag, pids = base / "slow.flag", base / "slow.pids"
    flag.write_text("on")
    os.environ.update({
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
        "HB_CAPTURE_SLOW_FLAG": str(flag), "HB_CAPTURE_PIDS": str(pids), "HB_CAPTURE_REAL_FFMPEG": real_ffmpeg,
        "DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db"),
        "EPISODES_DIR": str(base / "data" / "episodes"),
        "PROMPT_LINT_KIT_PATH": str(write_kit(base / "synthetic_lint_kit.json")),
    })
    for key in ("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "MODERATION_FALLBACK_MODELS"):
        os.environ.pop(key, None)
    os.chdir(base)
    await init_studio_db(os.environ["DATABASE_PATH"])
    await BudgetLedger(os.environ["DATABASE_PATH"]).init_db()
    await CreditLedger(os.environ["DATABASE_PATH"]).init_db()
    set_animal_counter(FakeAnimalCounter({"animal": 1}))
    write_audio(base, EPISODE)

    h = Harness({})
    async with await WorkflowEnvironment.start_local() as env:
        async with _worker(env, h, ClipFactory(base, [MOVING])):
            # tests.test_picture_assembly_round3._to_final_cut, up to the assembly QC (the gate itself is
            # only entered once the first delivery returns)
            beatmap = _write_episode(base, EPISODE, lint_ok(PROMPT), None)
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None],
                id=EPISODE, task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: h.still_calls, what="still submitted")
            await asyncio.sleep(0.5)
            await handle.execute_update("approve_g2_scene_stills")
            await _child_result(env, f"{EPISODE}-shot-A01", timeout=90)
            state = await _wait(handle, lambda s: (s["assembly"]["qc"] or {}).get("verdict"),
                                timeout=120, what="assembly QC")
            assert state["assembly"]["qc"]["verdict"] == "PASS", state["assembly"]["qc"]
            print("picture locked; waiting for 3 heartbeat-timed-out mix_and_deliver attempts (~10 min)", flush=True)
            state = await _wait(handle, lambda s: (s["delivery"]["qc"] or {}).get("verdict"),
                                timeout=3 * ATTEMPT_WAIT_S, what="failed delivery")
            qc = state["delivery"]["qc"]
            assert qc["verdict"] == "NEEDS_REVIEW" and "Heartbeat timeout" in qc["reason"], qc
            state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            print("delivery 1:", qc["verdict"], qc["reason"], "| gate:", state["current_gate"], flush=True)

            # "deploy": the old worker's encodes die with it, the slow encode is gone
            flag.unlink()
            for pid in {int(p) for p in pids.read_text().split()}:
                subprocess.run(["pkill", "-KILL", "-P", str(pid)], check=False)
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            await asyncio.sleep(2.0)

            await handle.signal("request_delivery_rebuild", "rebuild after the heartbeat fix deploy")
            state = await _wait(handle, lambda s: s["delivery"]["delivery_version"] == 2
                                and s["delivery"]["phase"] == "done", timeout=300, what="rebuilt delivery")
            assert state["delivery"]["qc"]["verdict"] == "PASS", state["delivery"]["qc"]
            print("delivery 2:", state["delivery"]["qc"]["verdict"], flush=True)
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=120)
            history = await handle.fetch_history()

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    import temporalio
    doc = {
        "workflow_id": EPISODE,
        "workflow_type": "EpisodeWorkflowV2",
        "captured_on_commit": commit,
        "temporalio": temporalio.__version__,
        "scenario": "mix_and_deliver fails 3x with 'activity Heartbeat timeout' (phone encode held past the "
                    "2 min heartbeat timeout), run parks at G_FINAL_CUT with the delivery NEEDS_REVIEW; "
                    "then delivery rebuild signal -> rebuilt delivery PASS -> G_FINAL_CUT approved -> completed",
        "history": scrub(json.loads(history.to_json()), machine_names(base)),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(doc['history']['events'])} events)")


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        ROOT / "tests" / "fixtures" / "replay" / "open_run_heartbeat_timeout.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
