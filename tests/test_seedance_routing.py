"""P2: Seedance 2.5 provider, explicit routing, declined_preset_id plumbing (strict mocks only).

All fixtures are synthetic. No live provider calls (respx intercepts every request).
"""

import hashlib
import json

import aiosqlite
import httpx
import pytest
import respx

from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
from hfvg.providers.kling_video import KlingVideoProvider
from hfvg.providers.seedance_video import SeedanceVideoProvider
from hfvg.routing import RoutingError, plan_route
from tests.ledger_helpers import insert_line, set_balance
from tests.lint_helpers import acapture, capture, lint_meta, lint_ok

BASE = "http://mock.api"
SEEDANCE_URL = f"{BASE}/bytedance/seedance-2.5/image-to-video"
KLING_URL = f"{BASE}/kling-video/v3.0/pro/image-to-video"
STILL_URL = f"{BASE}/xai/grok-imagine-image-2.0"
PRESET = "00000000-0000-4000-8000-000000000001"  # synthetic UUID


def _seedance() -> SeedanceVideoProvider:
    return SeedanceVideoProvider(api_key="test_id:test_secret", base_url=BASE)


def _body(route) -> dict:
    return json.loads(route.calls[0].request.content.decode())


# ---------------------------------------------------------------- Seedance request contract

def test_seedance_model_path_is_documented_literal():
    provider = _seedance()
    assert provider.model_path == "bytedance/seedance-2.5/image-to-video"


@pytest.mark.asyncio
@respx.mock
async def test_seedance_request_body_exact_and_audio_false():
    """Exact documented body: audio always false, no aspect param, end image forwarded."""
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-1"}))
    provider = _seedance()
    try:
        request_id = await provider.submit_video(
            image_url="https://example.com/start.jpg",
            prompt="Synthetic prompt",
            duration=6,
            resolution="720p",
            bitrate_mode="standard",
            end_image_url="https://example.com/end.jpg",
        )
    finally:
        await provider.close()
    assert request_id == "sd-1"
    assert _body(route) == {
        "image_url": "https://example.com/start.jpg",
        "prompt": "Synthetic prompt",
        "duration": 6,
        "resolution": "720p",
        "bitrate_mode": "standard",
        "end_image_url": "https://example.com/end.jpg",
        "generate_audio": False,
    }


@pytest.mark.asyncio
@respx.mock
async def test_seedance_generate_audio_always_false():
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-2"}))
    provider = _seedance()
    try:
        await provider.submit_video("https://example.com/a.jpg", "p", 5, "480p")
    finally:
        await provider.close()
    assert _body(route)["generate_audio"] is False


@pytest.mark.asyncio
@respx.mock
async def test_seedance_end_image_url_is_sent():
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-3"}))
    provider = _seedance()
    try:
        await provider.submit_video(
            "https://example.com/a.jpg", "p", 5, "480p", end_image_url="https://example.com/z.jpg"
        )
    finally:
        await provider.close()
    assert _body(route).get("end_image_url") == "https://example.com/z.jpg"


@pytest.mark.asyncio
@respx.mock
async def test_seedance_resolution_is_forwarded():
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-4"}))
    provider = _seedance()
    seen = []
    try:
        for resolution in ("480p", "720p", "1080p"):
            await provider.submit_video("https://example.com/a.jpg", "p", 5, resolution)
            seen.append(json.loads(route.calls[-1].request.content.decode())["resolution"])
    finally:
        await provider.close()
    assert seen == ["480p", "720p", "1080p"]


@pytest.mark.asyncio
@respx.mock
async def test_seedance_refuses_short_and_long_durations_before_any_request():
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-5"}))
    provider = _seedance()
    try:
        short = await acapture(
            ValueError, provider.submit_video("https://example.com/a.jpg", "p", 3, "480p")
        )
        long_ = await acapture(
            ValueError, provider.submit_video("https://example.com/a.jpg", "p", 31, "480p")
        )
        bad_res = await acapture(
            ValueError, provider.submit_video("https://example.com/a.jpg", "p", 5, "4k")
        )
    finally:
        await provider.close()
    assert short is not None, "duration 3 must be refused on Seedance"
    assert "requires duration >= 4" in str(short) and "Kling" in str(short), str(short)
    assert long_ is not None and "at most 30" in str(long_), long_
    assert bad_res is not None and "resolution" in str(bad_res), bad_res
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_seedance_duration_4_is_accepted_boundary():
    provider = _seedance()
    try:
        micros = await provider.estimate_usd_micros(4, "480p")
    finally:
        await provider.close()
    assert micros == 822_400  # 4 s x 0.2056 USD/s


