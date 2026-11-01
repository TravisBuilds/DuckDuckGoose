"""GAP-36: a Kling clip never reserves below the list rate, even when the live estimate drops.

ep05v2 G3 round 2 (2026-10-27 15:09 UTC): POST /estimate/kling-video/v3.0/pro/image-to-video
returned 0.28 USD / 4.48 credits for 5 s, half the 0.112 USD/s list rate it returned at 13:28 UTC.
The budget guard reserves from this estimate, so a half-price answer under-reserves the run cap.
"""
import httpx
import pytest
import respx

from hfvg.providers.kling_video import KlingVideoProvider, kling_reserve_usd_micros

EST = "https://api.higgsfield.ai/estimate/kling-video/v3.0/pro/image-to-video"


@pytest.fixture(autouse=True)
def _floor_on(monkeypatch, request):
    if "floor_off" not in request.node.name:
        monkeypatch.setenv("KLING_RESERVE_LIST_FLOOR", "1")


def test_floor_helper():
    assert kling_reserve_usd_micros(280_000, 5) == 560_000      # live below list: list
    assert kling_reserve_usd_micros(None, 4) == 448_000         # no live usd: list
    assert kling_reserve_usd_micros(700_000, 5) == 700_000      # live above list: live


@pytest.mark.asyncio
@respx.mock
async def test_estimate_usd_and_credits_floor(monkeypatch):
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test-key")
    monkeypatch.setenv("HIGGSFIELD_API_SECRET", "test-secret")
    respx.post(EST).mock(return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    p = KlingVideoProvider(api_key="k:s", base_url="https://api.higgsfield.ai")
    usd, credits = await p.estimate_usd_and_credits("https://example.com/s.jpg", "x", 5)
    assert usd == 560_000 and credits == pytest.approx(8.96)
    assert await p.estimate_usd_micros("https://example.com/s.jpg", "x", 5) == 560_000


@pytest.mark.asyncio
@respx.mock
async def test_live_above_list_is_kept(monkeypatch):
    respx.post(EST).mock(return_value=httpx.Response(200, json={"credits": "10.0", "usd": "0.62"}))
    p = KlingVideoProvider(api_key="k:s", base_url="https://api.higgsfield.ai")
    usd, credits = await p.estimate_usd_and_credits("https://example.com/s.jpg", "x", 5)
    assert usd == 620_000 and credits == pytest.approx(10.0)


@pytest.mark.asyncio
@respx.mock
async def test_floor_off_by_default_keeps_live_estimate(monkeypatch):
    monkeypatch.delenv("KLING_RESERVE_LIST_FLOOR", raising=False)
    respx.post(EST).mock(return_value=httpx.Response(200, json={"credits": "4.48", "usd": "0.28"}))
    p = KlingVideoProvider(api_key="k:s", base_url="https://api.higgsfield.ai")
    usd, credits = await p.estimate_usd_and_credits("https://example.com/s.jpg", "x", 5)
    assert usd == 280_000 and credits == pytest.approx(4.48)
    assert kling_reserve_usd_micros(None, 4) == 448_000         # no live usd: list, either way
