"""Round 7 B1: pre-approved start stills on any number of shots.

2 of 4 shots are pre-approved: A01 through ``pre_approved_still`` in shots.json, A03 through an
anchor row (the anchor shot itself used to be skipped entirely and never got a clip). Both skip the
still submit (no still spend), are recorded as their G2 still flagged ``pre_approved`` and shown at
G2, then get a clip, QC, a clip-only retry and assembly like every other shot. A01's pre-approved
still is a continuity ref of A04 (refs.json ``pre_approved_still_of``).

Runs the REAL EpisodeWorkflowV2 + ShotWorkflow in DRY_RUN with the production activities
(tests/test_gate_correctness_round1 harness).
"""

import asyncio
import hashlib
import json
from pathlib import Path

import aiosqlite
import pytest
from PIL import Image
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment

from tests.test_gate_correctness_round1 import (
    TASK_QUEUE, Recorder, _refusal, _shot_result, _until, _until_recorded, _wait, _worker, episode_env,  # noqa: F401
)

BEATMAP4 = """
# Round 7 pre-approved beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.0 (6.0) | Park | Day | D | Duck walks in park | Intro | - | - | K |
| 2 | A02 | 5.0 (6.0) | Park | Day | D G | Duck meets goose | Meeting | - | - | K |
| 3 | A03 | 5.0 (6.0) | Park | Day | D | Duck looks at pond | Beat | - | - | K |
| 4 | A04 | 5.0 (6.0) | Park | Day | D | Duck waves goodbye | Outro | - | - | K |
"""


def _jpeg(path: Path, color) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (540, 960), color).save(path, "JPEG")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_episode4(tmp_path: Path, episode_id: str, a01_sha: str | None = None) -> str:
    ep_dir = tmp_path / "data" / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "BEATMAP.md").write_text(BEATMAP4)
    real_sha = _jpeg(ep_dir / "approved" / "A01.jpeg", (200, 180, 40))
    base = {"count": 1, "characters": ["D"], "wet": False, "model": "kling", "duration": 6, "end_frame": False}
    shots = [
        {**base, "id": "A01", "prompt": "Duck walks in park", "ref_keys": [],
         "pre_approved_still": {"path": "approved/A01.jpeg", "sha256": a01_sha or real_sha,
                                "approved_by": "owner"}, "trim_head_s": 0.5},
        {**base, "id": "A02", "prompt": "Duck meets goose", "count": 2, "characters": ["D", "G"], "ref_keys": [],
         "qc_fail_if": ["the goose vanishes", "the duck leaves the frame"]},
        {**base, "id": "A03", "prompt": "Duck looks at pond", "ref_keys": [], "trim_tail_s": 1.0},
        {**base, "id": "A04", "prompt": "Duck waves goodbye", "ref_keys": ["a01_still"]},
    ]
    (ep_dir / "shots.json").write_text(json.dumps({"shots": shots,
                                                   "continuity_hard_checks": ["relative_size", "headcount"]}))
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {
        "A01": [], "A02": [], "A03": [], "A04": [{"key": "a01_still", "pre_approved_still_of": "A01"}],
    }}))
    (ep_dir / "prompt_kit.json").write_text("{}")
    return str(ep_dir / "BEATMAP.md")


async def _anchor_a03(db_path: str, tmp_path: Path, episode_id: str) -> tuple[str, str]:
    """A03's still is owner-approved via an anchor row (A03 anchors A04)."""
    from hfvg.studio_db import set_anchor_still, upsert_episode
    path = tmp_path / "anchors" / "A03.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (540, 960), (30, 90, 160)).save(path, "PNG")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    await upsert_episode(db_path, episode_id, beatmap_path="x", live_mode=False)
    await set_anchor_still(db_path, episode_id, "A03", ["A04"], str(path), 1, sha, "owner")
    return str(path), sha


async def _start(env, tmp_path, episode_id, a01_sha=None):
    beatmap = _write_episode4(tmp_path, episode_id, a01_sha)
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[episode_id, beatmap, True, None, None, None],
        id=episode_id, task_queue=TASK_QUEUE,
    )
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    return handle


async def _db_shots(db_path, episode_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT shot_id, still_url, qc_results FROM shots WHERE episode_id = ?", (episode_id,)
        ) as cur:
            return {r[0]: (r[1], json.loads(r[2]) if r[2] else {}) for r in await cur.fetchall()}