# ---------------------------------------------------------------- Seedance estimate (rate table)

@pytest.mark.asyncio
async def test_seedance_estimates_use_per_resolution_rates():
    provider = _seedance()
    try:
        r480 = await provider.estimate_usd_micros(10, "480p")
        r720 = await provider.estimate_usd_micros(10, "720p")
        r1080 = await provider.estimate_usd_micros(10, "1080p")
    finally:
        await provider.close()
    assert r480 == 2_056_000  # 10 s x 0.2056
    assert r720 == 4_622_000  # 10 s x 0.4622
    assert r1080 == 11_372_000  # 10 s x 1.1372


# ---------------------------------------------------------------- Idempotency key

@pytest.mark.asyncio
@respx.mock
async def test_seedance_idempotency_key_covers_model_resolution_duration_end_image_prompt():
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "sd-6"}))
    provider = _seedance()
    try:
        base = dict(image_url="https://example.com/a.jpg", prompt="p", duration=5, resolution="480p")
        keys = {
            "base": provider._generate_idempotency_key(**base),
            "same": provider._generate_idempotency_key(**base),
            "res": provider._generate_idempotency_key(**{**base, "resolution": "1080p"}),
            "dur": provider._generate_idempotency_key(**{**base, "duration": 6}),
            "end": provider._generate_idempotency_key(**base, end_image_url="https://example.com/e.jpg"),
            "prompt": provider._generate_idempotency_key(**{**base, "prompt": "q"}),
            "image": provider._generate_idempotency_key(**{**base, "image_url": "https://example.com/b.jpg"}),
        }
        provider.model_path = "bytedance/seedance-9.9/image-to-video"
        keys["model"] = provider._generate_idempotency_key(**base)
        provider.model_path = "bytedance/seedance-2.5/image-to-video"
        await provider.submit_video("https://example.com/a.jpg", "p", 5, "480p")
    finally:
        await provider.close()
    assert keys["base"] == keys["same"]
    others = [v for k, v in keys.items() if k not in ("base", "same")]
    assert all(v != keys["base"] for v in others)
    assert len(set(others)) == len(others)
    sent = route.calls[0].request.headers["Idempotency-Key"]
    assert sent == keys["base"] and sent.startswith("hf-sd-")


@pytest.mark.asyncio
async def test_kling_idempotency_key_covers_model_end_image_and_prompt():
    provider = KlingVideoProvider(api_key="test_id:test_secret", base_url=BASE)
    try:
        base = provider._generate_idempotency_key("https://e.com/a.jpg", "p", 5)
        with_end = provider._generate_idempotency_key("https://e.com/a.jpg", "p", 5, "https://e.com/z.jpg")
        other_prompt = provider._generate_idempotency_key("https://e.com/a.jpg", "q", 5)
        provider.model_path = "kling-video/v3.0/std/image-to-video"
        other_model = provider._generate_idempotency_key("https://e.com/a.jpg", "p", 5)
    finally:
        await provider.close()
    assert len({base, with_end, other_prompt, other_model}) == 4


# ---------------------------------------------------------------- declined_preset_id on every model

