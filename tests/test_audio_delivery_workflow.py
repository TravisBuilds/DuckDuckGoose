"""Round 4 wiring: audio + delivery run after the mute edit + assembly QC and before G_FINAL_CUT,
and delivery QC feeds the Round 3 blocking-verdict mechanism. Real EpisodeWorkflowV2 on a local
Temporal server, real ffmpeg clips / tones, the production activity list; no provider calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment

from hfvg.qc.animal_count import FakeAnimalCounter, set_animal_counter
from tests.lint_helpers import lint_ok
from tests.test_audio_delivery import FAST_ENCODE, TEST_SIZE, make_noise, make_tone
from tests.test_moderation_recovery import Harness, _wait, dry_env  # noqa: F401
from tests.test_picture_assembly_round3 import MOVING, ClipFactory, _to_final_cut, _worker, needs_ffmpeg, no_counter  # noqa: F401

PROMPT = "A duck waddles through the snow"


def write_audio(tmp_path: Path, ep: str, l2_at: float = 2.5) -> Path:
    root = tmp_path / "data" / "episodes" / ep
    make_tone(root / "audio" / "vo" / "L01.wav", 1.2, 300, -4.0)
    make_tone(root / "audio" / "vo" / "L02.wav", 1.0, 360, -8.0)
    make_noise(root / "audio" / "lib" / "bed.wav", 4.0, -18.0)
    plan = {"min_vo_gap_s": 0.4,
            "vo": {"lines": [{"id": "L01", "at": 0.5, "path": "audio/vo/L01.wav", "text": "First line."},
                             {"id": "L02", "at": l2_at, "path": "audio/vo/L02.wav", "text": "Second line."}]},
            "music": {"path": "audio/lib/bed.wav"}, **FAST_ENCODE}
    (root / "audio" / "audio_plan.json").write_text(json.dumps(plan))
    return root


def _delivered(s: dict) -> bool:
    return bool((s.get("delivery") or {}).get("qc", {}).get("verdict"))


@needs_ffmpeg
class TestDeliveryInEpisode:
    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_delivery_pass_holds_for_final_cut_approval(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """PASS: deliverables at deterministic paths in state; the workflow still waits for a human
        G_FINAL_CUT approval (a passing delivery never auto-approves), then completes."""
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        root = write_audio(tmp_path, "ep71")
        h = Harness({})
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
                _, handle, state = await _to_final_cut(env, h, tmp_path, "ep71", lint_ok(PROMPT))
                assert state["assembly"]["qc"]["verdict"] == "PASS", state["assembly"]["qc"]
                state = await _wait(handle, _delivered, timeout=180, what="delivery QC")
                d = state["delivery"]
                assert d["qc"]["verdict"] == "PASS", d["qc"].get("failed")
                assert d["blocking"] is False and d["phase"] == "done"
                assert d["paths"]["dir"] == str(root / "delivery")
                for name in ("master_subs", "nosubs", "phone", "mix_wav", "srt", "manifest"):
                    assert Path(d["paths"][name]).is_file(), name
                assert state["output_paths"]["delivery"]["phone"] == d["paths"]["phone"]
                assert d["qc"]["edit_version"] == state["assembly"]["edit_version"]
                assert state["stage"] == "picture_lock"
                await asyncio.sleep(1.5)
                assert (await handle.describe()).status.name == "RUNNING", "delivery PASS must not auto-approve"
                state = await handle.query("get_state")
                assert state["approvals"]["g_final_cut"] is False
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_delivery_failure_blocks_final_cut_until_accepted(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """A VO gap below the validated minimum -> non-retryable mix error -> NEEDS_REVIEW (blocking):
        approval refused, a blank acceptance ignored, a reasoned acceptance unlocks G_FINAL_CUT."""
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        write_audio(tmp_path, "ep72", l2_at=1.85)  # L01 ends 1.7 s -> 0.15 s gap
        h = Harness({})
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
                _, handle, _ = await _to_final_cut(env, h, tmp_path, "ep72", lint_ok(PROMPT))
                state = await _wait(handle, _delivered, timeout=180, what="delivery QC")
                qc = state["delivery"]["qc"]
                assert qc["verdict"] == "NEEDS_REVIEW" and qc["blocking"] is True, qc
                assert "gap" in qc["reason"], qc["reason"]
                assert state["delivery"]["blocking"] is True
                await handle.signal("approve_g_final_cut")
                state = await _wait(handle, lambda s: s["final_cut_refusals"], what="refusal")
                assert "delivery QC NEEDS_REVIEW" in state["final_cut_refusals"][-1]
                assert state["approvals"]["g_final_cut"] is False
                await handle.signal("accept_delivery_qc", "   ")
                state = await _wait(handle, lambda s: len(s["final_cut_refusals"]) == 2, what="blank accept")
                assert state["delivery"]["acceptance"] is None
                await asyncio.sleep(1.0)
                assert (await handle.describe()).status.name == "RUNNING"
                await handle.signal("accept_delivery_qc", "gap checked by ear")
                state = await _wait(handle, lambda s: s["delivery"]["acceptance"], what="acceptance")
                assert state["delivery"]["acceptance"]["reason"] == "gap checked by ear"
                assert state["delivery"]["blocking"] is False
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    async def test_audio_waits_for_picture_lock(self, dry_env, tmp_path, no_counter):  # noqa: F811
        """Audio is built only for a locked picture: while assembly QC blocks (no animal checker ->
        NEEDS_REVIEW) no delivery runs; accepting the picture triggers the delivery."""
        write_audio(tmp_path, "ep73")
        h = Harness({})
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
                _, handle, state = await _to_final_cut(env, h, tmp_path, "ep73", lint_ok(PROMPT))
                assert state["assembly"]["qc"]["verdict"] == "NEEDS_REVIEW"
                await asyncio.sleep(2.0)
                state = await handle.query("get_state")
                assert state["delivery"]["delivery_version"] == 0 and not state["delivery"]["qc"], state["delivery"]
                assert not (tmp_path / "data" / "episodes" / "ep73" / "delivery").exists()
                await handle.signal("accept_assembly_qc", "checked by eye")
                state = await _wait(handle, _delivered, timeout=180, what="delivery after picture lock")
                assert state["delivery"]["delivery_version"] == 1
                assert state["delivery"]["qc"]["verdict"] == "PASS", state["delivery"]["qc"].get("failed")
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)

    @pytest.mark.asyncio
    @pytest.mark.timeout(240)
    async def test_dry_run_without_audio_plan_is_dry_and_non_blocking(self, dry_env, tmp_path, no_counter):  # noqa: F811
        set_animal_counter(FakeAnimalCounter({"animal": 1}))
        h = Harness({})
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, ClipFactory(tmp_path, [MOVING])):
                _, handle, _ = await _to_final_cut(env, h, tmp_path, "ep74", lint_ok(PROMPT))
                state = await _wait(handle, _delivered, timeout=120, what="dry delivery")
                assert state["delivery"]["qc"]["verdict"] == "DRY_RUN"
                assert state["delivery"]["blocking"] is False
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=60)


# ------------------------------------------------------------------ API, worker, CLI

@pytest.mark.asyncio
async def test_state_exposes_delivery_and_accept_sends_signal(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from tests.test_gate_correctness_round1 import _api_client

    client, api_main, wf_handle = await _api_client(str(tmp_path / "api.db"), monkeypatch)
    delivery = {"paths": {"dir": "/data/episodes/ep75/delivery", "phone": "/data/episodes/ep75/delivery/ep75_phone.mp4"},
                "manifest": {}, "qc": {"verdict": "FAIL", "blocking": True, "failed": ["phone_size"]},
                "acceptance": None, "blocking": True, "due": False, "delivery_version": 1, "phase": "done"}
    wf_handle.query = AsyncMock(return_value={
        "episode_id": "ep75", "stage": "picture_lock", "approvals": {}, "shots": 1, "g1_plan": {},
        "assembly": {"path": "/x.mp4"}, "delivery": delivery})
    wf_handle.signal = AsyncMock()
    resp = client.get("/api/episodes/ep75")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery"]["qc"]["verdict"] == "FAIL" and body["delivery"]["blocking"] is True
    assert body["delivery_paths"]["phone"].endswith("ep75_phone.mp4")
    resp = client.post("/api/episodes/ep75/delivery/accept", json={"reason": "size ok for this platform"})
    assert resp.status_code == 200, resp.text
    wf_handle.signal.assert_awaited_once_with("accept_delivery_qc", "size ok for this platform")
    assert client.post("/api/episodes/ep75/delivery/accept", json={"reason": ""}).status_code == 422


def test_worker_registers_audio_delivery_activities():
    from hfvg import worker

    names = {getattr(a, "__temporal_activity_definition").name for a in worker.PRODUCTION_ACTIVITIES}
    assert {"load_audio_plan", "mix_and_deliver", "check_delivery_qc", "generate_voiceover",
            "generate_sfx", "generate_music"} <= names


@needs_ffmpeg
@pytest.mark.timeout(240)
def test_cli_mix_on_episode_dir(tmp_path):
    from click.testing import CliRunner

    from hfvg.cli import cli
    from tests.test_audio_delivery import make_picture

    root = write_audio(tmp_path, "ep76")
    make_picture(root / "edit" / "mute_assembly.mp4", 6.0, size=TEST_SIZE)
    runner = CliRunner()
    res = runner.invoke(cli, ["mix", str(root), "--json"])
    assert res.exit_code == 0, res.output
    out = json.loads(res.output)
    assert out["qc"]["verdict"] == "PASS" and Path(out["manifest"]["paths"]["phone"]).is_file()
    res = runner.invoke(cli, ["deliver", str(root)])  # alias; same inputs -> reused
    assert res.exit_code == 0 and "reused" in res.output, res.output
    # a line that needs generation: refused offline, stand-in with --dry-run
    plan = json.loads((root / "audio" / "audio_plan.json").read_text())
    plan["vo"]["lines"].append({"id": "L03", "at": 4.2, "text": "Third."})
    (root / "audio" / "audio_plan.json").write_text(json.dumps(plan))
    res = runner.invoke(cli, ["mix", str(root)])
    assert res.exit_code != 0 and "generation runs only inside the episode workflow" in res.output, res.output
    res = runner.invoke(cli, ["mix", str(root), "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["qc"]["verdict"] == "DRY_RUN"
