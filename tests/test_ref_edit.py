"""Ref EDIT path: a new ref made from an existing ref image through the still model (L1_refs)."""

import io
import json
import hashlib
from pathlib import Path

import pytest
from PIL import Image

from hfvg.credit_ledger import CreditLedger


def _png(w=320, h=560, color=(200, 190, 170)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _write_refs(tmp_path, entry, per_shot=None):
    ep = tmp_path / "ep99"
    ep.mkdir(exist_ok=True)
    (ep / "refs.json").write_text(json.dumps({
        "episode_id": "ep99",
        "per_shot": per_shot or {},
        "to_generate": [entry],
    }))


# ---------------------------------------------------------------- loader


@pytest.mark.asyncio
async def test_loader_passes_edit_from_and_marks_type(tmp_path, monkeypatch):
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    src = tmp_path / "src.png"
    src.write_bytes(_png())
    out = str(tmp_path / "refs" / "duck_cuff.png")
    _write_refs(tmp_path, {"key": "duck_cuff", "prompt": "remove the knot", "output_path": out,
                           "edit_from": str(src)})
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    [spec] = await load_refs_to_generate_activity("ep99", None, True)
    assert spec["edit_from"] == str(src)
    assert spec["type"] == "edit"
    assert spec["version"] == 1


@pytest.mark.asyncio
async def test_loader_refuses_in_place_edit(tmp_path, monkeypatch):
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    src = tmp_path / "src.png"
    src.write_bytes(_png())
    _write_refs(tmp_path, {"key": "duck", "prompt": "p", "output_path": str(src), "edit_from": str(src)})
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="NEW ref"):
        await load_refs_to_generate_activity("ep99", None, False)


@pytest.mark.asyncio
async def test_loader_live_refuses_missing_source_dry_allows(tmp_path, monkeypatch):
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    entry = {"key": "duck_cuff", "prompt": "p", "output_path": str(tmp_path / "o.png"),
             "edit_from": str(tmp_path / "missing.png")}
    _write_refs(tmp_path, entry)
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    assert len(await load_refs_to_generate_activity("ep99", None, False)) == 1
    _write_refs(tmp_path, entry)
    with pytest.raises(ValueError, match="not on disk"):
        await load_refs_to_generate_activity("ep99", None, True)


