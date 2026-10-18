"""Provider failures must keep the FULL raw body and headers (X-Correlation-ID) for support."""

import json
import logging

import httpx
import pytest
import respx

from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

BASE = "http://mock.hf"
RID = "8b427df8-8f15-4481-ae35-8d3271731297"
CID = "c2f6a06bc0ab429fb3bad419a3150b68"
FAILED_BODY = {
    "status": "failed",
    "request_id": RID,
    "error": "Generation failed",
    "status_url": f"{BASE}/requests/{RID}/status",
    "cancel_url": f"{BASE}/requests/{RID}/cancel",
}
HDRS = {"x-correlation-id": CID, "cf-ray": "9a1b2c3d4e5f-CDG", "set-cookie": "s=secret"}


def _provider():
    return HiggsfieldStillProvider(
        api_key="kid:ksecret", base_url=BASE, model_path="xai/grok-imagine-image-2.0"
    )


@pytest.mark.asyncio
async def test_failed_status_keeps_raw_body_and_headers(caplog):
    caplog.set_level(logging.INFO)
    p = _provider()
    with respx.mock:
        respx.get(f"{BASE}/requests/{RID}/status").mock(
            return_value=httpx.Response(200, json=FAILED_BODY, headers=HDRS)
        )
        job = await p.get_job_status(RID)
    await p.close()
    assert job.status.value == "failed"
    assert job.raw == FAILED_BODY
    assert job.response_headers["x-correlation-id"] == CID
    assert "set-cookie" not in job.response_headers
    log = caplog.text
    assert json.dumps(FAILED_BODY) in log
    assert CID in log and "9a1b2c3d4e5f-CDG" in log
    assert "s=secret" not in log


@pytest.mark.asyncio
async def test_completed_status_is_not_logged_as_error(caplog):
    caplog.set_level(logging.INFO)
    p = _provider()
    body = {"status": "completed", "request_id": RID, "images": [{"url": "https://cdn/x.png"}]}
    with respx.mock:
        respx.get(f"{BASE}/requests/{RID}/status").mock(
            return_value=httpx.Response(200, json=body, headers=HDRS)
        )
        job = await p.get_job_status(RID)
    await p.close()
    assert job.output_url == "https://cdn/x.png"
    assert job.raw == body
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


@pytest.mark.asyncio
async def test_status_http_error_carries_body_and_correlation_id(caplog):
    caplog.set_level(logging.INFO)
    p = _provider()
    with respx.mock:
        respx.get(f"{BASE}/requests/{RID}/status").mock(
            return_value=httpx.Response(404, json={"detail": "Request not found"}, headers=HDRS)
        )
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await p.get_job_status(RID)
    await p.close()
    assert ei.value.response.status_code == 404
    assert "Request not found" in str(ei.value) and CID in str(ei.value)
    assert "Request not found" in caplog.text and CID in caplog.text


@pytest.mark.asyncio
async def test_submit_rejection_carries_body_headers_and_request_summary(caplog):
    caplog.set_level(logging.INFO)
    p = _provider()
    urls = [f"https://cdn.example/ref{i}.jpg" for i in range(3)]
    with respx.mock:
        respx.post(f"{BASE}/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(
                422, json={"detail": [{"loc": ["body", "image_urls"], "msg": "too many"}]}, headers=HDRS
            )
        )
        with pytest.raises(httpx.HTTPStatusError) as ei:
            await p.submit_image("a duck" * 10, quality="medium", image_urls=urls, aspect_ratio="9:16")
    await p.close()
    msg = str(ei.value)
    assert "422" in msg and "too many" in msg and CID in msg
    log = caplog.text
    assert "too many" in log and CID in log
    assert '"n_image_urls": 3' in log and '"prompt_chars": 60' in log
    assert "ksecret" not in log


@pytest.mark.asyncio
async def test_submit_success_logs_correlation_id_and_summary(caplog):
    caplog.set_level(logging.INFO)
    p = _provider()
    urls = [f"https://cdn.example/ref{i}.jpg" for i in range(2)]
    with respx.mock:
        respx.post(f"{BASE}/xai/grok-imagine-image-2.0").mock(
            return_value=httpx.Response(200, json={"request_id": RID}, headers=HDRS)
        )
        rid = await p.submit_image("p" * 2470, quality="medium", image_urls=urls, aspect_ratio="9:16")
    await p.close()
    assert rid == RID
    log = caplog.text
    assert RID in log and CID in log
    assert '"n_image_urls": 2' in log and '"prompt_chars": 2470' in log
    assert "ksecret" not in log


@pytest.mark.asyncio
async def test_poll_activity_failure_returns_and_logs_raw_evidence(tmp_path, monkeypatch, caplog):
    from tests.fake_image_passthrough import passthrough_normalize
    from hfvg.studio_db import init_studio_db, create_episode, set_live_mode, approve_g108
    from hfvg.credit_ledger import CreditLedger
    from hfvg.activities.studio_generation import poll_job_status

    caplog.set_level(logging.INFO)
    db = str(tmp_path / "test.db")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "kid:ksecret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", BASE)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.setenv("DATABASE_PATH", db)
    passthrough_normalize()
    await init_studio_db(db)
    await create_episode(db, "ep05", None)
    await set_live_mode(db, "ep05", True, "test")
    await approve_g108(db, "ep05", "test")
    ledger = CreditLedger(db)
    await ledger.init_db()
    await ledger.create_episode_line("ep05", cap_credits=10000)
    await ledger.reserve_credits("ep05", "still-ep05-A02_end-v1", 4.0, "test")
    await ledger.persist_provider_job("still-ep05-A02_end-v1", RID, "higgsfield")

    with respx.mock:
        respx.get(f"{BASE}/requests/{RID}/status").mock(
            return_value=httpx.Response(200, json=FAILED_BODY, headers=HDRS)
        )
        res = await poll_job_status(job_id="still-ep05-A02_end-v1", job_type="still", episode_id="ep05")

    assert res["status"] == "failed"
    assert res["error"] == f"Generation failed (x-correlation-id {CID})"
    assert res["provider_raw"] == FAILED_BODY
    assert res["provider_headers"]["x-correlation-id"] == CID
    assert "set-cookie" not in res["provider_headers"]
    assert f"raw status body: {json.dumps(FAILED_BODY)}" in caplog.text
    assert (await ledger.get_episode_status("ep05"))["reserved_credits"] == 0.0