@pytest.mark.asyncio
@respx.mock
async def test_declined_preset_sent_on_still_request(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_DECLINED_PRESET_ID", PRESET)
    route = respx.post(STILL_URL).mock(return_value=httpx.Response(200, json={"request_id": "r1"}))
    provider = HiggsfieldStillProvider(api_key="test_id:test_secret", base_url=BASE)
    try:
        await provider.submit_image(prompt="p", resolution="1k", quality="medium")
    finally:
        await provider.close()
    assert _body(route).get("declined_preset_id") == PRESET


@pytest.mark.asyncio
@respx.mock
async def test_declined_preset_sent_on_kling_request(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_DECLINED_PRESET_ID", PRESET)
    route = respx.post(KLING_URL).mock(return_value=httpx.Response(200, json={"request_id": "r2"}))
    provider = KlingVideoProvider(api_key="test_id:test_secret", base_url=BASE)
    try:
        await provider.submit_video("https://example.com/a.jpg", "p", 5)
    finally:
        await provider.close()
    assert _body(route).get("declined_preset_id") == PRESET


@pytest.mark.asyncio
@respx.mock
async def test_declined_preset_sent_on_seedance_request(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_DECLINED_PRESET_ID", PRESET)
    route = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "r3"}))
    provider = _seedance()
    try:
        await provider.submit_video("https://example.com/a.jpg", "p", 5, "480p")
    finally:
        await provider.close()
    assert _body(route).get("declined_preset_id") == PRESET


@pytest.mark.asyncio
@respx.mock
async def test_declined_preset_unset_by_default_on_all_models(monkeypatch):
    monkeypatch.delenv("HIGGSFIELD_DECLINED_PRESET_ID", raising=False)
    still = respx.post(STILL_URL).mock(return_value=httpx.Response(200, json={"request_id": "a"}))
    kling = respx.post(KLING_URL).mock(return_value=httpx.Response(200, json={"request_id": "b"}))
    seed = respx.post(SEEDANCE_URL).mock(return_value=httpx.Response(200, json={"request_id": "c"}))
    sp = HiggsfieldStillProvider(api_key="test_id:test_secret", base_url=BASE)
    kp = KlingVideoProvider(api_key="test_id:test_secret", base_url=BASE)
    dp = _seedance()
    try:
        await sp.submit_image(prompt="p")
        await kp.submit_video("https://example.com/a.jpg", "p", 5)
        await dp.submit_video("https://example.com/a.jpg", "p", 5, "480p")
    finally:
        await sp.close()
        await kp.close()
        await dp.close()
    for route in (still, kling, seed):
        assert "declined_preset_id" not in _body(route)


# ---------------------------------------------------------------- Kling end frame (documented as last_image_url)

@pytest.mark.asyncio
@respx.mock
async def test_kling_end_frame_uses_documented_last_image_url():
    route = respx.post(KLING_URL).mock(return_value=httpx.Response(200, json={"request_id": "k1"}))
    provider = KlingVideoProvider(api_key="test_id:test_secret", base_url=BASE)
    try:
        await provider.submit_video(
            "https://example.com/a.jpg", "p", 5, end_image_url="https://example.com/z.jpg"
        )
    finally:
        await provider.close()
    body = _body(route)
    assert body["last_image_url"] == "https://example.com/z.jpg"
    assert "end_image_url" not in body


# ---------------------------------------------------------------- Planner (fail closed)

def _shot(**kw):
    shot = {"model": "seedance", "tier": "draft", "duration": 5, "tags": []}
    shot.update(kw)
    return shot


def test_planner_never_routes_wet_shot_to_seedance():
    err = capture(RoutingError, plan_route, _shot(tags=["wet"]))
    assert err is not None, "a wet shot must never be planned onto Seedance"
    assert "wet" in str(err)
    # Case-insensitive tag handling
    assert capture(RoutingError, plan_route, _shot(tags=["WET"])) is not None
    # Kling accepts wet shots
    assert plan_route(_shot(model="kling", tags=["wet"])).model == "kling"


def test_planner_never_routes_short_shot_to_seedance():
    err = capture(RoutingError, plan_route, _shot(duration=3))
    assert err is not None and "under 4s" in str(err), err
    assert capture(RoutingError, plan_route, _shot(duration=3.9)) is not None
    assert plan_route(_shot(duration=4)).resolution == "480p"
    assert plan_route(_shot(model="kling", duration=3)).model == "kling"


def test_planner_tier_resolution_and_explicit_fields_required():
    assert plan_route(_shot(tier="draft")).resolution == "480p"
    assert plan_route(_shot(tier="final")).resolution == "1080p"
    assert plan_route(_shot(model="kling", tier="final")).resolution is None
    for bad in (
        {"tier": "draft", "duration": 5},  # no model
        {"model": "seedance", "duration": 5},  # no tier
        {"model": "veo", "tier": "draft", "duration": 5},  # unknown model
        {"model": "kling", "tier": "retry", "duration": 5},  # not a routing tier
        {"model": "kling", "tier": "final"},  # no duration
        {"model": "kling", "tier": "final", "duration": 0},
    ):
        assert capture(RoutingError, plan_route, bad) is not None, bad