@pytest.mark.asyncio
async def test_loader_refuses_url_source(tmp_path, monkeypatch):
    from hfvg.activities.ref_generation import load_refs_to_generate_activity
    _write_refs(tmp_path, {"key": "k", "prompt": "p", "output_path": str(tmp_path / "o.png"),
                           "edit_from": "https://example.com/a.png"})
    monkeypatch.setenv("EPISODES_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="not a URL"):
        await load_refs_to_generate_activity("ep99", None, False)


# ---------------------------------------------------------------- activity fakes


class FakeBudget:
    calls: list = []
    reserve_ok = True

    def __init__(self, db_path):
        pass

    async def init_db(self):
        pass

    async def reserve(self, **kw):
        FakeBudget.calls.append(("reserve", kw))
        return FakeBudget.reserve_ok

    async def commit(self, **kw):
        FakeBudget.calls.append(("commit", kw))

    async def release(self, **kw):
        FakeBudget.calls.append(("release", kw))
        return kw["amount_usd_micros"]


class FakeStill:
    instances: list = []
    estimate_fails = False
    final_status = "completed"
    model_path = "xai/grok-imagine-image-2.0"

    def __init__(self, *a, **k):
        self.calls = []
        FakeStill.instances.append(self)

    async def upload_reference(self, path, version=1):
        self.calls.append(("upload", str(path), version))
        return {"public_url": "https://cdn.example/src.jpg"}

    async def estimate_usd_and_credits(self, **kw):
        self.calls.append(("estimate", kw))
        if FakeStill.estimate_fails:
            raise ValueError("Failed to get estimate")
        return 60_000, 0.96

    async def submit_image(self, **kw):
        self.calls.append(("submit", kw))
        return "req-1"

    async def get_job_status(self, rid):
        from hfvg.providers.base import ProviderJob, ProviderJobStatus
        st = ProviderJobStatus.COMPLETED if FakeStill.final_status == "completed" else ProviderJobStatus.FAILED
        return ProviderJob(job_id=rid, status=st, progress=1.0,
                           output_url="https://cdn.example/out.png" if st == ProviderJobStatus.COMPLETED else None,
                           cost=None, error=None if st == ProviderJobStatus.COMPLETED else "Generation failed",
                           raw={}, response_headers={})

    async def close(self):
        pass


class FakeResp:
    content = _png(640, 1136, (230, 225, 210))

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return FakeResp()


@pytest.fixture
async def live_env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    from hfvg.studio_db import init_studio_db, create_episode
    await init_studio_db(db_path)
    await create_episode(db_path, "ep99")
    cl = CreditLedger(db_path)
    await cl.init_db()
    await cl.create_episode_line("ep99", cap_credits=1000)

    async def _ok(*a, **k):
        return None

    async def _live(ep):
        return True, True

    import hfvg.studio_db as sdb
    import hfvg.activities.studio_generation as sg
    import hfvg.budget as budget
    import hfvg.providers.higgsfield_still as hs
    import httpx
    monkeypatch.setattr(sdb, "enforce_smoke_test_gate", _ok)
    monkeypatch.setattr(sg, "check_live_mode_and_g108", _live)
    monkeypatch.setattr(sg, "_check_higgsfield_credentials", lambda: None)
    monkeypatch.setattr(budget, "BudgetLedger", FakeBudget)
    monkeypatch.setattr(hs, "HiggsfieldStillProvider", FakeStill)
    monkeypatch.setattr(httpx, "AsyncClient", FakeHttp)
    FakeBudget.calls = []
    FakeBudget.reserve_ok = True
    FakeStill.instances = []
    FakeStill.estimate_fails = False
    FakeStill.final_status = "completed"
    src = tmp_path / "src" / "DUCK.png"
    src.parent.mkdir()
    src.write_bytes(_png())
    return {"db": db_path, "src": str(src), "out": str(tmp_path / "refs" / "DUCK-cuff.png")}


async def _run(env, version=1):
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    return await generate_ref_edit_activity("ep99", "duck_cuff", "paint out the knot", env["out"],
                                            version, env["src"])


@pytest.mark.asyncio
async def test_live_edit_sends_source_estimates_reserves_l1_and_commits(live_env):
    src_before = Path(live_env["src"]).read_bytes()
    r = await _run(live_env)
    assert r["status"] == "completed"
    sha8 = hashlib.sha256(src_before).hexdigest()[:8]
    assert r["job_id"] == f"ref-ep99-duck_cuff-v1-edit{sha8}"
    p = FakeStill.instances[0]
    kinds = [c[0] for c in p.calls]
    # estimates guard: estimate BEFORE submit, upload of the source BEFORE submit
    assert kinds.index("upload") < kinds.index("estimate") < kinds.index("submit")
    submit = next(c[1] for c in p.calls if c[0] == "submit")
    assert submit["image_urls"] == ["https://cdn.example/src.jpg"]
    assert submit["resolution"] == "2k" and submit["aspect_ratio"] == "9:16"
    assert submit["prompt"] == "paint out the knot"
    res = [c[1] for c in FakeBudget.calls if c[0] == "reserve"]
    com = [c[1] for c in FakeBudget.calls if c[0] == "commit"]
    assert res == [{"episode_id": "ep99", "line_name": "L1_refs", "amount_usd_micros": 60_000,
                    "reason": "Ref edit duck_cuff v1", "job_id": r["job_id"], "require_balance": True}]
    assert com and com[0]["line_name"] == "L1_refs" and com[0]["actual_usd_micros"] == 60_000
    assert Path(live_env["out"]).exists()
    assert Path(live_env["src"]).read_bytes() == src_before  # source untouched
    side = json.loads((Path(live_env["out"]).parent / ".versions" / "DUCK-cuff.json").read_text())
    assert side["version"] == 1 and side["edit_from_sha256"] == hashlib.sha256(src_before).hexdigest()


@pytest.mark.asyncio
async def test_live_edit_estimate_failure_reserves_and_submits_nothing(live_env):
    FakeStill.estimate_fails = True
    with pytest.raises(ValueError, match="estimate"):
        await _run(live_env)
    assert not [c for c in FakeBudget.calls if c[0] in ("reserve", "commit")]
    assert "submit" not in [c[0] for c in FakeStill.instances[0].calls]
    assert not Path(live_env["out"]).exists()


@pytest.mark.asyncio
async def test_live_edit_budget_refusal_never_submits(live_env):
    FakeBudget.reserve_ok = False
    with pytest.raises(ValueError, match="L1_refs"):
        await _run(live_env)
    assert "submit" not in [c[0] for c in FakeStill.instances[0].calls]
    assert [c for c in FakeBudget.calls if c[0] == "release"] == []  # reserve refused: nothing held


@pytest.mark.asyncio
async def test_live_edit_provider_failure_releases_l1(live_env):
    FakeStill.final_status = "failed"
    with pytest.raises(RuntimeError, match="failed"):
        await _run(live_env)
    rel = [c[1] for c in FakeBudget.calls if c[0] == "release"]
    assert rel and rel[0]["line_name"] == "L1_refs" and rel[0]["amount_usd_micros"] == 60_000
    assert not [c for c in FakeBudget.calls if c[0] == "commit"]
    assert not Path(live_env["out"]).exists()


@pytest.mark.asyncio
async def test_live_edit_reuses_existing_same_version_and_source(live_env):
    await _run(live_env)
    FakeStill.instances = []
    FakeBudget.calls = []
    r = await _run(live_env)
    assert r["status"] == "reused" and r["usd_micros"] == 0
    assert FakeStill.instances == [] and FakeBudget.calls == []


@pytest.mark.asyncio
async def test_live_edit_new_source_archives_and_regenerates(live_env):
    await _run(live_env)
    Path(live_env["src"]).write_bytes(_png(color=(10, 20, 30)))
    FakeStill.instances = []
    r = await _run(live_env)
    assert r["status"] == "completed"
    assert (Path(live_env["out"]).parent / "_v1" / "DUCK-cuff.png").exists()


@pytest.mark.asyncio
async def test_edit_refuses_bad_resolution_before_anything(live_env):
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    with pytest.raises(ValueError, match="resolution"):
        await generate_ref_edit_activity("ep99", "duck_cuff", "p", live_env["out"], 1, live_env["src"],
                                         resolution="1080p")
    assert FakeStill.instances == [] and FakeBudget.calls == []


@pytest.mark.asyncio
async def test_dry_edit_writes_scratch_only(tmp_path, monkeypatch):
    import temporalio.activity
    from hfvg.activities.ref_generation import generate_ref_edit_activity
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(temporalio.activity, "info", lambda: type("o", (), {"workflow_id": "wf-edit"})())
    src = tmp_path / "s.png"
    src.write_bytes(_png())
    real = tmp_path / "refs" / "o.png"
    r = await generate_ref_edit_activity("ep99", "k", "p", str(real), 1, str(src))
    assert r["status"] == "completed" and not real.exists()
    assert (tmp_path / "data/dry_run_outputs/wf-edit/refs/o.png").exists()


def test_worker_registers_edit_activity_and_workflow_routes_only_edit_from():
    import inspect
    from hfvg import worker
    from hfvg.workflows import episode_v2
    src = inspect.getsource(worker)
    assert "generate_ref_edit_activity" in src
    wf = inspect.getsource(episode_v2.EpisodeWorkflowV2)
    i = wf.index('if ref_spec.get("edit_from")')
    assert wf.index('"generate_ref_edit_activity"') > i
    assert wf.index('"generate_ref_activity"') > wf.index('"generate_ref_edit_activity"')
