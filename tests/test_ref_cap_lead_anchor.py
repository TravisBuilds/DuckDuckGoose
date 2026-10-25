"""Effective still ref cap (Grok Image 2.0 = 5) and the lead/anchor rule.

Live evidence (hfvg/config.py): every instant "Generation failed" on the still model came with 6-7
refs and every success had <=5. Rule: when a lead ref (the shot's start still, for _end frames) is
present and the set is over the cap, the A01 anchor is dropped FIRST, so A02_end becomes
start, duck, family, subject01, pool. If still over the cap: refuse before any spend; never drop others.
"""
import hashlib
import io
import json

import httpx
import pytest
import respx
from PIL import Image

from tests.lint_helpers import acapture, lint_meta, lint_ok
from tests.test_round4_shot_data import _live_env

GROK = "xai/grok-imagine-image-2.0"
START_URL = "https://d3u0tzju9qaucj.cloudfront.net/out/a02-start.jpeg"
COLORS = {
    "start": (200, 120, 40), "anchor": (250, 250, 250), "duck": (0, 200, 0), "family": (0, 0, 200),
    "subject01": (200, 0, 200), "pool": (0, 200, 200), "extra": (120, 120, 0),
}


def _png(path, color, size=(64, 64)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "PNG")
    return str(path)


