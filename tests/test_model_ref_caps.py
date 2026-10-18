"""Round 5: per-model reference caps (documented limits, env-overridable, refuse not truncate)."""
import json

import httpx
import pytest
import respx

from hfvg.config import DEFAULT_MODEL_MAX_REFS, max_refs_for, model_max_refs_table
from tests.lint_helpers import acapture, capture, lint_meta, lint_ok
from tests.test_round4_shot_data import _live_env


def test_default_caps_match_documented_limits():
    # docs say 10, but live ep05 runs failed instantly ("Generation failed") with 6-7 refs and
    # succeeded with <=5, so the effective default is 5 (see hfvg/config.py).
    assert max_refs_for("xai/grok-imagine-image-2.0") == 5
    assert max_refs_for("alibaba/qwen-image-3/edit") == 3  # docs max 3
    assert max_refs_for("bytedance/seedance-2.5/reference-to-video") == 8  # docs max 30
    assert max_refs_for("kling-video/v3.0/pro/image-to-video") == 3  # unconfirmed


def test_unknown_model_falls_back_to_still_max_refs(monkeypatch):
    monkeypatch.delenv("STILL_MAX_REFS", raising=False)
    assert max_refs_for("vendor/unknown-model") == 3
    monkeypatch.setenv("STILL_MAX_REFS", "2")
    assert max_refs_for("vendor/unknown-model") == 2
    assert max_refs_for("xai/grok-imagine-image-2.0") == 5  # table wins over the fallback


def test_env_json_overrides_one_model_only(monkeypatch):
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({"xai/grok-imagine-image-2.0": 6}))
    assert max_refs_for("xai/grok-imagine-image-2.0") == 6
    assert max_refs_for("alibaba/qwen-image-3/edit") == 3
    assert DEFAULT_MODEL_MAX_REFS["xai/grok-imagine-image-2.0"] == 5  # defaults not mutated


def test_env_json_negative_cap_is_rejected(monkeypatch):
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({"x/y": -1}))
    err = capture(ValueError, model_max_refs_table)
    assert err is not None, "a negative cap must be rejected, not accepted"


@pytest.mark.asyncio
@respx.mock
async def test_qwen_still_provider_refuses_four_refs_but_grok_accepts_them():
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    qwen = respx.post("http://mock.api/alibaba/qwen-image-3/edit").mock(
        return_value=httpx.Response(200, json={"request_id": "q"}))
    grok = respx.post("http://mock.api/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "g"}))
    urls = [f"https://example.com/{i}.png" for i in range(4)]
    qp = HiggsfieldStillProvider(api_key="a:b", base_url="http://mock.api",
                                 model_path="alibaba/qwen-image-3/edit")
    gp = HiggsfieldStillProvider(api_key="a:b", base_url="http://mock.api")
    try:
        refused = await acapture(ValueError, qp.submit_image(prompt="p", image_urls=urls))
        assert refused is not None and "alibaba/qwen-image-3/edit" in str(refused)
        assert qwen.call_count == 0
        err = await acapture(ValueError, gp.submit_image(prompt="p", image_urls=urls))
        assert err is None, f"Grok (cap 5) must accept 4 refs: {err}"
        assert len(json.loads(grok.calls[0].request.content)["image_urls"]) == 4
    finally:
        await qp.close()
        await gp.close()


@pytest.mark.asyncio
@respx.mock
async def test_live_still_activity_cap_follows_the_still_model(tmp_path, monkeypatch):
    from hfvg.activities.studio_generation import submit_still_job_enforced

    await _live_env(tmp_path, monkeypatch)
    for path in ("xai/grok-imagine-image-2.0", "alibaba/qwen-image-3/edit"):
        respx.post(f"https://api.higgsfield.ai/estimate/{path}").mock(
            return_value=httpx.Response(200, json={"credits": "1", "usd": "0.06"}))
    grok = respx.post("https://api.higgsfield.ai/xai/grok-imagine-image-2.0").mock(
        return_value=httpx.Response(200, json={"request_id": "g1"}))
    qwen = respx.post("https://api.higgsfield.ai/alibaba/qwen-image-3/edit").mock(
        return_value=httpx.Response(200, json={"request_id": "q1"}))
    refs = [f"https://example.com/r{i}.png" for i in range(5)]

    monkeypatch.setenv("MODEL_PATH_STILL", "alibaba/qwen-image-3/edit")
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, refs, "1k", "medium", "9:16", None, lint_meta(), []))
    assert err is not None and "alibaba/qwen-image-3/edit" in str(err), err
    assert qwen.call_count == 0

    monkeypatch.setenv("MODEL_PATH_STILL", "xai/grok-imagine-image-2.0")
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A01", lint_ok("S"), 1, refs, "1k", "medium", "9:16", None, lint_meta(), []))
    assert err is None or "limit" not in str(err), f"5 refs are within Grok's cap of 5: {err}"
    assert grok.call_count == 1
    assert len(json.loads(grok.calls[0].request.content)["image_urls"]) == 5

    # 6 refs (the count behind every live instant "Generation failed") must be refused before submit
    six = refs + ["https://example.com/r5.png"]
    err = await acapture(ValueError, submit_still_job_enforced(
        "ep99", "A02", lint_ok("S"), 1, six, "1k", "medium", "9:16", None, lint_meta(), []))
    assert err is not None and "xai/grok-imagine-image-2.0" in str(err), err
    assert grok.call_count == 1, "an over-cap Grok still must never reach the provider"
