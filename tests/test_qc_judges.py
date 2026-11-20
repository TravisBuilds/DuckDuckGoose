"""Automated QC judges, the judge service (hfvg/qc/judges.py, docs/QC-JUDGES.md).

Every paid judge call is cached by its inputs, passes the balance gate, is held on the episode's
qc_judges budget line before the call and committed (token usage) or released after it. Every refusal
or failure is JudgeUnavailable, which the callers record as NEEDS_REVIEW: never an answer, never a
pass. HFVG_QC_JUDGES_ENABLED (default off) registers nothing when off. The judge model is
FakeJudgeClient (or a respx-mocked OpenAI endpoint): no test makes a network call or pays."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import sqlite3
from datetime import timedelta

import pytest

from hfvg.qc import judges
from hfvg.qc.judges import (
    DEFAULT_MODEL, FakeJudgeClient, JudgeCall, JudgeService, JudgeUnavailable, OpenAIJudgeClient,
    call_estimate_usd_micros, register_qc_judges,
)
from tests.qc_judge_helpers import live_ledger, qc_line, qc_transactions, reset_registries, verdict_rows

EP = "ep-qcj"
SYSTEM = "Answer with a JSON object."
TEXT = "Is the square blue?"


@pytest.fixture
async def db(tmp_path, monkeypatch):
    reset_registries()
    path = await live_ledger(tmp_path, monkeypatch)
    yield path
    reset_registries()


def _call(episode_id: str | None = EP, media: str = "media-sha-1", **kw) -> JudgeCall:
    kw.setdefault("validate", lambda answer: {"ok": bool(answer["ok"])})
    return JudgeCall(kind="test_judge", episode_id=episode_id, subject="A01 still_v1.png", system=SYSTEM,
                     text=TEXT, images=[b"image-1"], key_parts={"media": media}, **kw)


def _estimate() -> int:
    return call_estimate_usd_micros(1, len(SYSTEM) + len(TEXT), judges.DEFAULT_MAX_OUTPUT_TOKENS)


# 1000 tokens in at $0.40 / M + 100 tokens out at $1.60 / M (FakeJudgeClient's usage)
TOKEN_COST = 560


# ------------------------------------------------------------------------------------------ flag


def test_flag_is_off_by_default_and_registers_nothing(monkeypatch):
    from hfvg.qc.animal_count import get_animal_counter
    from hfvg.qc.clip_constraints import get_constraint_checker
    from hfvg.qc.identity import get_identity_judge
    from hfvg.qc.still_review import get_still_reviewer

    reset_registries()
    monkeypatch.delenv("HFVG_QC_JUDGES_ENABLED", raising=False)
    assert judges.judges_enabled() is False
    for value in ("", "false", "0", "no", "off", "maybe"):
        monkeypatch.setenv("HFVG_QC_JUDGES_ENABLED", value)
        assert register_qc_judges("worker") is False and register_qc_judges("api") is False
        assert (get_still_reviewer(), get_animal_counter(), get_constraint_checker(), get_identity_judge()) == \
            (None, None, None, None)


def test_flag_on_registers_every_judge_in_the_worker_and_the_counter_in_the_api(monkeypatch):
    from hfvg.qc.animal_count import get_animal_counter
    from hfvg.qc.clip_constraints import get_constraint_checker
    from hfvg.qc.identity import VisionCharacterDetector, VisionIdentityJudge, get_identity_judge
    from hfvg.qc.still_review import VisionStillReviewer, get_still_reviewer
    from hfvg.qc.vision_constraints import VisionClipConstraintChecker
    from hfvg.qc.vision_counter import VisionAnimalCounter

    reset_registries()
    monkeypatch.setenv("HFVG_QC_JUDGES_ENABLED", "true")
    try:
        assert register_qc_judges("api") is True
        assert isinstance(get_animal_counter(), VisionAnimalCounter)
        assert (get_still_reviewer(), get_constraint_checker(), get_identity_judge()) == (None, None, None)
        reset_registries()
        assert register_qc_judges("worker") is True
        assert isinstance(get_still_reviewer(), VisionStillReviewer)
        assert isinstance(get_animal_counter(), VisionAnimalCounter)
        assert isinstance(get_constraint_checker(), VisionClipConstraintChecker)
        detector, judge = get_identity_judge()
        assert isinstance(detector, VisionCharacterDetector) and isinstance(judge, VisionIdentityJudge)
    finally:
        reset_registries()


def test_startup_log_says_whether_a_key_is_set_without_printing_it(monkeypatch, caplog):
    reset_registries()
    monkeypatch.setenv("HFVG_QC_JUDGES_ENABLED", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        with caplog.at_level(logging.WARNING, logger="hfvg.qc.judges"):
            register_qc_judges("worker")
        assert "QC judges enabled in the worker (no judge API key: every check needs review)" in caplog.text
        caplog.clear()
        monkeypatch.setenv("OPENAI_API_KEY", "placeholder-key-value-for-the-test")
        monkeypatch.setenv("HFVG_QC_JUDGE_MODEL", "placeholder-model-id")
        with caplog.at_level(logging.WARNING, logger="hfvg.qc.judges"):
            register_qc_judges("api")
        assert "QC judges enabled in the api" in caplog.text
        assert "placeholder-key-value" not in caplog.text and "placeholder-model-id" not in caplog.text
    finally:
        reset_registries()


async def test_worker_startup_registers_the_judges_only_with_the_flag(monkeypatch):
    import hfvg.worker as worker
    from hfvg.qc.still_review import VisionStillReviewer, get_still_reviewer

    class NoLedger:
        async def init_db(self):
            return None

    async def no_temporal(*args, **kwargs):
        raise RuntimeError("no Temporal server in this test")

    monkeypatch.setattr(worker, "Ledger", NoLedger)
    monkeypatch.setattr(worker.Client, "connect", no_temporal)
    reset_registries()
    try:
        monkeypatch.delenv("HFVG_QC_JUDGES_ENABLED", raising=False)
        with pytest.raises(RuntimeError, match="no Temporal server"):
            await worker.main()
        assert get_still_reviewer() is None
        monkeypatch.setenv("HFVG_QC_JUDGES_ENABLED", "true")
        with pytest.raises(RuntimeError, match="no Temporal server"):
            await worker.main()
        assert isinstance(get_still_reviewer(), VisionStillReviewer)
    finally:
        reset_registries()


async def test_api_startup_registers_the_counter_only_with_the_flag(tmp_path, monkeypatch):
    import secrets
    import sys

    from hfvg.qc.animal_count import get_animal_counter
    from hfvg.qc.vision_counter import VisionAnimalCounter

    monkeypatch.setenv("ADMIN_SECRET", secrets.token_urlsafe(32))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api.db"))
    sys.modules.pop("api.main", None)
    import api.main as api_main

    class NoTemporal:
        @staticmethod
        async def connect(*args, **kwargs):
            raise RuntimeError("no Temporal server in this test")

    monkeypatch.setattr(api_main, "TemporalClient", NoTemporal)
    reset_registries()
    try:
        monkeypatch.delenv("HFVG_QC_JUDGES_ENABLED", raising=False)
        with pytest.raises(RuntimeError, match="no Temporal server"):
            async with api_main.lifespan(api_main.app):
                pass
        assert get_animal_counter() is None
        monkeypatch.setenv("HFVG_QC_JUDGES_ENABLED", "true")
        with pytest.raises(RuntimeError, match="no Temporal server"):
            async with api_main.lifespan(api_main.app):
                pass
        assert isinstance(get_animal_counter(), VisionAnimalCounter)
    finally:
        reset_registries()


# ------------------------------------------------------------------------------------------ no call


async def test_dry_run_makes_no_call_and_holds_nothing(db, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="dry run"):
        await JudgeService(client).ask(_call())
    assert client.calls == [] and qc_line(db, EP) is None


async def test_no_api_key_refuses_before_any_hold(db):
    with pytest.raises(JudgeUnavailable, match="OPENAI_API_KEY"):
        await JudgeService().ask(_call())
    assert qc_line(db, EP) is None and verdict_rows(db) == []


async def test_a_call_without_an_episode_is_refused(db):
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="no episode id"):
        await JudgeService(client).ask(_call(episode_id=None))
    assert client.calls == []


@pytest.mark.parametrize("env, value", [
    ("HFVG_QC_JUDGE_USD_PER_MTOK_IN", "abc"), ("HFVG_QC_JUDGE_USD_PER_MTOK_OUT", "0"),
    ("HFVG_QC_JUDGE_USD_PER_MTOK_IN", "-1"), ("HFVG_QC_JUDGE_IMAGE_TOKENS", "0"),
    ("HFVG_QC_JUDGE_MAX_OUTPUT_TOKENS", "many"), ("HFVG_QC_LINE_CAP_USD", "0"), ("HFVG_QC_JUDGE_TIMEOUT_S", "-5"),
])
async def test_an_unpriced_or_unbounded_call_is_never_made(db, monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="QC judge settings invalid, no cost estimate or time limit"):
        await JudgeService(client).ask(_call())
    assert client.calls == [] and qc_line(db, EP) is None


# ------------------------------------------------------------------------------------------ paid call


async def test_an_answered_call_is_held_then_committed_at_its_token_cost(db):
    client = FakeJudgeClient({"ok": True})
    assert await JudgeService(client).ask(_call()) == {"ok": True}
    assert len(client.calls) == 1 and client.calls[0]["images"] == [b"image-1"]
    assert client.calls[0]["max_output_tokens"] == judges.DEFAULT_MAX_OUTPUT_TOKENS
    line = qc_line(db, EP)
    assert line == {"provider": "openai", "cap": 500_000, "stop": 400_000, "spent": TOKEN_COST, "reserved": 0}
    est = _estimate()
    assert [(t, a) for t, a, _r in qc_transactions(db, EP)] == \
        [("reserve", est), ("commit", TOKEN_COST), ("release", est - TOKEN_COST)]
    assert verdict_rows(db) == [("test_judge", json.dumps({"ok": True}), None, TOKEN_COST, "token_usage")]


async def test_the_line_cap_comes_from_its_setting(db, monkeypatch):
    monkeypatch.setenv("HFVG_QC_LINE_CAP_USD", "1.25")
    await JudgeService(FakeJudgeClient({"ok": True})).ask(_call())
    line = qc_line(db, EP)
    assert (line["cap"], line["stop"]) == (1_250_000, 1_000_000)


async def test_without_token_usage_the_estimate_is_committed(db):
    await JudgeService(FakeJudgeClient({"ok": True}, tokens=None)).ask(_call())
    assert qc_line(db, EP)["spent"] == _estimate()
    assert verdict_rows(db)[0][3:] == (_estimate(), "estimate")


async def test_the_model_id_comes_from_the_environment_and_stays_out_of_verdicts_and_the_ledger(db, monkeypatch):
    assert judges.judge_model() == DEFAULT_MODEL
    monkeypatch.setenv("HFVG_QC_JUDGE_MODEL", "placeholder-vision-model")
    client = FakeJudgeClient({"ok": True})
    answer = await JudgeService(client).ask(_call())
    assert client.calls[0]["model"] == "placeholder-vision-model"
    assert "placeholder-vision-model" not in json.dumps(answer)
    assert all("placeholder-vision-model" not in (r or "") for _t, _a, r in qc_transactions(db, EP))
    assert all("placeholder-vision-model" not in json.dumps(row) for row in verdict_rows(db))


# ------------------------------------------------------------------------------------------ cache


async def test_a_cached_answer_is_never_paid_again(db):
    first = FakeJudgeClient({"ok": True})
    assert await JudgeService(first).ask(_call()) == {"ok": True}
    again = FakeJudgeClient({"ok": False})  # a page reload, an approval, a retried activity: a new service
    assert await JudgeService(again).ask(_call()) == {"ok": True}
    assert again.calls == [] and qc_line(db, EP)["spent"] == TOKEN_COST
    assert [t for t, _a, _r in qc_transactions(db, EP)].count("reserve") == 1


async def test_changed_media_or_model_is_judged_again(db, monkeypatch):
    client = FakeJudgeClient({"ok": True}, {"ok": False}, {"ok": True})
    service = JudgeService(client)
    assert await service.ask(_call(media="sha-a")) == {"ok": True}
    assert await service.ask(_call(media="sha-b")) == {"ok": False}
    monkeypatch.setenv("HFVG_QC_JUDGE_MODEL", "another-model")
    assert await service.ask(_call(media="sha-a")) == {"ok": True}
    assert len(client.calls) == 3 and qc_line(db, EP)["spent"] == 3 * TOKEN_COST


async def test_concurrent_identical_calls_pay_once(db):
    client = FakeJudgeClient({"ok": True}, delay=0.2)
    service = JudgeService(client)
    answers = await asyncio.gather(*(service.ask(_call()) for _ in range(3)))
    assert answers == [{"ok": True}] * 3 and len(client.calls) == 1


async def test_a_failed_call_is_not_repaid_within_the_retry_window_then_is_asked_again(db, monkeypatch):
    client = FakeJudgeClient(RuntimeError("upstream broke"), {"ok": True})
    service = JudgeService(client)
    with pytest.raises(JudgeUnavailable, match=r"judge error \(RuntimeError\)"):
        await service.ask(_call())
    with pytest.raises(JudgeUnavailable, match="not asked again for 300 s"):
        await service.ask(_call())
    assert len(client.calls) == 1
    later = judges._now() + timedelta(seconds=301)
    monkeypatch.setattr(judges, "_now", lambda: later)
    assert await service.ask(_call()) == {"ok": True}
    assert len(client.calls) == 2


# ------------------------------------------------------------------------------------------ failures


async def test_a_timed_out_call_releases_its_hold(db, monkeypatch):
    monkeypatch.setenv("HFVG_QC_JUDGE_TIMEOUT_S", "0.05")
    with pytest.raises(JudgeUnavailable, match="the judge timed out"):
        await JudgeService(FakeJudgeClient({"ok": True}, delay=2.0)).ask(_call())
    line = qc_line(db, EP)
    assert (line["spent"], line["reserved"]) == (0, 0)
    assert [(t, a) for t, a, _r in qc_transactions(db, EP)] == [("reserve", _estimate()), ("release", _estimate())]
    assert verdict_rows(db)[0][1:3] == (None, "the judge timed out")


async def test_a_judge_error_releases_its_hold(db):
    with pytest.raises(JudgeUnavailable, match=r"judge error \(ConnectionError\)"):
        await JudgeService(FakeJudgeClient(ConnectionError("refused"))).ask(_call())
    line = qc_line(db, EP)
    assert (line["spent"], line["reserved"]) == (0, 0)


async def test_a_cancelled_call_releases_its_hold_and_stays_cancelled(db):
    service = JudgeService(FakeJudgeClient({"ok": True}, delay=5.0))
    task = asyncio.create_task(service.ask(_call()))
    for _ in range(100):
        if service.calls_made:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    line = qc_line(db, EP)
    assert (line["spent"], line["reserved"]) == (0, 0)


@pytest.mark.parametrize("answer", ["not json at all", "[1, 2]", {"verdict": "pass"}, {"ok": None, "other": 1}])
async def test_a_malformed_answer_is_paid_for_but_never_an_answer(db, answer):
    def validate(parsed):
        if not isinstance(parsed.get("ok"), bool):
            raise ValueError("ok must be true or false")
        return parsed

    with pytest.raises(JudgeUnavailable, match="malformed answer"):
        await JudgeService(FakeJudgeClient(answer)).ask(_call(validate=validate))
    assert qc_line(db, EP)["spent"] == TOKEN_COST
    kind, stored, error, usd, _source = verdict_rows(db)[0]
    assert stored is None and error.startswith("the judge gave a malformed answer") and usd == TOKEN_COST


# ------------------------------------------------------------------------------------------ budget


async def test_the_line_stop_refuses_without_a_call(db):
    from hfvg.budget import BudgetLedger

    await BudgetLedger(db).ensure_line(EP, "qc_judges", 500_000, stop_usd_micros=1, provider="openai")
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="budget refused the judge call: the qc_judges line or the episode "
                                               "is at its stop"):
        await JudgeService(client).ask(_call())
    assert client.calls == [] and qc_line(db, EP)["reserved"] == 0
    assert verdict_rows(db) == []  # a refusal is not remembered: the next call asks the budget again


async def test_the_line_cap_refuses_without_a_call(db, monkeypatch):
    monkeypatch.setenv("HFVG_QC_LINE_CAP_USD", "0.000001")
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="budget refused the judge call: Budget cap exceeded"):
        await JudgeService(client).ask(_call())
    assert client.calls == []


async def test_the_episode_stop_refuses_without_a_call(db):
    from hfvg.budget import BudgetLedger

    ledger = BudgetLedger(db)
    await ledger.ensure_line(EP, "L3_final_stills", 48_000_000, stop_usd_micros=48_000_000)
    assert await ledger.reserve(EP, "L3_final_stills", 47_999_900, "stills", job_id="stills-1")
    await ledger.commit(EP, "L3_final_stills", 47_999_900, 47_999_900, job_id="stills-1")
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="at its stop"):
        await JudgeService(client).ask(_call())
    assert client.calls == [] and qc_line(db, EP)["reserved"] == 0


async def test_the_gc01_headroom_refuses_without_a_call(db):
    from hfvg.budget import BudgetLedger

    await BudgetLedger(db).set_manual_balance(5_000_100, set_by="tester")
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="budget refused the judge call: GC.01 headroom"):
        await JudgeService(client).ask(_call())
    assert client.calls == []


async def test_the_balance_gate_is_asked_for_openai_and_its_refusal_holds_nothing(db, monkeypatch):
    from hfvg.balance_monitor import BalanceMonitor

    asked = []

    async def refuse(self, provider, estimate_usd_micros, auto_topup_enabled=False):
        asked.append((provider, estimate_usd_micros, auto_topup_enabled))
        return False, "openai balance insufficient: $0.00 < required $0.01"

    monkeypatch.setattr(BalanceMonitor, "check_preflight_balance", refuse)
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match="balance check refused the judge call: openai balance insufficient"):
        await JudgeService(client).ask(_call())
    assert asked == [("openai", _estimate(), False)]
    assert client.calls == [] and qc_transactions(db, EP) == []


async def test_a_broken_balance_gate_refuses(db, monkeypatch):
    from hfvg.balance_monitor import BalanceMonitor

    async def broken(self, *args, **kwargs):
        raise RuntimeError("balance table locked")

    monkeypatch.setattr(BalanceMonitor, "check_preflight_balance", broken)
    client = FakeJudgeClient({"ok": True})
    with pytest.raises(JudgeUnavailable, match=r"balance check unavailable \(RuntimeError\)"):
        await JudgeService(client).ask(_call())
    assert client.calls == []


async def test_an_openai_balance_is_unknown_so_the_gate_lets_the_line_bound_the_spend(db):
    from hfvg.balance_monitor import BalanceMonitor

    passed, reason = await BalanceMonitor(db).check_preflight_balance("openai", 2_000)
    assert passed and "openai balance unknown" in reason and "qc_judges budget line" in reason


async def test_qc_spend_is_not_counted_as_higgsfield_spend_against_the_manual_balance(db):
    from hfvg.budget import BudgetLedger

    ledger = BudgetLedger(db)
    await ledger.set_manual_balance(20_000_000, set_by="tester")
    await JudgeService(FakeJudgeClient({"ok": True})).ask(_call())
    status = await ledger.get_balance_status()
    assert status["spent_since_entry_usd_micros"] == 0 and status["reserved_usd_micros"] == 0


# ------------------------------------------------------------------------------------------ OpenAI client


class FakeOpenAIEndpoint:
    """The chat completions endpoint inside the test process: an HTTP client of the type the installed
    OpenAI SDK takes, with a mock transport. No request leaves the process (no DNS lookup either)."""

    def __init__(self, status: int = 200, body: dict | None = None):
        import openai._base_client as sdk_base

        self.http = getattr(sdk_base, "httpx2", None) or sdk_base.httpx
        self.status, self.body = status, body
        self.requests: list = []

    def client(self):
        def handler(request):
            self.requests.append(request)
            return self.http.Response(self.status, json=self.body)
        return self.http.AsyncClient(transport=self.http.MockTransport(handler))

    def sent(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].content)


def _completion(content: str, usage: dict | None) -> dict:
    body = {"id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "placeholder-model",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


async def test_openai_client_sends_one_json_request_with_the_images(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("HFVG_QC_JUDGE_TEMPERATURE", raising=False)
    api = FakeOpenAIEndpoint(body=_completion('{"verdict": "pass"}', {
        "prompt_tokens": 1234, "completion_tokens": 56, "total_tokens": 1290}))
    reply = await OpenAIJudgeClient("placeholder-key", http_client=api.client()).complete(
        model="placeholder-model", system="judge it", text="the prompt", images=[b"\xff\xd8one", b"\xff\xd8two"],
        max_output_tokens=321)
    assert (reply.text, reply.tokens_in, reply.tokens_out) == ('{"verdict": "pass"}', 1234, 56)
    assert len(api.requests) == 1
    request = api.requests[0]
    assert request.method == "POST" and str(request.url).endswith("/chat/completions")
    assert request.headers["authorization"] == "Bearer placeholder-key"
    body = api.sent()
    assert body["model"] == "placeholder-model" and body["response_format"] == {"type": "json_object"}
    assert body["max_completion_tokens"] == 321 and body["temperature"] == 0
    system, user = body["messages"]
    assert system == {"role": "system", "content": "judge it"}
    assert user["content"][0] == {"type": "text", "text": "the prompt"}
    urls = [part["image_url"]["url"] for part in user["content"][1:]]
    assert urls == ["data:image/jpeg;base64," + base64.b64encode(b).decode() for b in (b"\xff\xd8one", b"\xff\xd8two")]


async def test_openai_client_without_temperature_or_usage(monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("HFVG_QC_JUDGE_TEMPERATURE", "none")
    api = FakeOpenAIEndpoint(body=_completion("{}", None))
    reply = await OpenAIJudgeClient("placeholder-key", http_client=api.client()).complete(
        model="m", system="s", text="t", images=[], max_output_tokens=10)
    assert "temperature" not in api.sent()
    assert (reply.tokens_in, reply.tokens_out) == (None, None)


async def test_an_openai_error_is_one_attempt_and_releases_the_hold(db, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    api = FakeOpenAIEndpoint(status=500, body={"error": {"message": "server error"}})
    service = JudgeService(OpenAIJudgeClient("placeholder-key", http_client=api.client()))
    with pytest.raises(JudgeUnavailable, match="judge error"):
        await service.ask(_call())
    assert len(api.requests) == 1  # max_retries=0: one paid attempt per call, the cache decides the rest
    line = qc_line(db, EP)
    assert (line["spent"], line["reserved"]) == (0, 0)


async def test_an_openai_answer_through_the_service_is_committed_at_its_token_cost(db, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    api = FakeOpenAIEndpoint(body=_completion('{"ok": true}', {
        "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100}))
    service = JudgeService(OpenAIJudgeClient("placeholder-key", http_client=api.client()))
    assert await service.ask(_call()) == {"ok": True}
    assert qc_line(db, EP)["spent"] == TOKEN_COST and api.sent()["model"] == DEFAULT_MODEL


async def test_the_service_builds_its_client_from_the_configured_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "placeholder-key")
    client = JudgeService().client()
    assert isinstance(client, OpenAIJudgeClient) and client._api_key == "placeholder-key"


async def test_the_verdict_table_survives_a_reload(db):
    await JudgeService(FakeJudgeClient({"ok": True})).ask(_call())
    with sqlite3.connect(db) as c:
        (count,) = c.execute("SELECT COUNT(*) FROM qc_judge_verdicts").fetchone()
    assert count == 1
