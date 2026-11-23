"""Record the history behind tests/test_cast_revision_resync_replay.py.

Run on the commit BEFORE a G2 resync takes a revised cast lock (main c6cf8c5), from the repo root of a
checkout of that commit:

    python scripts/capture_cast_revision_resync_history.py tests/fixtures/replay/cast_revision_resync_refused.json

Real local Temporal dev server, the dry-run harness of the gate tests (tests.test_gate_correctness_round1:
two shots, the production activity list, the real studio DB) and a cast library in a temporary
directory. A01 has a character whose version 1 is locked:
  1. the episode passes G1; both stills land; A01 is approved at G2;
  2. the locked words of the character are rewritten with the lock hash recomputed over the same
     sheet (what an admin's revise-lock does; that route is not on this commit), and resync_shot_plans
     is refused, as a changed shot_meta was then (the first ``parked_events`` events: a run like
     ep61opr at G2 when this deploys);
  3. G2 is approved, both clips are made, the final cut is approved and the run completes.
Worker identities, the host name and machine paths (also inside the JSON payloads) are replaced (scrub).
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

EPISODE = "ep-cast-revision-resync"
NEW_DESCRIPTION = "a tall square robot with two blue eyes"


def _rewrite_locked_words(character_id: str, version: int, description: str) -> str:
    """The cast DB change of an admin's lock revision: new locked words, the lock hash recomputed over
    the same sheet images (hfvg.cast_store.revise_locked_version on later commits)."""
    from hfvg import cast_store
    from hfvg.cast_prompt import character_clause

    with cast_store.connect() as conn, cast_store._tx(conn):
        character = cast_store._character_row(conn, character_id)
        row = cast_store._version_row(conn, character_id, version)
        words = character_clause(character["name"], description, row["outfit"])
        conn.execute("UPDATE cast_versions SET locked_description = ? WHERE character_id = ? AND version = ?",
                     (words, character_id, version))
        row = cast_store._version_row(conn, character_id, version)
        roles = cast_store.SHEET_ROLES
        images = conn.execute("SELECT * FROM cast_images WHERE character_id = ? AND version = ? AND status = 'active' "
                              f"AND role IN ({', '.join('?' * len(roles))})", (character_id, version, *roles)).fetchall()
        digest = cast_store.lock_hash(cast_store._lock_snapshot(character, row, images))
        conn.execute("UPDATE cast_versions SET lock_hash = ? WHERE character_id = ? AND version = ?",
                     (digest, character_id, version))
    assert cast_store.verify_lock(character_id, version) == [], "the rewritten lock must still verify"
    return words


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
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    import hfvg
    from hfvg import cast_store
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    from scripts.capture_open_run_history import machine_names, scrub
    from tests.cast_helpers import complete_sheet, new_character
    from tests.test_gate_correctness_round1 import TASK_QUEUE, Recorder, _activities, _until, _wait, _write_episode

    assert Path(hfvg.__file__).resolve().is_relative_to(ROOT), f"hfvg imported from {hfvg.__file__}, not {ROOT}"
    os.environ.update({"DRY_RUN": "true", "DATABASE_PATH": str(base / "studio.db"),
                       "CAST_STORAGE_DIR": str(base / "private-cast")})
    for key in ("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "MODERATION_FALLBACK_MODELS", "HFVG_QC_JUDGES_ENABLED",
                "MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON", "SHOTS_JSON_PATH", "REFS_JSON_PATH"):
        os.environ.pop(key, None)
    os.chdir(base)
    await init_studio_db(os.environ["DATABASE_PATH"])
    await BudgetLedger(os.environ["DATABASE_PATH"]).init_db()
    await CreditLedger(os.environ["DATABASE_PATH"]).init_db()

    pip = new_character(("episode", EPISODE))
    complete_sheet(pip)
    old = cast_store.approve_version(pip["character_id"], 1, "t")["locked_description"]

    rec = Recorder()
    async with await WorkflowEnvironment.start_local() as env:
        async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(rec)):
            beatmap = _write_episode(base, EPISODE)
            shots_path = Path(beatmap).with_name("shots.json")
            shots = json.loads(shots_path.read_text())
            shots["shots"][0]["cast"] = [pip["slug"]]
            shots_path.write_text(json.dumps(shots))
            handle = await env.client.start_workflow(
                "EpisodeWorkflowV2", args=[EPISODE, beatmap, True, None, None, None], id=EPISODE,
                task_queue=TASK_QUEUE)
            await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
            await handle.execute_update("approve_g1_shot_plan")
            await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
            await _until(lambda: len(rec.still_calls) == 2, what="both stills")
            assert [c[9]["cast"][0]["clause"] for c in rec.still_calls if c[1] == "A01"] == [old], rec.still_calls
            await _settled(handle)
            await handle.execute_update("approve_shot", args=["A01"])

            new = _rewrite_locked_words(pip["character_id"], 1, NEW_DESCRIPTION)
            assert new != old
            try:
                await handle.execute_update("resync_shot_plans", args=["Pip's new look"])
                raise AssertionError("this commit refuses a resync that changes shot_meta")
            except WorkflowUpdateFailedError as e:
                refusal = str(e.cause)
            assert "A01: shot_meta changed" in refusal, refusal
            parked = await _settled(handle)
            print(f"parked at G2 after {len(parked)} events ({refusal})", flush=True)

            await handle.execute_update("approve_g2_scene_stills")
            await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
            assert len(rec.clips_for("A01")) == 1 and len(rec.clips_for("A02")) == 1, rec.clip_calls
            await handle.signal("approve_g_final_cut")
            await asyncio.wait_for(handle.result(), timeout=60)
            history = json.loads((await handle.fetch_history()).to_json())
            assert history["events"][:len(parked)] == parked, "the parked history is not a prefix"

    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    import temporalio
    names = machine_names(base)
    doc = {
        "workflow_id": EPISODE,
        "workflow_type": "EpisodeWorkflowV2",
        "captured_on_commit": commit,
        "temporalio": temporalio.__version__,
        "scenario": "A01 has a locked cast character; both stills land and A01 is approved at G2; the locked "
                    "words are rewritten (lock hash recomputed) and resync_shot_plans is refused (shot_meta "
                    "changed; the first parked_events events); then G2 approved -> both clips -> final cut "
                    "approved -> completed",
        "refusal": refusal,
        "words": {"before": old, "after": new},
        "parked_events": len(parked),
        "history": scrub(history, names),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes, {len(doc['history']['events'])} events)")


def main() -> None:
    out = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else \
        ROOT / "tests" / "fixtures" / "replay" / "cast_revision_resync_refused.json"
    base = Path(tempfile.mkdtemp(prefix="hfvg-replay-capture-"))
    try:
        asyncio.run(capture(out, base))
    finally:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
