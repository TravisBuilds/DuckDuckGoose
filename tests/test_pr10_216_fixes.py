"""
PR #10 @ 216c8b8 follow-ups (test-only, no network, no spend):

- M-145: HiggsfieldStillProvider.upload_reference must PUT the *normalized* JPEG (<=1280px,
  <=REF_MAX_BYTES, alpha flattened), never the original file, and must leave the original untouched.
- M-146: the REAL ShotWorkflow submits the _end still with shot_plan["end_prompt"] (start still keeps
  "prompt"); without end_prompt the _end still falls back to "prompt".
- M-148: a provider-failed job surfaces the harness job id AND the raw provider error.
- M-149: normalization cannot be skipped by any env var (SKIP_IMAGE_NORMALIZE must have no effect).
"""
import asyncio
import hashlib
import io
import time
from datetime import timedelta

import httpx
import pytest
import respx
from PIL import Image
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.image_normalize import normalize_reference_image


def _noisy_rgba_png(path, size=(1520, 2688)):
    """Big, photo-like (smooth, high-entropy) RGBA PNG with a transparent band (like the Studio refs
    that failed). Deterministic; not pure noise (see the separate JPEG-buffer finding)."""
    import random
    w, h = size
    rnd = random.Random(216)
    small = (max(1, w // 4), max(1, h // 4))
    img = Image.frombytes("RGB", small, bytes(rnd.getrandbits(8) for _ in range(small[0] * small[1] * 3)))
    img = img.resize(size, Image.Resampling.BILINEAR).convert("RGBA")
    alpha = Image.new("L", size, 255)
    alpha.paste(0, (0, 0, w, h // 4))  # top quarter fully transparent
    img.putalpha(alpha)
    img.save(path, "PNG")
    return path


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ M-145 upload path
@pytest.mark.asyncio
async def test_still_upload_reference_puts_normalized_jpeg_not_original(tmp_path, monkeypatch):
    (tmp_path / "refs").mkdir()
    src = _noisy_rgba_png(tmp_path / "refs" / "big.png")
    before_sha, before_size = _sha(src), src.stat().st_size
    assert before_size > 1_048_576 and max(Image.open(src).size) > 1280, f"fixture must be oversize, got {before_size}"

    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider

    puts, posts = [], []
    with respx.mock:
        def post(req):
            import json
            posts.append(json.loads(req.content))
            return httpx.Response(200, json={"upload_url": "http://mock.upload/put", "public_url": "http://mock.cdn/n.jpg",
                                             "upload_headers": {"Content-Type": "image/jpeg"}})
        respx.post("http://mock.api/files/generate-upload-url").mock(side_effect=post)
        respx.put("http://mock.upload/put").mock(side_effect=lambda r: (puts.append(r.content), httpx.Response(200))[1])
        res = await HiggsfieldStillProvider().upload_reference(str(src), version=1)

    assert res["public_url"] == "http://mock.cdn/n.jpg", f"unexpected result {res}"
    assert len(puts) == 1, f"expected exactly one PUT, got {len(puts)}"
    body = puts[0]
    assert body[:3] == b"\xff\xd8\xff", f"uploaded bytes are not JPEG (starts {body[:8]!r}): original was uploaded"
    assert body != src.read_bytes(), "uploaded the original file instead of the normalized one"
    assert len(body) <= 1_048_576, f"uploaded {len(body)} bytes > REF_MAX_BYTES"
    up = Image.open(io.BytesIO(body))
    assert up.mode == "RGB", f"uploaded mode {up.mode}, alpha not flattened"
    assert max(up.size) <= 1280, f"uploaded size {up.size} > 1280 long side"
    # transparent band flattened onto white (not black)
    r, g, b = up.getpixel((up.width // 2, 5))
    assert min(r, g, b) >= 240, f"transparent area not flattened to white: {(r, g, b)}"
    assert posts and posts[0].get("content_type") == "image/jpeg", f"content_type not image/jpeg: {posts}"
    assert _sha(src) == before_sha and src.stat().st_size == before_size, "original reference file was modified"


# ------------------------------------------------------------------ M-149 not skippable
@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "1", "TRUE"])
async def test_normalization_cannot_be_skipped_by_env(tmp_path, monkeypatch, value):
    monkeypatch.setenv("SKIP_IMAGE_NORMALIZE", value)
    monkeypatch.setenv("DRY_RUN", "false")
    src = _noisy_rgba_png(tmp_path / "ref.png", size=(1600, 900))
    out, sha = normalize_reference_image(src, cache_dir=tmp_path / "cache")
    assert out != src, f"SKIP_IMAGE_NORMALIZE={value} made normalization return the original file"
    assert out.read_bytes()[:3] == b"\xff\xd8\xff", "normalized output is not a JPEG"
    assert max(Image.open(out).size) <= 1280, "normalized output not downscaled"
    assert sha == _sha(src), "original sha (used for anchor job ids) must be returned"


# ------------------------------------------------------------------ M-147 helper (cache key)
@pytest.mark.asyncio
async def test_normalize_cache_keyed_by_content_not_name(tmp_path):
    cache = tmp_path / "cache"
    a = tmp_path / "a"; b = tmp_path / "b"; a.mkdir(); b.mkdir()
    Image.new("RGB", (64, 64), "red").save(a / "ref.png")
    Image.new("RGB", (64, 64), "blue").save(b / "ref.png")      # same name, different content
    Image.new("RGB", (64, 64), "red").save(b / "renamed.png")   # same content, different name
    n_red, _ = normalize_reference_image(a / "ref.png", cache_dir=cache)
    n_blue, _ = normalize_reference_image(b / "ref.png", cache_dir=cache)
    n_red2, _ = normalize_reference_image(b / "renamed.png", cache_dir=cache)
    assert n_red == n_red2, f"same content must reuse one cached file: {n_red} vs {n_red2}"
    assert n_red != n_blue, "different content must not share a cached file"
    assert Image.open(n_blue).getpixel((10, 10))[2] > 200, "blue ref came back with another file's pixels"


# ------------------------------------------------------------------ real ShotWorkflow harness
START = "Exactly 0 animals in frame. Real-time, no slow motion. START frame probe."
END = "Exactly 0 animals in frame. Real-time, no slow motion. END frame probe."


def _activities(log, shot, fail_still=False):
    def mk(name, fn):
        async def w(*a):
            return await fn(*a)
        return activity.defn(name=name)(w)

    async def ok(*a): return {"ok": True, "valid": True, "errors": [], "warnings": []}
    async def parse(*a): return {"shots": [dict(shot)]}
    async def g1(*a):
        return {"shots": [{"shot_id": shot["shot_id"], "still_credits": 0.1, "clip_credits": 0.1, "total_credits": 0.2}],
                "total_credits": 0.2, "total_usd": "0.2", "estimates_available": True}
    async def binit(*a):
        from hfvg.budget import REQUIRED_BUDGET_LINES
        return {"line_count": len(REQUIRED_BUDGET_LINES), "lines": REQUIRED_BUDGET_LINES}
    async def true_(*a): return True
    async def empty_d(*a): return {}
    async def empty_l(*a): return []
    async def zero(*a): return 0
    async def none(*a): return None
    async def path(*a): return "/tmp/fake-still.jpg"
    async def passed(*a): return {"passed": True, "issues": []}
    async def submit_still(ep, sid, prompt, version, *rest):
        log.append(("submit_still", sid, prompt))
        return {"job_id": f"still-{ep}-{sid}-v{version}", "line_name": "L", "reserved_amount": 1}
    async def poll(job_id, *a):
        if fail_still and job_id.startswith("still-"):
            return {"status": "failed", "error": "Generation failed (raw provider text 7f3a)"}
        log.append(("landed", job_id))
        return {"status": "completed", "output_url": f"fake://{job_id}.jpg", "cost": 1}
    async def submit_clip(ep, sid, *rest):
        log.append(("submit_clip", sid))
        return {"job_id": f"clip-{ep}-{sid}", "line_name": "L", "reserved_amount": 1}

    out = [mk(n, f) for n, f in [
        ("acquire_run_lock", true_), ("release_run_lock", true_), ("load_gate_policy_activity", empty_d),
        ("parse_beatmap_activity", parse), ("load_refs_to_generate_activity", empty_l), ("generate_run_report", empty_d),
        ("compute_g1_plan_activity", g1), ("init_episode_budget_lines_activity", binit), ("approve_g2_shots", zero),
        ("list_anchor_stills", empty_l), ("precheck_still_qc", passed), ("submit_still_job_enforced", submit_still),
        ("poll_job_status", poll), ("commit_job_budget", none), ("commit_credits_activity", none),
        ("release_job_budget", none), ("release_credits_activity", none), ("save_asset_locally", path),
        ("review_still", passed), ("record_shot_result", none), ("mark_job_pending_reconcile", none),
        ("review_clip", passed), ("precheck_clip_qc", passed), ("submit_clip_job_enforced", submit_clip)]]
    out += [mk(n, ok) for n in [
        "init_episode_credit_line_activity", "preflight_validate_episode_activity", "upsert_episode_activity",
        "verify_episode_files_activity", "approve_g108_activity", "recheck_refs_activity", "update_credit_cap_activity"]]
    return out


async def _run_episode(tmp_path, monkeypatch, shot, done, fail_still=False, budget=20.0):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "studio.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    from hfvg.workflows.episode_v2 import EpisodeWorkflowV2
    from hfvg.workflows.shot import ShotWorkflow
    log, st = [], None
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue="pr10-216", workflows=[EpisodeWorkflowV2, ShotWorkflow],
                          activities=_activities(log, shot, fail_still)):
            h = await env.client.start_workflow(EpisodeWorkflowV2.run, args=["ep05", None, True, [shot["shot_id"]], 1000],
                                                id="ep05", task_queue="pr10-216")
            async def state():
                return await asyncio.wait_for(h.query(EpisodeWorkflowV2.get_state, rpc_timeout=timedelta(seconds=2)), 3)
            try:
                end = time.time() + 10
                while time.time() < end:
                    try:
                        st = await state()
                        if st.get("current_gate") == "G1_SHOT_PLAN":
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
                assert st and st.get("current_gate") == "G1_SHOT_PLAN", f"never reached G1: {st}"
                await h.execute_update(EpisodeWorkflowV2.approve_g1_shot_plan)
                end = time.time() + budget
                while time.time() < end:
                    try:
                        st = await state()
                        if done(st, log):
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.05)
            finally:
                for wid in ("ep05", f"ep05-shot-{shot['shot_id']}"):
                    try:
                        await env.client.get_workflow_handle(wid).terminate("test done")
                    except Exception:
                        pass
    return st, log


def _shot(**extra):
    s = {"shot_id": "A02", "prompt": START, "duration": 3.0, "params": {"end_frame": True},
         "shot_meta": {"count": 0, "characters": [], "wet": False}}
    s.update(extra)
    return s


def _both_landed(st, log):
    landed = [e[1] for e in log if e[0] == "landed"]
    return "still-ep05-A02-v1" in landed and "still-ep05-A02_end-v1" in landed


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_real_shot_end_still_uses_end_prompt(tmp_path, monkeypatch):
    st, log = await _run_episode(tmp_path, monkeypatch, _shot(end_prompt=END), _both_landed)
    subs = {e[1]: e[2] for e in log if e[0] == "submit_still"}
    assert "A02" in subs and "A02_end" in subs, f"both stills not submitted: {log}"
    assert subs["A02"] == START, f"start still must use prompt, got {subs['A02']!r}"
    assert subs["A02_end"] == END, f"_end still must use end_prompt, got {subs['A02_end']!r}"
    assert not any(e[0] == "submit_clip" for e in log), f"clip started before G2: {log}"


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_real_shot_end_still_falls_back_to_prompt(tmp_path, monkeypatch):
    st, log = await _run_episode(tmp_path, monkeypatch, _shot(), _both_landed)
    subs = {e[1]: e[2] for e in log if e[0] == "submit_still"}
    assert subs.get("A02_end") == START, f"_end without end_prompt must reuse prompt, got {subs!r}"


@pytest.mark.asyncio
@pytest.mark.timeout(90)
async def test_failed_job_reason_has_job_id_and_raw_error(tmp_path, monkeypatch):
    st, log = await _run_episode(tmp_path, monkeypatch, _shot(end_prompt=END),
                                 lambda s, l: bool(s.get("failed_shots")), fail_still=True)
    assert st and st.get("failed_shots"), f"shot never failed: {st}"
    reason = st["failed_shots"][0]["reason"]
    assert "still-ep05-A02-v1" in reason, f"failure reason lacks the job id: {reason!r}"
    assert "Generation failed (raw provider text 7f3a)" in reason, f"failure reason lacks the raw provider error: {reason!r}"
    assert not any(e[1] == "A02_end" for e in log if e[0] == "submit_still"), f"_end submitted after start failed: {log}"


def test_normalize_high_detail_image_does_not_crash(tmp_path):
    """Pure-noise 1600x900: q88 4:4:4 JPEG exceeds Pillow's w*h optimize buffer -> OSError before the fix."""
    import random
    rnd = random.Random(7)
    src = tmp_path / "noise.png"
    Image.frombytes("RGB", (1600, 900), bytes(rnd.getrandbits(8) for _ in range(1600 * 900 * 3))).save(src)
    out, _ = normalize_reference_image(src, cache_dir=tmp_path / "c")
    assert out.read_bytes()[:3] == b"\xff\xd8\xff", "output is not a JPEG"
    assert max(Image.open(out).size) <= 1280, "not downscaled"


@pytest.mark.asyncio
async def test_smoke_upload_probe_passes_with_normalized_upload(tmp_path, monkeypatch):
    """Live smoke failed Oct 18 14:13: probe compared the CDN bytes to the original 69-byte PNG, but
    upload_reference now uploads the normalized JPEG (286 bytes). The probe must verify the uploaded bytes."""
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    from hfvg.activities.smoke_test import _probe_upload
    stored = {}
    with respx.mock:
        respx.post("http://mock.api/files/generate-upload-url").mock(return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put", "public_url": "http://mock.cdn/probe.jpg",
            "upload_headers": {"Content-Type": "image/jpeg"}}))
        respx.put("http://mock.upload/put").mock(side_effect=lambda r: (stored.update(b=r.content), httpx.Response(200))[1])
        respx.get("http://mock.cdn/probe.jpg").mock(side_effect=lambda r: httpx.Response(200, content=stored["b"]))
        res = await _probe_upload(HiggsfieldStillProvider())
    assert stored["b"][:3] == b"\xff\xd8\xff", "probe upload should be the normalized JPEG"
    assert res["passed"], f"smoke upload probe failed against its own upload: {res}"
