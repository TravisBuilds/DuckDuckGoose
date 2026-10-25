"""Round 1 (PR #15): gate correctness and spend safety.

G-GATE-1  per-shot G2 decisions: a rejected still is never animated / charged; the DB only approves
          the stills that were kept.
G-QC-1    clip QC expects the dimensions the clip was generated at (9:16 derived from the shot's
          params via routing); an unresolvable spec fails BEFORE any paid clip submit.
G-MODEL-1 gpt_image_2 is refused with a plain error (no silent fallback).
G-RETRY-1 bounded clip retry after G2 (workflow update + API route), budget-checked before submit,
          deterministic job ids.
G-BUD-1   per-round USD caps and max paid retries per shot per round, enforced before submit on the
          USD ledger; the run report carries real caps.

Every workflow test runs the REAL EpisodeWorkflowV2 + ShotWorkflow with the production activity
list in DRY_RUN (no provider calls, sockets disabled); only the paid-submit activities are wrapped
to record their arguments (they still delegate to the real activities).
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx
from temporalio import activity
from temporalio.client import WorkflowUpdateFailedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.lint_helpers import lint_ok, lint_meta

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
TASK_QUEUE = "round1-tasks"

BEATMAP = """
# Round 1 test beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.5 (6.0) | Park | Day | D | Duck walks in park | Intro | - | - | K |
| 2 | A02 | 5.0 (6.0) | Park | Day | D G | Duck meets goose | Meeting | - | - | K |
"""


# ─────────────────────────────────────────────────────────────────────────────
# Harness
# ─────────────────────────────────────────────────────────────────────────────

class Recorder:
    """Arguments of every paid-submit activity call (the real activity still runs, in dry-run)."""

    def __init__(self):
        self.still_calls: list[tuple] = []
        self.clip_calls: list[tuple] = []
        self.clip_gate: asyncio.Event | None = None  # when set: clip RETRY submits wait for it

    def clips_for(self, shot_id: str) -> list[tuple]:
        return [c for c in self.clip_calls if c[1] == shot_id]

    @staticmethod
    def clip_retry_of(call: tuple):
        return call[13] if len(call) > 13 else None


def _write_episode(tmp_path: Path, episode_id: str) -> str:
    ep_dir = tmp_path / "data" / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "BEATMAP.md").write_text(BEATMAP)
    shots = [
        {"id": "A01", "prompt": "Duck walks in park", "count": 1, "characters": ["D"], "wet": False,
         "model": "kling", "duration": 6, "end_frame": False, "ref_keys": []},
        {"id": "A02", "prompt": "Duck meets goose", "count": 2, "characters": ["D", "G"], "wet": False,
         "model": "kling", "duration": 6, "end_frame": False, "ref_keys": []},
    ]
    (ep_dir / "shots.json").write_text(json.dumps({"shots": shots}))
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": [], "A02": []}}))
    (ep_dir / "prompt_kit.json").write_text("{}")
    return str(ep_dir / "BEATMAP.md")


def _activities(rec: Recorder, param_overrides: dict | None = None):
    from hfvg.worker import PRODUCTION_ACTIVITIES
    from hfvg.activities import studio_generation as sg
    from hfvg.activities import workflow_support as ws

    def name(a):
        return getattr(a, "__temporal_activity_definition").name

    wrapped = {"submit_clip_job_enforced", "submit_still_job_enforced", "parse_beatmap_activity"}
    acts = [a for a in PRODUCTION_ACTIVITIES if name(a) not in wrapped]

    @activity.defn(name="submit_still_job_enforced")
    async def still(*args):
        rec.still_calls.append(args)
        return await sg.submit_still_job_enforced(*args)

    @activity.defn(name="submit_clip_job_enforced")
    async def clip(*args):
        rec.clip_calls.append(args)
        if Recorder.clip_retry_of(args) and rec.clip_gate is not None:
            await rec.clip_gate.wait()
        return await sg.submit_clip_job_enforced(*args)

    @activity.defn(name="parse_beatmap_activity")
    async def parse(*args):
        result = await ws.parse_beatmap_activity(*args)
        for shot in result["shots"]:
            override = (param_overrides or {}).get(shot["shot_id"])
            if override:
                shot["params"] = {**(shot.get("params") or {}), **override}
        return result

    return [*acts, still, clip, parse]


async def _wait(handle, pred, timeout=30.0, what="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    state = None
    while loop.time() < end:
        state = await handle.query("get_state")
        if pred(state):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"{what} not reached; last state={state}")


async def _until(pred, timeout=30.0, what="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} not reached")


async def _refusal(coro) -> str | None:
    """Run a workflow update; return the refusal message (None = accepted). Lets a surviving
    mutation fail with a plain AssertionError instead of 'DID NOT RAISE'."""
    try:
        await coro
    except WorkflowUpdateFailedError as e:
        return str(e.cause or e)
    return None


@pytest.fixture
async def episode_env(tmp_path, monkeypatch):
    """Dry-run studio DB + ledgers in tmp_path (conftest already chdir'ed there)."""
    from hfvg.studio_db import init_studio_db
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger

    db_path = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    await CreditLedger(db_path).init_db()
    return db_path


async def _to_g2(env, rec, tmp_path, episode_id, param_overrides=None, credit_cap=None):
    """Start the episode, approve G1, wait until both stills were submitted and the shots parked."""
    beatmap = _write_episode(tmp_path, episode_id)
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2",
        args=[episode_id, beatmap, True, None, credit_cap, None],
        id=episode_id,
        task_queue=TASK_QUEUE,
    )
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _until(lambda: {c[1] for c in rec.still_calls} >= {"A01", "A02"}, what="both stills submitted")
    await asyncio.sleep(0.5)  # let the still review land before the human decision
    return handle


