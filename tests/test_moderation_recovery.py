"""G-REC-1 moderation recovery ladder: content blocks are classified distinctly, then recovered
one bounded, budget-checked, audited rung at a time (neutral reword -> validated fallback model ->
human review with the provider's exact message).

Workflow tests run the REAL EpisodeWorkflowV2 + ShotWorkflow with the production activity list in
DRY_RUN; only the paid submits are wrapped (to record arguments; they still delegate to the real
activities) and ``poll_job_status`` is wrapped to return a provider content block N times."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import aiosqlite
import httpx
import pytest
import respx
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from tests.lint_helpers import lint_meta, lint_ok

BLOCK_MSG = "Request rejected: content flagged by safety system (code NSFW-451)"
TASK_QUEUE = "moderation-tasks"
DOCS = Path(__file__).resolve().parent.parent / "docs" / "CONTENT-POLICY.md"


async def _audit_actions_for(db_path, episode_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT action, details FROM audit_log WHERE episode_id = ? ORDER BY id", (episode_id,)) as cur:
            return [(a, json.loads(d) if d and d.startswith("{") else d) for a, d in await cur.fetchall()]


async def _shot_row(db_path, episode_id, shot_id):
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT status, qc_results FROM shots WHERE id = ?",
                              (f"{episode_id}-{shot_id}",)) as cur:
            row = await cur.fetchone()
    if not row:
        return None, {}
    return row[0], (json.loads(row[1]) if row[1] else {})


# ─────────────────────────────────────────────────────────────────────────────
# Rung 1: deterministic neutral reword (never obfuscation)
# ─────────────────────────────────────────────────────────────────────────────

class TestNeutralReword:
    PROMPTS = [
        "A wet duck, soaked and dripping, waddles out of the pond",
        "A barefoot goose on exposed rocks during a film shoot",
        "Steamy bathroom mirror, a duck in a towel",
        "A duck walks in the park",
        "WET Duck. Dripping feathers; killer sunset view!",
    ]

    def test_reword_only_removes_or_neutralizes_listed_words(self):
        """Property: deterministic; every output word is an input word or a plain replacement
        word; every input word NOT on the documented list survives (in order); every listed word
        is gone."""
        from hfvg.activities.moderation_recovery import NEUTRALIZE, REPLACEMENT_VOCAB, sanitize_prompt

        word = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
        connectives = {"and", "or"}  # a dangling ", and," left by a removal may be tidied
        for prompt in self.PROMPTS:
            out, changes = sanitize_prompt(prompt)
            assert sanitize_prompt(prompt) == (out, changes), "reword must be deterministic"
            in_words = [w.lower() for w in word.findall(prompt)]
            out_words = [w.lower() for w in word.findall(out)]
            assert set(out_words) <= set(in_words) | REPLACEMENT_VOCAB, (prompt, out)
            # exactly: listed words removed / swapped in place, every other word kept in order
            expected = [NEUTRALIZE.get(w, w) for w in in_words]
            assert [w for w in expected if w and w not in connectives] == \
                [w for w in out_words if w not in connectives], (prompt, out)
            assert not [w for w in out_words if w in NEUTRALIZE and w not in REPLACEMENT_VOCAB], out
            assert [c["word"].lower() for c in changes] == [w for w in in_words if w in NEUTRALIZE]
            for c in changes:
                assert c["replacement"] == NEUTRALIZE[c["word"].lower()]

    def test_reword_examples(self):
        from hfvg.activities.moderation_recovery import sanitize_prompt

        assert sanitize_prompt("A wet duck, soaked and dripping, waddles out of the pond")[0] == \
            "A duck, waddles out of the pond"
        assert sanitize_prompt("A barefoot goose on exposed rocks during a film shoot")[0] == \
            "A goose on rocks during a film film"
        out, changes = sanitize_prompt("A duck walks in the park")
        assert (out, changes) == ("A duck walks in the park", [])
        # whole words only: "wetland" / "shooter" are not on the list
        assert sanitize_prompt("A duck in the wetland")[1] == []

    def test_word_lists_are_sensible(self):
        """No obfuscation tooling: replacements are plain dictionary words, never a hard-stop
        term, never another listed word; the two lists never overlap."""
        from hfvg.activities.moderation_recovery import HARD_STOP, NEUTRALIZE, REPLACEMENT_VOCAB

        assert not set(NEUTRALIZE) & HARD_STOP
        for repl in REPLACEMENT_VOCAB:
            assert re.fullmatch(r"[a-z]+", repl), repl
            assert repl not in HARD_STOP and repl not in NEUTRALIZE, repl
        assert 0 < len(NEUTRALIZE) <= 40, "keep the neutralize list short"

    def test_word_lists_documented_in_content_policy(self):
        """docs/CONTENT-POLICY.md lists exactly the NEUTRALIZE words and HARD_STOP terms."""
        from hfvg.activities.moderation_recovery import HARD_STOP, NEUTRALIZE

        text = DOCS.read_text()
        assert "## Moderation Recovery Ladder" in text
        section = text.split("## Moderation Recovery Ladder", 1)[1].split("\n## ", 1)[0]
        neutral_part, hard_part = section.split("### Hard-stop terms", 1)
        table_words = re.findall(r"^\| `([^`]+)` \|", neutral_part, re.M)
        assert sorted(table_words) == sorted(NEUTRALIZE), table_words
        assert sorted(re.findall(r"`([^`]+)`", hard_part)) == sorted(HARD_STOP)


# ─────────────────────────────────────────────────────────────────────────────
# Classification: only a provider moderation block is a content block
# ─────────────────────────────────────────────────────────────────────────────

async def _statuses(provider):
    from hfvg.providers.base import ProviderJobStatus as S

    expected = {"nsfw": S.BLOCKED, "blocked": S.BLOCKED, "canceled": S.CANCELED,
                "failed": S.FAILED, "completed": S.COMPLETED}
    got = {}
    with respx.mock(assert_all_called=False) as router:
        for raw in expected:
            router.get(f"https://api.test/requests/{raw}-job/status").mock(
                return_value=httpx.Response(200, json={"status": raw, "error": BLOCK_MSG}))
        try:
            for raw in expected:
                got[raw] = (await provider.get_job_status(f"{raw}-job")).status
        finally:
            await provider.close()
    return expected, got


class TestClassification:
    @pytest.mark.asyncio
    async def test_kling_status_mapping(self):
        from hfvg.providers.kling_video import KlingVideoProvider
        expected, got = await _statuses(KlingVideoProvider(api_key="id:secret", base_url="https://api.test"))
        assert got == expected

    @pytest.mark.asyncio
    async def test_seedance_status_mapping(self):
        from hfvg.providers.seedance_video import SeedanceVideoProvider
        expected, got = await _statuses(SeedanceVideoProvider(api_key="id:secret", base_url="https://api.test"))
        assert got == expected

    @pytest.mark.asyncio
    async def test_still_status_mapping(self):
        from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
        expected, got = await _statuses(HiggsfieldStillProvider(api_key="id:secret", base_url="https://api.test"))
        assert got == expected

    def test_failure_class(self):
        from hfvg.activities.studio_generation import failure_class
        assert failure_class("blocked") == "content_block"
        assert failure_class("canceled") == "canceled"
        assert failure_class("failed") == "provider_failure"
        assert failure_class("completed") is None


# ─────────────────────────────────────────────────────────────────────────────
# Ladder decisions (activity logic against a real studio DB + USD ledger)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def ladder_db(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.studio_db import init_studio_db

    monkeypatch.delenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", raising=False)
    monkeypatch.delenv("ROUND_CAP_USD", raising=False)
    monkeypatch.delenv("MODERATION_FALLBACK_MODELS", raising=False)
    db_path = str(tmp_path / "ladder.db")
    await init_studio_db(db_path)
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep99")
    return db_path


def _req(db_path, **kw):
    req = {
        "episode_id": "ep99", "shot_id": "A01", "kind": "clip", "db_path": db_path,
        "prompt": "A wet duck waddles through the reeds", "model": "kling", "duration": 6.0,
        "tags": [], "count": 1, "tier": None,
        "start_image_url": "https://cdn.example.com/start.png", "end_image_url": None,
        "provider_message": BLOCK_MSG, "blocked_job_id": "clip-ep99-A01-v1",
        "amount_usd_micros": 336_000, "steps_done": [], "next_n": 1, "paid_retries_used": 0,
        "attempts_left": 2,
    }
    req.update(kw)
    return req


class TestLadderDecisions:
    @pytest.mark.asyncio
    async def test_first_rung_is_neutral_reword_and_audited(self, ladder_db):
        from hfvg.activities.moderation_recovery import decide_next_step

        d = await decide_next_step(_req(ladder_db))
        assert d["action"] == "reword", d
        assert d["prompt"] == "A duck waddles through the reeds"
        assert d["retry"] == {"n": 1, "step": "reword"}
        assert d["changes"] == [{"word": "wet", "replacement": ""}]
        actions = await _audit_actions_for(ladder_db, "ep99")
        assert [a for a, _ in actions] == ["moderation_block", "moderation_reword"]
        assert actions[0][1]["provider_message"] == BLOCK_MSG
        assert actions[0][1]["failure_class"] == "content_block"
        assert actions[1][1]["prompt_before"] == "A wet duck waddles through the reeds"
        assert actions[1][1]["prompt_after"] == "A duck waddles through the reeds"

    @pytest.mark.asyncio
    async def test_hard_stop_terms_escalate_without_rewrite(self, ladder_db):
        from hfvg.activities.moderation_recovery import decide_next_step

        prompt = "A seductive wet duck pose"
        d = await decide_next_step(_req(ladder_db, prompt=prompt))
        assert d["action"] == "escalate", d
        assert d["prompt"] == prompt, "a hard-stop prompt must never be rewritten"
        assert d["provider_message"] == BLOCK_MSG
        assert "seductive" in d["reason"]
        status, qc = await _shot_row(ladder_db, "ep99", "A01")
        assert status == "needs_review"
        assert qc["moderation"]["provider_message"] == BLOCK_MSG
        actions = [a for a, _ in await _audit_actions_for(ladder_db, "ep99")]
        assert "moderation_reword" not in actions and actions[-1] == "moderation_escalated"

    @pytest.mark.asyncio
    async def test_fallback_after_reword_when_budget_allows(self, ladder_db, monkeypatch):
        """Second block after a reword: default limit (1 paid retry per shot per round) -> human
        review; with the limit raised to 2 -> validated fallback model (seedance -> kling)."""
        from hfvg.activities.moderation_recovery import decide_next_step

        kw = dict(model="seedance", prompt="A duck waddles through the reeds", steps_done=["reword"],
                  next_n=2, paid_retries_used=1)
        d = await decide_next_step(_req(ladder_db, **kw))
        assert d["action"] == "escalate", d
        assert "max 1 per shot per round" in d["reason"], d["reason"]

        monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "2")
        d = await decide_next_step(_req(ladder_db, **kw))
        assert d["action"] == "fallback", d
        assert d["model"] == "kling" and d["retry"] == {"n": 2, "step": "fallback"}
        assert d["prompt"] == "A duck waddles through the reeds"
        actions = [a for a, _ in await _audit_actions_for(ladder_db, "ep99")]
        assert "moderation_budget_refused" in actions and actions[-1] == "moderation_fallback"

    @pytest.mark.asyncio
    async def test_no_trigger_word_skips_reword(self, ladder_db):
        """No listed word: rung 1 is skipped (never resubmit an identical prompt); kling has no
        fallback by default -> human review."""
        from hfvg.activities.moderation_recovery import decide_next_step

        d = await decide_next_step(_req(ladder_db, prompt="A duck waddles through the reeds"))
        assert d["action"] == "escalate", d
        actions = [a for a, _ in await _audit_actions_for(ladder_db, "ep99")]
        assert actions == ["moderation_block", "moderation_reword_skipped", "moderation_escalated"]

    @pytest.mark.asyncio
    async def test_fallback_refused_by_model_caps_preflight(self, ladder_db):
        """A 20 s seedance clip cannot fall back to kling (3-15 s): the model_caps preflight
        refuses the rung, nothing is resubmitted."""
        from hfvg.activities.moderation_recovery import decide_next_step

        d = await decide_next_step(_req(ladder_db, model="seedance", duration=20.0,
                                        prompt="A duck waddles through the reeds"))
        assert d["action"] == "escalate", d
        actions = await _audit_actions_for(ladder_db, "ep99")
        refused = [det for a, det in actions if a == "moderation_fallback_refused"]
        assert refused, actions
        assert refused[0]["fallback_model"] == "kling"
        assert any("20" in e for e in refused[0]["errors"]), refused

    @pytest.mark.asyncio
    async def test_fallback_refused_by_routing(self, ladder_db, monkeypatch):
        """kling -> seedance fallback for a shot tagged wet is refused by the routing planner."""
        from hfvg.activities.moderation_recovery import decide_next_step

        monkeypatch.setenv("MODERATION_FALLBACK_MODELS", "kling:seedance")
        d = await decide_next_step(_req(ladder_db, tags=["wet"], prompt="A duck waddles through the reeds"))
        assert d["action"] == "escalate", d
        refused = [det for a, det in await _audit_actions_for(ladder_db, "ep99")
                   if a == "moderation_fallback_refused"]
        assert refused and refused[0]["errors"][0].startswith("routing:"), refused

    @pytest.mark.asyncio
    async def test_stills_have_no_fallback(self, ladder_db):
        from hfvg.activities.moderation_recovery import decide_next_step

        d = await decide_next_step(_req(ladder_db, kind="still", model=None, steps_done=["reword"],
                                        prompt="A duck waddles", amount_usd_micros=50_000))
        assert d["action"] == "escalate" and d["reason"] == "recovery ladder exhausted", d

    @pytest.mark.asyncio
    async def test_no_attempts_left_escalates(self, ladder_db):
        from hfvg.activities.moderation_recovery import decide_next_step

        d = await decide_next_step(_req(ladder_db, attempts_left=0))
        assert d["action"] == "escalate", d
        assert "no workflow attempts left" in d["reason"]

    @pytest.mark.asyncio
    async def test_round_ledger_refuses_when_shot_already_used_its_paid_retry(self, ladder_db):
        """The shot already holds its one paid G3 retry (a clip retry c1): the ledger pre-check
        refuses the moderation reword -> human review, no hold taken."""
        from hfvg.activities.moderation_recovery import decide_next_step
        from hfvg.budget import BudgetLedger

        ledger = BudgetLedger(ladder_db)
        await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-ep99-A01-v1-c1", 100_000,
                                           retry_key="c1")
        d = await decide_next_step(_req(ladder_db))
        assert d["action"] == "escalate", d
        assert "round ledger" in d["reason"], d["reason"]
        assert await ledger.get_round_hold("clip-ep99-A01-v1-m1") is None
        actions = [a for a, _ in await _audit_actions_for(ladder_db, "ep99")]
        assert "moderation_budget_refused" in actions and "moderation_reword" not in actions

    @pytest.mark.asyncio
    async def test_round_ledger_refuses_over_round_budget(self, ladder_db, monkeypatch):
        from hfvg.activities.moderation_recovery import decide_next_step

        monkeypatch.setenv("ROUND_CAP_USD", "0.10")  # stop at 0.08 < 0.336 clip estimate
        d = await decide_next_step(_req(ladder_db))
        assert d["action"] == "escalate", d
        assert "stop" in d["reason"], d["reason"]


# ─────────────────────────────────────────────────────────────────────────────
# Live submit wiring: -m{n} job id + round hold, refused before the provider call
# ─────────────────────────────────────────────────────────────────────────────

async def _live_episode(tmp_path, monkeypatch):
    from tests.fake_image_passthrough import passthrough_normalize
    from tests.ledger_helpers import insert_line, set_balance
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

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
    monkeypatch.delenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", raising=False)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("MODEL_PATH_KLING_VIDEO", "kling-video/v3.0/pro/image-to-video")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    passthrough_normalize()
    return db_path, ledger, credits


def _live_routes(router):
    router.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "7.5", "usd": "0.075"}))
    router.route(method="GET").mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64))
    router.post("https://api.higgsfield.ai/files/generate-upload-url").mock(
        return_value=httpx.Response(200, json={"public_url": "https://cdn.example.com/s.png",
                                               "upload_url": "https://upload.example.com/s"}))
    router.put("https://upload.example.com/s").mock(return_value=httpx.Response(200))
    return router.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "prov-m1"}))


class TestLiveModerationSubmit:
    @pytest.mark.asyncio
    async def test_moderation_resubmit_gets_own_job_id_and_round_hold(self, tmp_path, monkeypatch):
        from hfvg.activities.studio_generation import submit_clip_job_enforced

        db_path, ledger, _ = await _live_episode(tmp_path, monkeypatch)
        with respx.mock(assert_all_called=False) as router:
            submit = _live_routes(router)
            result = await submit_clip_job_enforced(
                "ep99", "A01", "https://cdn.example.com/start.png", lint_ok("Duck walks"), 5.0, 1,
                shot_meta=lint_meta(), moderation_retry={"n": 1, "step": "reword"})
            calls = submit.call_count
        assert result["job_id"] == "clip-ep99-A01-v1-m1", result
        assert calls == 1
        hold = await ledger.get_round_hold("clip-ep99-A01-v1-m1")
        assert hold is not None, "a paid moderation resubmit must hold G3 round budget"
        assert hold["retry_key"] == "m1" and hold["round_id"] == "G3_CLIPS" and hold["shot_id"] == "A01", hold

    @pytest.mark.asyncio
    async def test_moderation_resubmit_refused_before_provider_call(self, tmp_path, monkeypatch):
        from temporalio.exceptions import ApplicationError
        from hfvg.activities.studio_generation import submit_clip_job_enforced

        db_path, ledger, credits = await _live_episode(tmp_path, monkeypatch)
        await ledger.authorize_round_spend("ep99", "G3_CLIPS", "A01", "clip-ep99-A01-v1-c1", 100_000,
                                           retry_key="c1")
        before = await ledger.get_line_status("ep99", "L4_video")
        credits_before = await credits.get_episode_status("ep99")
        err = None
        with respx.mock(assert_all_called=False) as router:
            submit = _live_routes(router)
            try:
                await submit_clip_job_enforced(
                    "ep99", "A01", "https://cdn.example.com/start.png", lint_ok("Duck walks"), 5.0, 1,
                    shot_meta=lint_meta(), moderation_retry={"n": 1, "step": "reword"})
            except ApplicationError as e:
                err = e
            provider_called = submit.called
        assert err is not None and err.type == "RoundBudgetError", err
        assert provider_called is False
        assert (await ledger.get_line_status("ep99", "L4_video"))["reserved"] == before["reserved"]
        assert (await credits.get_episode_status("ep99"))["reserved_credits"] == credits_before["reserved_credits"]
        assert await ledger.get_round_hold("clip-ep99-A01-v1-m1") is None


# ─────────────────────────────────────────────────────────────────────────────
# The ladder inside the real workflows (dry run)
# ─────────────────────────────────────────────────────────────────────────────

BEATMAP = """
# Moderation test beatmap

| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | A01 | 5.5 (6.0) | Park | Day | D | Duck walks in park | Intro | - | - | K |
"""


class Harness:
    """Records paid submits; poll returns a provider block for the first N jobs of a kind."""

    def __init__(self, blocks: dict[str, int], status: str = "blocked"):
        self.blocks = dict(blocks)
        self.status = status
        self.still_calls: list[tuple] = []
        self.clip_calls: list[tuple] = []
        self.blocked_jobs: list[str] = []
        self._decided: dict[str, bool] = {}

    def activities(self, model: str = "kling"):
        from hfvg.worker import PRODUCTION_ACTIVITIES
        from hfvg.activities import studio_generation as sg
        from hfvg.activities import workflow_support as ws

        def name(a):
            return getattr(a, "__temporal_activity_definition").name

        wrapped = {"submit_clip_job_enforced", "submit_still_job_enforced", "poll_job_status",
                   "parse_beatmap_activity"}
        acts = [a for a in PRODUCTION_ACTIVITIES if name(a) not in wrapped]
        h = self

        @activity.defn(name="submit_still_job_enforced")
        async def still(*args):
            h.still_calls.append(args)
            return await sg.submit_still_job_enforced(*args)

        @activity.defn(name="submit_clip_job_enforced")
        async def clip(*args):
            h.clip_calls.append(args)
            return await sg.submit_clip_job_enforced(*args)

        @activity.defn(name="poll_job_status")
        async def poll(job_id, job_type, episode_id):
            if job_id not in h._decided:
                h._decided[job_id] = h.blocks.get(job_type, 0) > 0
                if h._decided[job_id]:
                    h.blocks[job_type] -= 1
            if h._decided[job_id]:
                h.blocked_jobs.append(job_id)
                return {"status": h.status, "output_url": None, "error": BLOCK_MSG, "cost": None,
                        "failure_class": sg.failure_class(h.status), "provider_message": BLOCK_MSG}
            return await sg.poll_job_status(job_id, job_type, episode_id)

        @activity.defn(name="parse_beatmap_activity")
        async def parse(*args):
            result = await ws.parse_beatmap_activity(*args)
            for shot in result["shots"]:
                shot["params"] = {**(shot.get("params") or {}), "model": model}
            return result

        return [*acts, still, clip, poll, parse]


def _clip_mod_retry(call):
    return call[14] if len(call) > 14 else None


def _still_mod_retry(call):
    return call[13] if len(call) > 13 else None


@pytest.fixture
async def dry_env(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import init_studio_db

    db_path = str(tmp_path / "studio.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", raising=False)
    monkeypatch.delenv("MODERATION_FALLBACK_MODELS", raising=False)
    await init_studio_db(db_path)
    await BudgetLedger(db_path).init_db()
    await CreditLedger(db_path).init_db()
    return db_path


def _write_episode(tmp_path: Path, episode_id: str, prompt: str, model: str) -> str:
    ep_dir = tmp_path / "data" / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "BEATMAP.md").write_text(BEATMAP)
    shots = [{"id": "A01", "prompt": prompt, "count": 1, "characters": ["D"], "wet": False,
              "model": model, "duration": 6, "end_frame": False, "ref_keys": []}]
    (ep_dir / "shots.json").write_text(json.dumps({"shots": shots}))
    (ep_dir / "refs.json").write_text(json.dumps({"per_shot": {"A01": []}}))
    (ep_dir / "prompt_kit.json").write_text("{}")
    return str(ep_dir / "BEATMAP.md")


def _worker(env, h, model="kling"):
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    return Worker(env.client, task_queue=TASK_QUEUE, workflows=[EpisodeWorkflowV2, ShotWorkflow],
                  activities=h.activities(model))


async def _wait(handle, pred, timeout=40.0, what="condition"):
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


async def _child_result(env, workflow_id, timeout=40.0):
    """Result of a child workflow that may not have been started yet."""
    from temporalio.service import RPCError
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        try:
            return await asyncio.wait_for(env.client.get_workflow_handle(workflow_id).result(),
                                          timeout=max(1.0, end - loop.time()))
        except RPCError:
            if loop.time() > end:
                raise AssertionError(f"{workflow_id} never started")
            await asyncio.sleep(0.2)


async def _run_episode(env, h, tmp_path, episode_id, prompt, model="kling", stills_only=False):
    beatmap = _write_episode(tmp_path, episode_id, prompt, model)
    handle = await env.client.start_workflow(
        "EpisodeWorkflowV2", args=[episode_id, beatmap, True, None, None, None],
        id=episode_id, task_queue=TASK_QUEUE)
    await _wait(handle, lambda s: s["current_gate"] == "G1_SHOT_PLAN", what="G1")
    await handle.execute_update("approve_g1_shot_plan")
    if stills_only:
        return await _child_result(env, f"{episode_id}-shot-A01"), handle
    await _wait(handle, lambda s: s["current_gate"] == "G2_SCENE_STILLS", what="G2")
    await _until(lambda: h.still_calls, what="still submitted")
    await asyncio.sleep(0.5)
    await handle.execute_update("approve_g2_scene_stills")
    result = await _child_result(env, f"{episode_id}-shot-A01")
    await _wait(handle, lambda s: s["current_gate"] == "G_FINAL_CUT", what="G_FINAL_CUT")
    return result, handle


class TestLadderInWorkflow:
    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_blocked_clip_reworded_and_resubmitted_once(self, dry_env, tmp_path):
        """One block on the clip: rung 1 rewords (listed words removed), the clip is resubmitted
        once with moderation_retry m1, lands; audited."""
        h = Harness({"clip": 1})
        prompt = lint_ok("A wet duck waddles through dripping reeds")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle = await _run_episode(env, h, tmp_path, "ep81", prompt)
                assert result["status"] not in ("failed", "blocked"), result
                assert len(h.clip_calls) == 2, h.clip_calls
                first, second = h.clip_calls
                assert first[3] == prompt and _clip_mod_retry(first) is None
                assert "wet" not in second[3].lower().split() and "dripping" not in second[3].lower(), second[3]
                assert "duck waddles through" in second[3] and "Exactly 1 animals in frame" in second[3]
                assert _clip_mod_retry(second) == {"n": 1, "step": "reword"}
                assert len(h.still_calls) == 1, "a clip block must not re-submit the still"
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)
        actions = [a for a, _ in await _audit_actions_for(dry_env, "ep81")]
        assert actions.count("moderation_block") == 1 and actions.count("moderation_reword") == 1, actions

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_repeated_block_escalates_with_exact_provider_message(self, dry_env, tmp_path):
        """Blocked again after the reword: the per-shot paid retry limit (1) stops the ladder; the
        shot goes to human review with the provider's exact message (child result, episode
        state, shots table)."""
        h = Harness({"clip": 5})
        prompt = lint_ok("A wet duck waddles through the reeds")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, model="seedance"):
                result, handle = await _run_episode(env, h, tmp_path, "ep82", prompt, model="seedance")
                assert result["status"] == "blocked" and result["needs_review"] is True, result
                assert result["provider_message"] == BLOCK_MSG
                assert result["moderation"]["provider_message"] == BLOCK_MSG
                assert len(h.clip_calls) == 2, [(_clip_mod_retry(c), c[8]) for c in h.clip_calls]
                state = await handle.query("get_state")
                assert any(f["shot_id"] == "A01" and f["status"] == "needs_review"
                           for f in state["failed_shots"]), state["failed_shots"]
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)
        status, qc = await _shot_row(dry_env, "ep82", "A01")
        assert status == "needs_review"
        assert qc["moderation"]["provider_message"] == BLOCK_MSG
        actions = [a for a, _ in await _audit_actions_for(dry_env, "ep82")]
        assert "moderation_escalated" in actions and "moderation_fallback" not in actions, actions

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_fallback_model_used_when_no_trigger_word(self, dry_env, tmp_path):
        """A blocked seedance clip with no listed word: rung 1 skipped, rung 2 re-routes to kling
        (validated) with the SAME prompt."""
        h = Harness({"clip": 1})
        prompt = lint_ok("A duck waddles through the reeds")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h, model="seedance"):
                result, handle = await _run_episode(env, h, tmp_path, "ep83", prompt, model="seedance")
                assert result["status"] not in ("failed", "blocked"), result
                assert [c[8] for c in h.clip_calls] == ["seedance", "kling"], h.clip_calls
                assert h.clip_calls[1][3] == prompt
                assert _clip_mod_retry(h.clip_calls[1]) == {"n": 1, "step": "fallback"}
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)
        actions = [a for a, _ in await _audit_actions_for(dry_env, "ep83")]
        assert "moderation_reword_skipped" in actions and "moderation_fallback" in actions, actions

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_hard_stop_still_block_goes_straight_to_review(self, dry_env, tmp_path):
        h = Harness({"still": 5})
        prompt = lint_ok("A seductive duck pose by the pond")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle = await _run_episode(env, h, tmp_path, "ep84", prompt, stills_only=True)
                assert result["status"] == "blocked" and result["needs_review"] is True, result
                assert len(h.still_calls) == 1, h.still_calls
                assert h.clip_calls == []
                await handle.terminate("test done")

    @pytest.mark.asyncio
    @pytest.mark.timeout(150)
    async def test_canceled_clip_never_enters_the_ladder(self, dry_env, tmp_path):
        """A canceled job is a provider failure, not a content block: no moderation retry."""
        h = Harness({"clip": 5}, status="canceled")
        prompt = lint_ok("A duck waddles through dripping reeds")
        async with await WorkflowEnvironment.start_local() as env:
            async with _worker(env, h):
                result, handle = await _run_episode(env, h, tmp_path, "ep85", prompt)
                assert result["status"] == "failed", result
                assert all(_clip_mod_retry(c) is None for c in h.clip_calls), h.clip_calls
                await handle.signal("approve_g_final_cut")
                await asyncio.wait_for(handle.result(), timeout=30)
        actions = [a for a, _ in await _audit_actions_for(dry_env, "ep85")]
        assert not [a for a in actions if a.startswith("moderation_")], actions