class TestPreApprovedStills:

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    async def test_two_of_four_pre_approved_full_lifecycle_and_clip_retry(self, episode_env, tmp_path):
        rec = Recorder()
        ep = "ep71"
        anchor_path, anchor_sha = await _anchor_a03(episode_env, tmp_path, ep)
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _start(env, tmp_path, ep)
                g1 = (await handle.query("get_state"))["g1_plan"]
                assert g1["pre_approved_stills"] == ["A01", "A03"], g1
                by_id = {s["shot_id"]: s for s in g1["shots"]}
                assert by_id["A01"]["pre_approved"] and by_id["A03"]["pre_approved"]
                assert not by_id["A02"]["pre_approved"] and not by_id["A04"]["pre_approved"]
                assert g1["picture_seconds"] == 22.5, "4 x 6 s minus 0.5 s head (A01) and 1 s tail (A03)"
                await handle.execute_update("approve_g1_shot_plan")
                g2 = await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
                assert set(g2["pre_approved_stills"]) == {"A01", "A03"}, g2["pre_approved_stills"]
                assert g2["pre_approved_stills"]["A03"]["source"] == "anchor"
                assert g2["pre_approved_stills"]["A03"]["sha256"] == anchor_sha
                await _until(lambda: {c[1] for c in rec.still_calls} >= {"A02", "A04"}, what="paid stills")
                await _until_recorded(ep, {"A01", "A02", "A03", "A04"})

                # No still spend for the pre-approved shots
                assert sorted({c[1] for c in rec.still_calls}) == ["A02", "A04"], rec.still_calls

                # Recorded as the G2 still, flagged pre-approved (what the G2 page shows)
                rows = await _db_shots(episode_env, ep)
                for sid in ("A01", "A03"):
                    still_url, qc = rows[sid]
                    assert still_url.endswith(f"outputs/{sid}/still_v1.{'jpg' if sid == 'A01' else 'png'}"), still_url
                    assert Path(still_url).is_file()
                    assert qc["still_qc"]["pre_approved"] is True, qc
                assert rows["A03"][1]["still_qc"]["sha256"] == anchor_sha
                assert not rows["A02"][1].get("still_qc", {}).get("pre_approved"), rows["A02"]

                # A01's pre-approved still is a continuity ref of A04 (plus A03's anchor still)
                a04_refs = [c[4] for c in rec.still_calls if c[1] == "A04"][0]
                a01_src = str((tmp_path / "data" / "episodes" / ep / "approved" / "A01.jpeg").resolve())
                assert a01_src in a04_refs, a04_refs

                await handle.execute_update("approve_g2_scene_stills")
                state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
                # Trims reach the assembly (pre-approved shots included) and the picture length
                segs = {c["id"]: c for c in state["assembly"]["manifest"]["clips"]}
                assert sorted(segs) == ["A01", "A02", "A03", "A04"], "the first edit has every finished shot"
                assert state["assembly"]["edit_version"] == 1
                assert (segs["A01"]["trim_in"], segs["A01"]["trim_out"]) == (0.5, 6.0)
                assert (segs["A03"]["trim_in"], segs["A03"]["trim_out"]) == (0.0, 5.0)
                assert state["assembly"]["picture_seconds"] == 22.5, sorted(segs)
                # qc_fail_if reaches the clip QC of A02 (dry: recorded per clip as not checked)
                a02 = state["shot_outcomes"]["A02"]["qc_constraints"]
                assert a02 is not None, "A02 declares qc_fail_if: the clip QC must record its constraints"
                assert a02["status"] == "DRY_RUN" and a02["declared"] == ["the goose vanishes", "the duck leaves the frame"]
                assert state["shot_outcomes"]["A01"]["qc_constraints"] is None, "none declared on A01"
                # continuity_hard_checks reaches the assembly QC
                policy = state["assembly"]["qc"]["continuity_policy"]
                assert policy == {"hard": ["headcount", "relative_size"], "advisory": ["left_to_right_order"],
                                  "source": "episode"}, policy

                # Every shot (incl. both pre-approved) is animated exactly once, from its G2 still
                for sid in ("A01", "A02", "A03", "A04"):
                    assert len(rec.clips_for(sid)) == 1, (sid, rec.clips_for(sid))
                assert rec.clips_for("A01")[0][2] == rows["A01"][0]
                assert rec.clips_for("A03")[0][2] == rows["A03"][0]
                outcomes = state["shot_outcomes"]
                assert outcomes["A01"]["pre_approved"] and outcomes["A03"]["pre_approved"], outcomes
                assert all(outcomes[s]["clip_url"] for s in ("A01", "A02", "A03", "A04")), outcomes

                # B5: the moderation lint decision of every paid submit is kept in the shot state
                a02_state = await env.client.get_workflow_handle(f"{ep}-shot-A02").query("get_state")
                assert [r["status"] for r in a02_state["qc_constraints_by_clip"]] == ["DRY_RUN"]
                assert set(a02_state["moderation_lint"]) == {"still", "clip"}, a02_state["moderation_lint"]
                assert a02_state["moderation_lint"]["clip"]["decision"] in ("pass", "advisory")
                a01_state = await env.client.get_workflow_handle(f"{ep}-shot-A01").query("get_state")
                assert set(a01_state["moderation_lint"]) == {"clip"}, "pre-approved: no still submit to lint"

                # Clip-only retry of a pre-approved shot: no still submit, same approved still
                stills_before = len(rec.still_calls)
                try:
                    queued = await handle.execute_update("retry_clip", args=["A01", "arm jitter", None])
                except WorkflowUpdateFailedError as e:  # plain AssertionError for redgreen
                    raise AssertionError(f"clip-only retry of a pre-approved shot refused: {e.cause or e}") from e
                assert queued["workflow_id"] == f"{ep}-shot-A01-clipretry1", queued
                await _until(lambda: any(Recorder.clip_retry_of(c) for c in rec.clips_for("A01")),
                             timeout=30, what="A01 clip retry submitted")
                retry_result = await _shot_result(env, f"{ep}-shot-A01-clipretry1")
                assert retry_result["status"] == "completed", retry_result
                retries = [c for c in rec.clips_for("A01") if Recorder.clip_retry_of(c)]
                assert len(retries) == 1 and retries[0][2] == rows["A01"][0], retries
                assert len(rec.still_calls) == stills_before, "a clip retry must never re-submit a still"
                state = await _wait(handle, lambda s: not s["pending_clip_retries"]
                                    and s["current_gate"] == "G_FINAL_CUT", what="retry landed")
                assert state["clip_retries"] == {"A01": 1}
                assert state["shot_outcomes"]["A01"]["pre_approved"] is True

                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_pre_approved_sha_mismatch_fails_closed(self, episode_env, tmp_path):
        """A pre-approved still whose bytes do not match the declared sha256 is never used: the shot
        fails (no still spend substituted, no clip)."""
        rec = Recorder()
        ep = "ep72"
        await _anchor_a03(episode_env, tmp_path, ep)
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _start(env, tmp_path, ep, a01_sha="0" * 64)
                await handle.execute_update("approve_g1_shot_plan")
                await _wait(handle, lambda s: s["shots"] >= 4, what="shots launched")
                a01 = await _shot_result(env, f"{ep}-shot-A01")
                assert a01["status"] == "failed" and "sha256 mismatch" in a01["error"], a01
                assert "A01" not in {c[1] for c in rec.still_calls}, "no paid still may replace it"
                assert rec.clips_for("A01") == []
                await handle.terminate()


def test_pre_approved_still_of_requires_a_pre_approved_shot(tmp_path):
    from hfvg.shot_data import ShotDataError, resolve_refs
    ep_dir = tmp_path / "data" / "episodes" / "ep73"
    ep_dir.mkdir(parents=True)
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {
        "A02": [{"key": "a01", "pre_approved_still_of": "A01"}],
    }}))
    with pytest.raises(ShotDataError, match="without a pre_approved_still"):
        resolve_refs("ep73", {"A02": ["a01"]}, {})
    still = tmp_path / "a01.jpg"
    _jpeg(still, (1, 2, 3))
    assert resolve_refs("ep73", {"A02": ["a01"]}, {"A01": str(still)}) == {
        "A02": {"refs": [str(still)], "missing_refs": []}}
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {
        "A02": [{"key": "a01", "pre_approved_still_of": "A01", "path": str(still)}],
    }}))
    with pytest.raises(ShotDataError, match="cannot be combined"):
        resolve_refs("ep73", {"A02": ["a01"]}, {"A01": str(still)})