def _start_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (720, 1280), COLORS["start"]).save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _name_of(data):
    px = Image.open(io.BytesIO(data)).convert("RGB")
    px = px.getpixel((px.width // 2, px.height // 2))
    best = min(COLORS, key=lambda k: sum((a - b) ** 2 for a, b in zip(COLORS[k], px)))
    assert sum((a - b) ** 2 for a, b in zip(COLORS[best], px)) < 900, f"unrecognized ref colour {px}"
    return best


async def _setup(tmp_path, monkeypatch, *, anchor=True):
    from hfvg.studio_db import set_anchor_still

    db_path = await _live_env(tmp_path, monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MODEL_MAX_REFS_JSON", raising=False)
    monkeypatch.delenv("MODEL_PATH_STILL", raising=False)
    refs = {k: _png(tmp_path / "refs" / f"{k}.png", COLORS[k]) for k in ("duck", "family", "subject01", "pool", "extra")}
    anchor_sha = None
    if anchor:
        a = _png(tmp_path / "anchors" / "A01.png", COLORS["anchor"])
        anchor_sha = hashlib.sha256(open(a, "rb").read()).hexdigest()
        await set_anchor_still(db_path, "ep99", "A01", ["A02"], a, 1, anchor_sha)
    puts, n = {}, {"i": 0}

    def post(req):
        n["i"] += 1
        return httpx.Response(200, json={"upload_url": f"http://mock.upload/put{n['i']}",
                                         "public_url": f"https://cdn.upload/ref{n['i']}.jpg",
                                         "upload_headers": {"Content-Type": "image/jpeg"}})
    respx.post("https://api.higgsfield.ai/files/generate-upload-url").mock(side_effect=post)
    respx.put(url__regex=r"http://mock\.upload/put\d+").mock(
        side_effect=lambda r: (puts.__setitem__(str(r.url).rsplit("put", 1)[1], r.content), httpx.Response(200))[1])
    respx.get(START_URL).mock(return_value=httpx.Response(200, content=_start_bytes(), headers={"content-type": "image/jpeg"}))
    est = respx.post(f"https://api.higgsfield.ai/estimate/{GROK}").mock(
        return_value=httpx.Response(200, json={"credits": "0.96", "usd": "0.06"}))
    sub = respx.post(f"https://api.higgsfield.ai/{GROK}").mock(return_value=httpx.Response(200, json={"request_id": "req"}))
    return db_path, refs, anchor_sha, puts, est, sub


def _sent(sub, puts):
    urls = json.loads(sub.calls[0].request.content)["image_urls"]
    return [_name_of(puts[u.rsplit("ref", 1)[1].replace(".jpg", "")]) for u in urls]


START_SHA8 = hashlib.sha256(_start_bytes()).hexdigest()[:8]


def _submit(shot, planned, lead):
    from hfvg.activities.studio_generation import submit_still_job_enforced
    return submit_still_job_enforced("ep99", shot, lint_ok("S"), 1, planned, "1k", "medium", "9:16",
                                     None, lint_meta(), [], 4, lead)


async def _ok(shot, planned, lead):
    """Submit expecting success; a refusal surfaces as an AssertionError (not a bare ValueError)."""
    box = {}

    async def run():
        box["out"] = await _submit(shot, planned, lead)
    err = await acapture(ValueError, run())
    assert err is None, f"{shot} with {len(planned)} planned refs (lead={bool(lead)}) was refused: {err}"
    return box["out"]


@pytest.mark.asyncio
@respx.mock
async def test_end_still_drops_anchor_first_when_lead_pushes_over_cap(tmp_path, monkeypatch):
    _, refs, anchor_sha, puts, _, sub = await _setup(tmp_path, monkeypatch)
    planned = [refs[k] for k in ("duck", "family", "subject01", "pool")]
    out = await _ok("A02_end", planned, START_URL)
    assert sub.call_count == 1
    assert _sent(sub, puts) == ["start", "duck", "family", "subject01", "pool"], _sent(sub, puts)
    # the id describes what was sent: lead suffix, and no anchor suffix once the anchor is dropped
    assert out["job_id"] == f"still-ep99-A02_end-r3-lead{START_SHA8}", out


@pytest.mark.asyncio
@respx.mock
async def test_end_still_keeps_anchor_after_lead_when_within_cap(tmp_path, monkeypatch):
    _, refs, anchor_sha, puts, _, sub = await _setup(tmp_path, monkeypatch)
    out = await _ok("A02_end", [refs[k] for k in ("duck", "family", "subject01")], START_URL)
    assert _sent(sub, puts) == ["start", "anchor", "duck", "family", "subject01"], _sent(sub, puts)
    assert out["job_id"] == f"still-ep99-A02_end-r3-lead{START_SHA8}-anchor{anchor_sha[:8]}", out


@pytest.mark.asyncio
@respx.mock
async def test_end_still_still_over_cap_after_anchor_drop_is_refused_before_spend(tmp_path, monkeypatch):
    from hfvg.budget import BudgetLedger

    db_path, refs, _, puts, est, sub = await _setup(tmp_path, monkeypatch)
    planned = [refs[k] for k in ("duck", "family", "subject01", "pool", "extra")]
    err = await acapture(ValueError, _submit("A02_end", planned, START_URL))
    from hfvg.preflight import PreflightError
    assert isinstance(err, PreflightError) and err.non_retryable, repr(err)
    assert "6 reference images" in str(err) and "at most 5" in str(err) and "start still" in str(err), err
    assert sub.call_count == 0 and est.call_count == 0 and not puts, "over-cap _end reached the provider"
    status = await BudgetLedger(db_path).get_line_status("ep99", "L2_drafts")
    assert status["reserved"] == 0, status


@pytest.mark.asyncio
@respx.mock
async def test_start_still_keeps_anchor_first_at_cap(tmp_path, monkeypatch):
    _, refs, anchor_sha, puts, _, sub = await _setup(tmp_path, monkeypatch)
    out = await _ok("A02", [refs[k] for k in ("duck", "family", "subject01", "pool")], None)
    assert _sent(sub, puts) == ["anchor", "duck", "family", "subject01", "pool"], _sent(sub, puts)
    assert out["job_id"] == f"still-ep99-A02-r3-anchor{anchor_sha[:8]}", out


@pytest.mark.asyncio
@respx.mock
async def test_start_still_never_drops_anchor_without_a_lead(tmp_path, monkeypatch):
    _, refs, _, puts, est, sub = await _setup(tmp_path, monkeypatch)
    planned = [refs[k] for k in ("duck", "family", "subject01", "pool", "extra")]
    err = await acapture(ValueError, _submit("A02", planned, None))
    assert err is not None, f"over-cap start still (no lead) was submitted, sent {_sent(sub, puts) if sub.call_count else []}"
    from hfvg.preflight import PreflightError
    assert isinstance(err, PreflightError), repr(err)
    assert "6 reference images" in str(err) and "at most 5" in str(err) and "Remove 1" in str(err), err
    assert sub.call_count == 0 and est.call_count == 0 and not puts


@pytest.mark.asyncio
@respx.mock
async def test_end_still_without_anchor_keeps_lead_first(tmp_path, monkeypatch):
    _, refs, _, puts, _, sub = await _setup(tmp_path, monkeypatch, anchor=False)
    out = await _ok("A02_end", [refs[k] for k in ("duck", "family", "subject01", "pool")], START_URL)
    assert _sent(sub, puts) == ["start", "duck", "family", "subject01", "pool"], _sent(sub, puts)
    assert out["job_id"] == f"still-ep99-A02_end-r3-lead{START_SHA8}", out


@pytest.mark.asyncio
@respx.mock
async def test_cap_is_configurable_per_model_and_keeps_anchor_when_raised(tmp_path, monkeypatch):
    _, refs, anchor_sha, puts, _, sub = await _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_MAX_REFS_JSON", json.dumps({GROK: 6}))
    monkeypatch.setenv("MODEL_CAPS_ALLOW_RAISE", "1")  # raising above the documented cap needs the explicit flag
    out = await _ok("A02_end", [refs[k] for k in ("duck", "family", "subject01", "pool")], START_URL)
    assert _sent(sub, puts) == ["start", "anchor", "duck", "family", "subject01", "pool"], _sent(sub, puts)
    assert out["job_id"] == f"still-ep99-A02_end-r3-lead{START_SHA8}-anchor{anchor_sha[:8]}", out
