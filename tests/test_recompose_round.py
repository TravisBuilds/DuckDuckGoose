"""GAP-25 (ep05full F06 recompose 2, 2026-10-27): a recompose still (new composition ordered at the
G3 review) was authorized as a G2 still re-roll (attempt > 1 -> G2_STILLS) and refused because F06
had used its G2 re-rolls. It is now held in its own review round G3_RECOMPOSE (same round cap, 80%
stop and per-shot paid-retry limit; episode stop unchanged). No network, no spend."""
import aiosqlite
import httpx
import pytest
import respx

from tests.lint_helpers import lint_meta, lint_ok
from tests.test_moderation_recovery import _live_episode


def _routes(router):
    body = {"credits": "0.96", "usd": "0.06", "request_id": "prov-s1",
            "public_url": "https://cdn.example.com/r.png", "upload_url": "https://upload.example.com/r",
            "upload_headers": {}}
    router.route(method="GET").mock(return_value=httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"0" * 64))
    router.put("https://upload.example.com/r").mock(return_value=httpx.Response(200))
    return router.route(method="POST").mock(return_value=httpx.Response(200, json=body))


async def _episode(tmp_path, monkeypatch, limit="2"):
    from tests.ledger_helpers import insert_line
    db_path, ledger, credits = await _live_episode(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", limit)
    async with aiosqlite.connect(db_path) as db:
        await insert_line(db, "ep99", "L2_drafts", 2_000_000, 1_600_000)
        await db.commit()
    ref = tmp_path / "rowan.png"
    ref.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    # the shot already used its G2 still re-rolls
    for k in ("r1", "r2"):
        await ledger.authorize_round_spend("ep99", "G2_STILLS", "A01", f"still-ep99-A01-{k}", 60_000, retry_key=k)
    return ledger, str(ref)


async def _submit(ref, review_round, attempt=4):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    return await submit_still_job_enforced(
        "ep99", "A01", lint_ok("Tighter wide shot"), attempt, [ref], "1k", "medium", "9:16", None,
        lint_meta(), [], attempt, None, None, review_round)


@pytest.mark.asyncio
async def test_recompose_still_is_held_in_its_own_round(tmp_path, monkeypatch):
    ledger, ref = await _episode(tmp_path, monkeypatch)
    from temporalio.exceptions import ApplicationError
    with respx.mock(assert_all_called=False) as router:
        _routes(router)
        with pytest.raises(ApplicationError) as e:          # as a G2 re-roll: refused (the bug)
            await _submit(ref, None)
        assert e.value.type == "RoundBudgetError"
        result = await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc1"})
    hold = await ledger.get_round_hold(result["job_id"])
    assert hold["round_id"] == "G3_RECOMPOSE" and hold["retry_key"] == "rc1" and hold["shot_id"] == "A01", hold


@pytest.mark.asyncio
async def test_recompose_round_keeps_its_per_shot_limit(tmp_path, monkeypatch):
    ledger, ref = await _episode(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_PAID_RETRIES_PER_SHOT_PER_ROUND", "1")
    await ledger.authorize_round_spend("ep99", "G3_RECOMPOSE", "A01", "still-ep99-A01-r3", 60_000, retry_key="rc1")
    from temporalio.exceptions import ApplicationError
    with respx.mock(assert_all_called=False) as router:
        post = _routes(router)
        with pytest.raises(ApplicationError) as e:
            await _submit(ref, {"round": "G3_RECOMPOSE", "retry_key": "rc2"}, attempt=5)
        assert e.value.type == "RoundBudgetError" and "G3_RECOMPOSE" in str(e.value)
        submitted = [c for c in post.calls if "estimate" not in str(c.request.url)
                     and "upload" not in str(c.request.url)]
    assert submitted == []  # refused before the provider submit


@pytest.mark.asyncio
async def test_only_the_recompose_round_is_accepted(tmp_path, monkeypatch):
    ledger, ref = await _episode(tmp_path, monkeypatch)
    from temporalio.exceptions import ApplicationError
    with respx.mock(assert_all_called=False) as router:
        _routes(router)
        with pytest.raises(ApplicationError, match="invalid review_round"):
            await _submit(ref, {"round": "G2_STILLS", "retry_key": "x"})