async def _shot_result(env, workflow_id):
    return await asyncio.wait_for(env.client.get_workflow_handle(workflow_id).result(), timeout=30)


def _worker(env, rec, param_overrides=None):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=_activities(rec, param_overrides))


# ─────────────────────────────────────────────────────────────────────────────
# G-GATE-1
# ─────────────────────────────────────────────────────────────────────────────

class TestPerShotG2Decisions:
    """G-GATE-1: per-shot G2 decisions (approve / reject / retry)."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_rejected_shot_never_reaches_clip_generation(self, episode_env, tmp_path):
        """Reject A02 at G2, approve G2: only A01's clip is submitted; A02's child ends 'rejected'
        without a clip; the DB marks A02 rejected and approves only A01."""
        rec = Recorder()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep91")
                result = await handle.execute_update("reject_shot", args=["A02", "Goose off-model"])
                assert result["action"] == "rejected"
                await handle.execute_update("approve_g2_scene_stills")
                state = await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")

                a02 = await _shot_result(env, "ep91-shot-A02")
                assert a02["status"] == "rejected", a02
                assert a02["reason"] == "Goose off-model"
                assert rec.clips_for("A02") == [], "a rejected still must never be animated (paid clip)"
                assert len(rec.clips_for("A01")) == 1, "the kept still must be animated exactly once"
                assert state["shot_decisions"] == {"A02": "rejected"}

                async with aiosqlite.connect(episode_env) as db:
                    async with db.execute(
                        "SELECT shot_id, status FROM shots WHERE episode_id = 'ep91' ORDER BY shot_id"
                    ) as cur:
                        statuses = dict(await cur.fetchall())
                assert statuses.get("A02") == "rejected", statuses
                assert statuses.get("A01") != "rejected", statuses
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_reject_refused_outside_g2_and_for_unknown_shot(self, episode_env, tmp_path):
        """reject_shot is validated: unknown shot refused at G2; refused once G2 is approved."""
        rec = Recorder()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep92")
                unknown = await _refusal(handle.execute_update("reject_shot", args=["Z99", "x"]))
                assert unknown is not None and "unknown shot" in unknown, unknown
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
                late = await _refusal(handle.execute_update("reject_shot", args=["A01", "too late"]))
                assert late is not None and "not at G2" in late, late
                assert len(rec.clips_for("A01")) == 1 and len(rec.clips_for("A02")) == 1
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    async def test_studio_db_only_approves_pending_shots(self, tmp_path):
        """approve_g2_shots approves only reviewed, kept stills: rejected (by id or by status),
        failed, blocked and still-less shots are never approved."""
        from hfvg.studio_db import init_studio_db, create_episode, approve_g2_shots

        db_path = str(tmp_path / "g2.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep99")
        rows = [
            ("A01", "still_complete", "https://example.com/a01.jpg"),
            ("A02", "needs_review", "https://example.com/a02.jpg"),
            ("A03", "still_complete", "https://example.com/a03.jpg"),  # rejected via workflow decision
            ("A04", "failed", "https://example.com/a04.jpg"),
            ("A05", "rejected", "https://example.com/a05.jpg"),  # rejected via API row
            ("A06", "blocked", "https://example.com/a06.jpg"),
            ("A07", "pending", None),  # no still yet
        ]
        async with aiosqlite.connect(db_path) as db:
            for shot_id, status, url in rows:
                await db.execute(
                    "INSERT INTO shots (id, episode_id, shot_id, prompt, status, still_url) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"ep99-{shot_id}", "ep99", shot_id, "p", status, url),
                )
            await db.commit()

        os.environ["DATABASE_PATH"] = db_path
        count = await approve_g2_shots("ep99", ["A03"])
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT shot_id, status FROM shots ORDER BY shot_id") as cur:
                statuses = dict(await cur.fetchall())
        assert statuses == {
            "A01": "approved", "A02": "approved", "A03": "rejected", "A04": "failed",
            "A05": "rejected", "A06": "blocked", "A07": "pending",
        }, statuses
        assert count == 2, f"only the 2 kept stills may be approved, got {count}"

    @pytest.mark.asyncio
    async def test_reject_still_persists_state_in_db(self, tmp_path, monkeypatch):
        """API reject: the workflow update runs first (args [shot_id, reason]); only an accepted
        rejection is written (status + rejection_reason + audit). A refusal is a 409 and the DB row
        is left untouched."""
        from temporalio.exceptions import ApplicationError
        from unittest.mock import AsyncMock, MagicMock
        from hfvg.studio_db import init_studio_db, create_episode

        db_path = str(tmp_path / "api.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep99")
        async with aiosqlite.connect(db_path) as db:
            for sid in ("A01", "A02"):
                await db.execute(
                    "INSERT INTO shots (id, episode_id, shot_id, prompt, status, still_url) VALUES (?, ?, ?, ?, ?, ?)",
                    (f"ep99-{sid}", "ep99", sid, "p", "still_complete", f"https://example.com/{sid}.jpg"),
                )
            await db.commit()
        client, api_main, wf_handle = await _api_client(db_path, monkeypatch)

        wf_handle.execute_update = AsyncMock(return_value={"success": True, "action": "rejected"})
        resp = client.post("/api/episodes/ep99/shots/A01/reject", data={"reason": "Low quality"})
        assert resp.status_code == 200, resp.text
        call = wf_handle.execute_update.await_args
        assert call.args == ("reject_shot",) and call.kwargs == {"args": ["A01", "Low quality"]}, call

        wf_handle.execute_update = AsyncMock(side_effect=ApplicationError("Cannot reject A02: not at G2 gate"))
        refused = client.post("/api/episodes/ep99/shots/A02/reject", data={"reason": "late"})
        assert refused.status_code == 409, refused.text
        assert "not at G2" in refused.json()["detail"]

        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT shot_id, status, rejection_reason FROM shots ORDER BY shot_id"
            ) as cur:
                got = await cur.fetchall()
            async with db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'reject_still'") as cur:
                audits = (await cur.fetchone())[0]
        assert got == [("A01", "rejected", "Low quality"), ("A02", "still_complete", None)], got
        assert audits == 1


async def _api_client(db_path, monkeypatch):
    from unittest.mock import MagicMock
    admin_secret = "a" * 32
    monkeypatch.setenv("ADMIN_SECRET", admin_secret)
    monkeypatch.setenv("DATABASE_PATH", db_path)
    sys.modules.pop("api.main", None)
    import api.main as api_main
    from fastapi.testclient import TestClient

    wf_handle = MagicMock()
    mock_client = MagicMock()
    mock_client.get_workflow_handle = MagicMock(return_value=wf_handle)
    api_main.temporal_client = mock_client
    client = TestClient(api_main.app)
    resp = client.post("/api/login", json={"admin_secret": admin_secret})
    assert resp.status_code == 200
    client.cookies = resp.cookies
    return client, api_main, wf_handle


# ─────────────────────────────────────────────────────────────────────────────
# G-QC-1
# ─────────────────────────────────────────────────────────────────────────────

def _make_clip(path: Path, size: str, seconds: float = 2.0):
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
           f"testsrc=size={size}:rate=24:duration={seconds}", "-pix_fmt", "yuv420p", "-y", str(path)]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr


class TestClipQCDefaults:
    """G-QC-1: clip QC expectations come from the shot (no 1024x576 / 16:9 default)."""

    def test_expected_dims_derived_from_params(self):
        from hfvg.qc.clip_expect import ClipSpecError, expected_clip_dims

        assert expected_clip_dims({"model": "kling"}) == {
            "resolution": "1080x1920", "aspect_ratio": "9:16", "source": "derived"}
        assert expected_clip_dims({"model": "kling", "aspect_ratio": "16:9"})["resolution"] == "1920x1080"
        assert expected_clip_dims({"model": "seedance", "aspect_ratio": "9:16"})["resolution"] == "1080x1920"
        # still tiers in params.resolution ("1k") are not clip dimensions and are ignored
        assert expected_clip_dims({"model": "kling", "resolution": "1k"})["resolution"] == "1080x1920"
        explicit = expected_clip_dims({"model": "kling", "clip_resolution": "720x1280"})
        assert explicit == {"resolution": "720x1280", "aspect_ratio": "9:16", "source": "explicit"}
        for bad in ({"model": "mystery-model"},
                    {"model": "kling", "clip_resolution": "1920x1080", "aspect_ratio": "9:16"}):
            err = None
            try:
                expected_clip_dims(bad)
            except ClipSpecError as e:
                err = e
            assert err is not None, f"{bad} must be refused"

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_clip_qc_fails_loudly_without_resolution(self, episode_env, tmp_path):
        """A shot whose expected clip dimensions cannot be established (here: a landscape
        clip_resolution on a 9:16 shot) fails at clip PRE-FLIGHT: no clip is submitted (no spend),
        the error names the problem, and the other shot is unaffected. (Unknown models are already
        refused at G1 preflight.)"""
        rec = Recorder()
        overrides = {"A02": {"clip_resolution": "1920x1080", "aspect_ratio": "9:16"}}
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec, overrides):
                handle = await _to_g2(env, rec, tmp_path, "ep93", param_overrides=overrides)
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", timeout=60, what="G_FINAL_CUT")
                a02 = await _shot_result(env, "ep93-shot-A02")
                assert a02["status"] == "failed", a02
                assert "Clip pre-flight QC failed" in a02.get("error", ""), a02
                assert rec.clips_for("A02") == [], "no paid clip submit for an unverifiable spec"
                assert len(rec.clips_for("A01")) == 1
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not HAVE_FFMPEG and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")
    async def test_synthetic_916_clip_passes_qc(self, tmp_path):
        """A real 1080x1920 clip passes motion QC against the dims derived for a 9:16 Kling shot."""
        from hfvg.qc.clip_expect import expected_clip_dims
        from hfvg.activities.qc import check_motion_qc

        clip = tmp_path / "portrait.mp4"
        _make_clip(clip, "1080x1920")
        dims = expected_clip_dims({"model": "kling", "aspect_ratio": "9:16"})
        result = await check_motion_qc(str(clip), 2.0, dims["resolution"], dims["aspect_ratio"])
        assert result["passed"] is True, result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not HAVE_FFMPEG and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")
    async def test_synthetic_169_clip_fails_qc_for_916_params(self, tmp_path):
        """A landscape 1920x1080 clip fails motion QC when the shot is 9:16."""
        from hfvg.qc.clip_expect import expected_clip_dims
        from hfvg.activities.qc import check_motion_qc

        clip = tmp_path / "landscape.mp4"
        _make_clip(clip, "1920x1080")
        dims = expected_clip_dims({"model": "kling"})
        result = await check_motion_qc(str(clip), 2.0, dims["resolution"], dims["aspect_ratio"])
        assert result["passed"] is False, result
        assert any("1920x1080" in issue or "aspect" in issue.lower() for issue in result["issues"]), result

    @pytest.mark.asyncio
    @pytest.mark.skipif(not HAVE_FFMPEG and os.getenv("REQUIRE_FFMPEG") != "1", reason="ffmpeg not installed")
    async def test_live_review_clip_without_dims_escalates(self, tmp_path, monkeypatch):
        """Live review_clip with a rubric lacking resolution/aspect escalates instead of checking
        against a landscape default (a 1024x576 clip would otherwise 'pass')."""
        from hfvg.activities.qc import review_clip
        from hfvg.studio_db import init_studio_db, create_episode

        db_path = str(tmp_path / "qc.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep99")
        async with aiosqlite.connect(db_path) as db:
            await db.execute("UPDATE episodes SET live_mode = 1, g108_approved = 1 WHERE episode_id = 'ep99'")
            await db.commit()
        monkeypatch.setenv("DATABASE_PATH", db_path)
        monkeypatch.setenv("DRY_RUN", "false")
        clip = tmp_path / "landscape.mp4"
        _make_clip(clip, "1024x576")
        with respx.mock(assert_all_called=False) as router:
            router.get("https://cdn.example.com/clip.mp4").mock(
                return_value=httpx.Response(200, content=clip.read_bytes()))
            result = await review_clip("https://cdn.example.com/clip.mp4", {"duration": 2.0},
                                       episode_id="ep99", shot_id="A01")
        assert result["passed"] is False, result
        assert result.get("escalate") is True, result
        assert "no expected resolution" in result["issues"][0], result


# ─────────────────────────────────────────────────────────────────────────────
# G-MODEL-1
# ─────────────────────────────────────────────────────────────────────────────

class TestModelDefaults:
    """G-MODEL-1: gpt_image_2 removed from defaults."""

    @pytest.mark.asyncio
    async def test_higgsfield_provider_refuses_gpt_image_2(self):
        """Higgsfield provider must raise ValueError for gpt_image_2, not fall back."""
        from hfvg.providers.higgsfield import HiggsfieldProvider

        provider = HiggsfieldProvider(dry_run=True)
        err = None
        try:
            await provider.submit_image(prompt="Test", model="gpt_image_2", resolution="1k")
        except ValueError as e:
            err = e
        assert err is not None, "gpt_image_2 must be refused, not silently mapped to another model"
        assert "gpt_image_2 is not available" in str(err)
        assert "MODEL-CAPS.md" in str(err)

    def test_base_provider_has_no_model_default(self):
        """submit_image signature must not have model default."""
        from hfvg.providers.base import GenerationProvider
        import inspect

        sig = inspect.signature(GenerationProvider.submit_image)
        model_param = sig.parameters.get("model")
        assert model_param is not None, "model parameter must exist"
        assert model_param.default == inspect.Parameter.empty, \
            "model parameter must have no default value"


# ─────────────────────────────────────────────────────────────────────────────
# G-BUD-1 (ledger)
# ─────────────────────────────────────────────────────────────────────────────

async def _round_error(coro):
    from hfvg.budget import RoundBudgetError
    try:
        await coro
    except RoundBudgetError as e:
        return e
    return None


class TestBudgetUnification:
    """G-BUD-1: USD ledger is the source of truth; per-round caps + paid-retry limit."""

    @pytest.mark.asyncio
    async def test_budget_ledger_uses_usd_micros(self, tmp_path):
        """Budget ledger must use USD micros as the unit."""
        from hfvg.budget import BudgetLedger

        db_path = tmp_path / "test_budget.db"
        ledger = BudgetLedger(str(db_path))
        await ledger.init_db()
        await ledger.init_episode_budget("ep99")
        async with aiosqlite.connect(str(db_path), uri=True) as db:
            async with db.execute("SELECT cap_usd_micros, stop_usd_micros FROM budget_lines LIMIT 1") as cursor:
                row = await cursor.fetchone()
        assert row is not None
        assert row[0] > 0, "cap_usd_micros must be positive"
        assert row[1] > 0, "stop_usd_micros must be positive"

    @pytest.mark.asyncio
    async def test_round_cap_refuses_before_submit(self, tmp_path, monkeypatch):
        """A round's held+committed total may not pass its 80% stop; a refusal holds nothing, a
        released (failed, uncharged) hold frees the room again, and the same job id is idempotent."""
        from hfvg.budget import BudgetLedger

        monkeypatch.setenv("ROUND_CAP_USD", "1.00")  # stop at 0.80
        monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "5")
        ledger = BudgetLedger(str(tmp_path / "round.db"))
        await ledger.init_db()
        await ledger.init_episode_budget("ep99")

        ok = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-a", 500_000, retry_key="c1")
        assert ok["authorized"] is True
        again = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-a", 500_000, retry_key="c1")
        assert again["authorized"] is True, "same job id must be idempotent"
        err = await _round_error(
            ledger.authorize_round_spend("ep99", "G3_CLIPS", "A02", "clip-b", 400_000, retry_key="c1"))
        assert err is not None and "stop" in str(err), "0.50 + 0.40 > 0.80 stop must be refused"
        assert await ledger.get_round_hold("clip-b") is None, "a refused job must not hold anything"
        status = await ledger.get_round_status("ep99")
        assert status["rounds"]["G3_CLIPS"]["total_usd_micros"] == 500_000, status
        # other round is independent
        other = await ledger.authorize_round_spend("ep99", "G2_STILLS", "A02", "still-b", 400_000, retry_key="r1")
        assert other["authorized"] is True
        await ledger.release_round_hold("clip-a")
        freed = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A02", "clip-b", 400_000, retry_key="c1")
        assert freed["authorized"] is True

    @pytest.mark.asyncio
    async def test_max_one_paid_retry_per_shot_per_round(self, tmp_path, monkeypatch):
        """Default: one paid retry per shot per round; the limit is configurable."""
        from hfvg.budget import BudgetLedger

        monkeypatch.delenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", raising=False)
        ledger = BudgetLedger(str(tmp_path / "retries.db"))
        await ledger.init_db()
        await ledger.init_episode_budget("ep99")
        first = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-c1", 100_000, retry_key="c1")
        assert first["authorized"] is True
        second = await _round_error(
            ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-c2", 100_000, retry_key="c2"))
        assert second is not None and "max 1 per shot per round" in str(second), second
        other_shot = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A02", "a02-c1", 100_000, retry_key="c1")
        assert other_shot["authorized"] is True
        monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "2")
        raised = await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-c2", 100_000, retry_key="c2")
        assert raised["authorized"] is True

    @pytest.mark.asyncio
    async def test_live_clip_retry_refused_before_provider_call(self, tmp_path, monkeypatch):
        """Live submit of a second paid clip retry for the same shot is refused BEFORE the provider
        is called; the line reservation and credits are released."""
        from tests.ledger_helpers import insert_line, set_balance
        from hfvg.activities.studio_generation import submit_clip_job_enforced
        from hfvg.budget import BudgetLedger
        from hfvg.credit_ledger import CreditLedger
        from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
        from tests.fake_image_passthrough import passthrough_normalize
        from temporalio.exceptions import ApplicationError

        db_path = str(tmp_path / "live.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep99")
        ledger = BudgetLedger(db_path)
        await ledger.init_db()
        async with aiosqlite.connect(db_path) as db:
            await insert_line(db, "ep99", "L4_video", 20_000_000, 16_000_000)
            await db.commit()
        await set_live_mode(db_path, "ep99", True)
        await approve_g108(db_path, "ep99")
        await set_balance(db_path)
        credits = CreditLedger(db_path)
        await credits.init_db()
        await credits.create_episode_line("ep99", cap_credits=500)
        # the shot already used its one paid clip retry this round
        await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-ep99-A01-v1-c1", 100_000, retry_key="c1")

        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
        monkeypatch.setenv("DATABASE_PATH", db_path)
        monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
        monkeypatch.setenv("MODEL_PATH_KLING_VIDEO", "kling-video/v3.0/pro/image-to-video")
        monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
        passthrough_normalize()
        before = await ledger.get_line_status("ep99", "L4_video")
        credits_before = await credits.get_episode_status("ep99")

        with respx.mock(assert_all_called=False) as router:
            router.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
                return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.075"}))
            router.route(method="GET").mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64))
            router.post("https://api.higgsfield.ai/files/generate-upload-url").mock(
                return_value=httpx.Response(200, json={"public_url": "https://cdn.example.com/s.png",
                                                       "upload_url": "https://upload.example.com/s"}))
            router.put("https://upload.example.com/s").mock(return_value=httpx.Response(200))
            submit = router.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
                return_value=httpx.Response(200, json={"request_id": "should-not-happen"}))
            err = None
            try:
                await submit_clip_job_enforced(
                    "ep99", "A01", "https://cdn.example.com/start.png", lint_ok("Duck walks"), 5.0, 1,
                    shot_meta=lint_meta(), clip_retry={"n": 2})
            except ApplicationError as e:
                err = e
            provider_called = submit.called

        assert err is not None and err.type == "RoundBudgetError", err
        assert provider_called is False, "the provider must not be called for a refused paid retry"
        after = await ledger.get_line_status("ep99", "L4_video")
        assert after["reserved"] == before["reserved"], (before, after)
        credits_after = await credits.get_episode_status("ep99")
        assert credits_after["reserved_credits"] == credits_before["reserved_credits"]
        assert await ledger.get_round_hold("clip-ep99-A01-v1-c2") is None


# ─────────────────────────────────────────────────────────────────────────────
# G-RETRY-1 (+ G-BUD-1 wiring, run report)
# ─────────────────────────────────────────────────────────────────────────────

class TestClipRetry:
    """G-RETRY-1: bounded clip retry after G2."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_retry_clip_reuses_approved_still_and_is_bounded(self, episode_env, tmp_path):
        """retry_clip re-runs ONLY the clip (no still submit), reuses the approved still, gets a new
        deterministic id (-clipretry1 / -c1), and a second paid retry in the round is refused."""
        rec = Recorder()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep94")
                early = await _refusal(handle.execute_update("retry_clip", args=["A01", "jitter", None]))
                assert early is not None and "G2 not approved" in early, early
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
                original = rec.clips_for("A01")
                assert len(original) == 1
                stills_before = len(rec.still_calls)

                no_reason = await _refusal(handle.execute_update("retry_clip", args=["A01", "  ", None]))
                assert no_reason is not None and "reason is required" in no_reason, no_reason
                queued = await handle.execute_update("retry_clip", args=["A01", "hand jitter", "Duck walks slowly"])
                assert queued["workflow_id"] == "ep94-shot-A01-clipretry1", queued
                assert queued["job_id"] == "clip-ep94-A01-v1-c1", queued
                assert queued["estimate_usd_micros"] > 0
                await _until(lambda: any(Recorder.clip_retry_of(c) for c in rec.clips_for("A01")),
                             timeout=20, what="clip retry submitted (without a new still)")
                assert len(rec.still_calls) == stills_before, "a clip retry must never re-submit a still"
                retry_result = await _shot_result(env, "ep94-shot-A01-clipretry1")
                assert retry_result["status"] != "failed", retry_result

                retries = [c for c in rec.clips_for("A01") if Recorder.clip_retry_of(c)]
                assert len(retries) == 1, rec.clips_for("A01")
                assert retries[0][2] == original[0][2], "the retry must animate the SAME approved still"
                assert retries[0][3] == "Duck walks slowly", "prompt override must reach the clip submit"
                assert Recorder.clip_retry_of(retries[0])["n"] == 1
                assert len(rec.still_calls) == stills_before, "a clip retry must never re-submit a still"

                state = await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry landed")
                assert state["clip_retries"] == {"A01": 1}
                second = await _refusal(handle.execute_update("retry_clip", args=["A01", "still jittery", None]))
                assert second is not None and "max paid clip retries" in second, second
                assert len([c for c in rec.clips_for("A01") if Recorder.clip_retry_of(c)]) == 1

                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_retry_clip_refused_for_rejected_shot(self, episode_env, tmp_path):
        """A still rejected at G2 can never be animated through retry_clip either."""
        rec = Recorder()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep95")
                await handle.execute_update("reject_shot", args=["A02", "off-model"])
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT"
                            and s["shot_outcomes"].get("A02", {}).get("status") == "rejected", what="A02 rejected")
                refused = await _refusal(handle.execute_update("retry_clip", args=["A02", "try anyway", None]))
                assert refused is not None and "rejected at G2" in refused, refused
                await asyncio.sleep(0.5)
                assert rec.clips_for("A02") == [], "a rejected still must never be animated"
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_retry_clip_refused_when_round_cap_exhausted(self, episode_env, tmp_path, monkeypatch):
        """With the G3 round cap too small for one clip, retry_clip is refused by the ledger
        pre-check before any child / submit."""
        monkeypatch.setenv("ROUND_CAP_USD", "0.01")
        rec = Recorder()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep96")
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
                refused = await _refusal(handle.execute_update("retry_clip", args=["A01", "jitter", None]))
                assert refused is not None and "round budget refused" in refused, refused
                state = await handle.query("get_state")
                assert state["clip_retries"] == {} and state["pending_clip_retries"] == [], state
                assert [c for c in rec.clip_calls if Recorder.clip_retry_of(c)] == []
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_final_cut_waits_for_pending_clip_retry_and_report_has_real_caps(self, episode_env, tmp_path):
        """Final-cut approval sent while a clip retry is still running is ignored (recorded); a new
        approval after the retry landed completes the run. The run report carries the real credit
        cap from the ledger (not a placeholder) and the round caps / retry counts."""
        rec = Recorder()
        rec.clip_gate = asyncio.Event()
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, rec):
                handle = await _to_g2(env, rec, tmp_path, "ep97", credit_cap=777)
                await handle.execute_update("approve_g2_scene_stills")
                await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
                await handle.execute_update("retry_clip", args=["A02", "goose blinks twice", None])
                await _until(lambda: any(Recorder.clip_retry_of(c) for c in rec.clip_calls), what="retry submit")

                await handle.signal("approve_g_final_cut")
                state = await _wait(handle, lambda s: s["final_cut_refusals"], timeout=10, what="refusal recorded")
                assert state["approvals"]["g_final_cut"] is False, state
                assert state["pending_clip_retries"] == ["A02"], state

                rec.clip_gate.set()
                await _wait(handle, lambda s: not s["pending_clip_retries"], what="retry landed")
                await handle.signal("approve_g_final_cut")
                result = await asyncio.wait_for(handle.result(), timeout=30)
                assert result["approvals"]["g_final_cut"] is True

        reports = list(Path("./data/reports").glob("ep97_full_*.json"))
        assert len(reports) == 1, reports
        report = json.loads(reports[0].read_text())
        assert report["total_credits_cap"] == 777, report["total_credits_cap"]
        assert report["rounds"]["clip_retries"] == {"A02": 1}, report["rounds"]
        assert report["rounds"]["max_paid_retries_per_shot_per_round"] == 1
        assert report["rounds"]["round_cap"] == 10_000_000
        assert report["budget_usd"]["episode_cap"] > 0
        md = reports[0].with_suffix(".md").read_text()
        assert "## Review rounds" in md and "max paid retries per shot per round: 1" in md

    @pytest.mark.asyncio
    async def test_retry_clip_api_route(self, tmp_path, monkeypatch):
        """POST /retry-clip runs the retry_clip update with [shot_id, reason, prompt_override],
        audits it, maps a refusal to 409 and requires a reason."""
        from temporalio.exceptions import ApplicationError
        from unittest.mock import AsyncMock
        from hfvg.studio_db import init_studio_db, create_episode

        db_path = str(tmp_path / "api.db")
        await init_studio_db(db_path)
        await create_episode(db_path, "ep99")
        client, api_main, wf_handle = await _api_client(db_path, monkeypatch)

        wf_handle.execute_update = AsyncMock(return_value={
            "status": "queued", "retry": 1, "job_id": "clip-ep99-A01-v1-c1",
            "workflow_id": "ep99-shot-A01-clipretry1"})
        ok = client.post("/api/episodes/ep99/shots/A01/retry-clip", json={"reason": "jitter"})
        assert ok.status_code == 200, ok.text
        call = wf_handle.execute_update.await_args
        assert call.args == ("retry_clip",) and call.kwargs == {"args": ["A01", "jitter", None]}, call

        wf_handle.execute_update = AsyncMock(side_effect=ApplicationError(
            "Cannot retry clip for A01: max paid clip retries per shot per round (1) reached"))
        refused = client.post("/api/episodes/ep99/shots/A01/retry-clip", json={"reason": "again"})
        assert refused.status_code == 409, refused.text
        assert "max paid clip retries" in refused.json()["detail"]

        missing = client.post("/api/episodes/ep99/shots/A01/retry-clip", json={"reason": ""})
        assert missing.status_code == 422
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("SELECT details FROM audit_log WHERE action = 'retry_clip'") as cur:
                audits = [r[0] for r in await cur.fetchall()]
        assert audits == ["Shot A01 clip retry 1 (clip-ep99-A01-v1-c1): jitter"], audits