# ---------------------------------------------------------------- Clip activity (live) wiring

async def _live_env(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import approve_g108, create_episode, init_studio_db, set_live_mode

    db_path = str(tmp_path / "sd.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    await BudgetLedger(db_path).init_db()
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L4_video", 20_000_000, 16_000_000)
        await db.commit()
    await set_live_mode(db_path, "ep99", True)
    await approve_g108(db_path, "ep99")
    await set_balance(db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai")
    monkeypatch.delenv("MODEL_PATH_SEEDANCE", raising=False)
    return db_path


@pytest.mark.asyncio
@respx.mock
async def test_clip_activity_seedance_final_is_fresh_1080p_with_rate_table_estimate(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger

    db_path = await _live_env(tmp_path, monkeypatch)
    sub = respx.post("https://api.higgsfield.ai/bytedance/seedance-2.5/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "sd-live-1"})
    )
    result = await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/still.jpg", lint_ok("Synthetic clip"), 5.0, 1,
        "final", lint_meta(), "seedance", [], "https://example.com/end.jpg",
    )
    body = _body(sub)
    assert body["resolution"] == "1080p"
    assert body["generate_audio"] is False
    assert body["end_image_url"] == "https://example.com/end.jpg"
    assert "draft_job_id" not in body
    assert result["reserved_amount"] == 5_686_000  # 5 s x 1.1372 USD/s, no network estimate
    jobs = await BudgetLedger(db_path).get_jobs("ep99")
    assert jobs[0]["model"] == "bytedance/seedance-2.5/image-to-video"
    assert jobs[0]["tier"] == "final" and jobs[0]["usd_micros"] == 5_686_000


@pytest.mark.asyncio
@respx.mock
async def test_clip_activity_seedance_draft_is_480p(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced

    await _live_env(tmp_path, monkeypatch)
    sub = respx.post("https://api.higgsfield.ai/bytedance/seedance-2.5/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "sd-live-2"})
    )
    result = await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/still.jpg", lint_ok("Synthetic clip"), 5.0, 1,
        "draft", lint_meta(), "seedance",
    )
    assert _body(sub)["resolution"] == "480p"
    assert result["reserved_amount"] == 1_028_000  # 5 s x 0.2056


@pytest.mark.asyncio
@respx.mock
async def test_clip_activity_refuses_wet_or_short_seedance_before_any_spend(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced
    from hfvg.budget import BudgetLedger
from hfvg.credit_ledger import CreditLedger

    db_path = await _live_env(tmp_path, monkeypatch)
    sub = respx.post("https://api.higgsfield.ai/bytedance/seedance-2.5/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "never"})
    )
    wet = await acapture(RoutingError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("Synthetic clip"), 5.0, 1,
        "final", lint_meta(), "seedance", ["wet"],
    ))
    short = await acapture(RoutingError, submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/s.jpg", lint_ok("Synthetic clip"), 3.0, 1,
        "final", lint_meta(), "seedance", [],
    ))
    assert wet is not None and short is not None
    assert sub.call_count == 0
    status = await BudgetLedger(db_path).get_line_status("ep99", "L4_video")
    assert status["reserved"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_clip_activity_kling_forwards_end_frame_as_last_image_url(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_clip_job_enforced

    await _live_env(tmp_path, monkeypatch)
    respx.post("https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"})
    )
    sub = respx.post("https://api.higgsfield.ai/kling-video/v3.0/pro/image-to-video").mock(
        return_value=httpx.Response(200, json={"request_id": "k-live-1"})
    )
    await submit_clip_job_enforced(
        "ep99", "A01", "https://example.com/still.jpg", lint_ok("Synthetic clip"), 5.0, 1,
        "final", lint_meta(), "kling", [], "https://example.com/end.jpg",
    )
    assert _body(sub)["last_image_url"] == "https://example.com/end.jpg"
