"""G2.01 recheck write-back: re-rolls and clip launches read parsed_beatmap, so the recheck result
(refs produced after G1, e.g. a ref edit) must be merged there too, not only into the launched shots."""

import asyncio

import pytest

from tests.test_round4_shot_data import BEAT, SEEN_PLANS_M95, StubShotM95, _gate, _shot, _write_data  # noqa: F401
from tests.test_round4_shot_data import data_env  # noqa: F401  (fixture)


def test_merge_rechecked_refs_writes_back_to_parsed_beatmap():
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    wf = EpisodeWorkflowV2.__new__(EpisodeWorkflowV2)
    stale = {"shot_id": "A01", "refs": ["/have.png"], "missing_refs": ["r_new"], "prompt": "p"}
    other = {"shot_id": "B04", "refs": ["/have.png"], "missing_refs": [], "prompt": "q"}
    wf.parsed_beatmap = {"shots": [dict(stale), dict(other)]}
    rechecked = {"A01": {"refs": ["/have.png", "/new.png"], "missing_refs": []}}
    merged = wf._merge_rechecked_refs([dict(stale), dict(other)], rechecked)
    for rows in (merged, wf.parsed_beatmap["shots"]):
        a01 = next(s for s in rows if s["shot_id"] == "A01")
        assert a01["missing_refs"] == [] and a01["refs"] == ["/have.png", "/new.png"] and a01["prompt"] == "p"
        assert next(s for s in rows if s["shot_id"] == "B04") == other


@pytest.mark.asyncio
async def test_g2_reroll_after_recheck_uses_rechecked_refs(tmp_path, monkeypatch):
    """A G2 re-roll (retry_shot) of a shot whose ref was made after G1 launches with the RECHECKED refs,
    not the stale parse-time missing_refs (which held it at G2.01: "refs not on disk yet")."""
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108, list_anchor_stills, approve_g2_shots
    from hfvg.activities.credit_activities import init_episode_credit_line_activity, init_episode_budget_lines_activity
    import hfvg.activities as activities
    
    # Fresh DB for unique episode
    db_path = str(tmp_path / "rr9597.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    await init_studio_db(db_path)
    await create_episode(db_path, "ep_rr9597")
    await set_live_mode(db_path, "ep_rr9597", True)
    await approve_g108(db_path, "ep_rr9597")
    await init_episode_credit_line_activity("ep_rr9597", cap_credits=100_000)
    await init_episode_budget_lines_activity("ep_rr9597")
    
    # Create data files - r_have exists, r_new missing
    beat = tmp_path / "BEATMAP_RR9597.md"
    beat.write_text(BEAT.replace("ep98", "ep_rr9597"))
    data_dir = tmp_path / "rr9597data"
    data_dir.mkdir(exist_ok=True)
    shots = [_shot("A01"), _shot("B04", duration=3, ref_keys=["r_have"])]
    sp, rp, new = _write_data(data_dir, shots, make_new=False)
    monkeypatch.setenv("SHOTS_JSON_PATH", str(sp))
    monkeypatch.setenv("REFS_JSON_PATH", str(rp))
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")  # Bypass smoke test gate
    
    # Create activity mocks
    from temporalio import activity
    
    @activity.defn(name="preflight_validate_episode_activity")
    async def mock_preflight(*args):
        return {"validated": True, "warnings": []}
    @activity.defn(name="verify_episode_files_activity")
    async def mock_verify(*args):
        return True
    @activity.defn(name="load_refs_to_generate_activity")
    async def mock_load_refs(*args):
        return []
    @activity.defn(name="generate_run_report")
    async def mock_report(*args):
        return {"report_path": "/fake/report.txt"}
    @activity.defn(name="acquire_run_lock")
    async def mock_acquire_lock(*args):
        return None
    @activity.defn(name="release_run_lock")
    async def mock_release_lock(*args):
        return None
    @activity.defn(name="recheck_refs_activity")
    async def mock_recheck(episode_id: str, ref_keys_by_shot: dict):
        """Mock recheck that returns r_new present for M-97 test."""
        have_path = str(data_dir / "have.png")
        new_path = str(data_dir / "new.png")
        # Always return both refs present (simulating that r_new was created)
        return {
            "A01": {"refs": [have_path, new_path], "missing_refs": []},
            "B04": {"refs": [have_path], "missing_refs": []},
        }
    
    @activity.defn(name="compute_g1_plan_activity")
    async def mock_g1_plan(*args):
        # Stub G1 plan to avoid real /estimate API calls
        return {
            "shots": [],
            "total_still_credits": 0.0,
            "total_clip_credits": 0.0,
            "total_credits": 0.0,
            "total_still_usd": "0.00",
            "total_clip_usd": "0.00",
            "total_usd": "0.00",
            "estimates_available": True,
        }
    
    SEEN_PLANS_M95.clear()
    
    # Fresh Temporal env
    env = await WorkflowEnvironment.start_time_skipping()
    async with env:
        worker = Worker(
            env.client, task_queue="rr9597-q", workflows=[EpisodeWorkflowV2, StubShotM95],
            activities=[
                mock_acquire_lock, mock_release_lock,
                activities.upsert_episode_activity,
                init_episode_credit_line_activity, init_episode_budget_lines_activity,
                mock_preflight, mock_verify, mock_load_refs, mock_report, mock_recheck, mock_g1_plan,
                activities.load_gate_policy_activity, activities.parse_beatmap_activity,
                activities.check_live_mode_and_g108,
                activities.approve_g108_activity,
                list_anchor_stills,
                approve_g2_shots,
                activities.trim_clips, activities.render_edit, activities.mix_audio,
                activities.generate_voiceover, activities.generate_sfx, activities.generate_music,
                activities.post_to_platform,
            ],
        )
        async with worker:
            # Test M-97: recheck merge
            # The parse_beatmap initially sees r_new missing (from refs.json + filesystem check)
            # The mocked recheck returns r_new present (simulating file creation)
            # The workflow should merge the recheck data into shot plans
            handle = await env.client.start_workflow(
                EpisodeWorkflowV2.run,
                args=["ep_rr9597", str(beat), False],
                id="wf-rr9597",
                task_queue="rr9597-q",
            )
            
            # Approve G1
            await handle.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
            state = await _gate(handle, "G2_SCENE_STILLS")
            state = await handle.query(EpisodeWorkflowV2.get_state)
            
            first = SEEN_PLANS_M95["A01"]
            assert first["missing_refs"] == []
            # Parse time saw r_new missing for A01; the re-roll must use the rechecked plan.
            res = await handle.execute_update("retry_shot", args=["A01", "RE-ROLL FIX: deeper."])
            assert res["status"] == "queued", res
            end = asyncio.get_event_loop().time() + 10
            while asyncio.get_event_loop().time() < end and SEEN_PLANS_M95["A01"].get("attempt") != 2:
                await asyncio.sleep(0.1)
            plan = SEEN_PLANS_M95["A01"]
            assert plan.get("attempt") == 2, plan.get("attempt")
            assert plan["missing_refs"] == [], f"re-roll launched with stale missing_refs {plan['missing_refs']}"
            assert any("new.png" in str(r) for r in plan["refs"]), plan["refs"]
            assert plan["prompt"].endswith("RE-ROLL FIX: deeper.")
            await handle.terminate()


