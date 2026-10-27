"""Preflight, per_shot refs schema: a ref generated after G1 (list-form to_generate, e.g. a ref edit) may be
missing on disk at preflight, but only at its own planned output_path and only with a prompt."""

import json

import pytest


async def _problems(tmp_path, monkeypatch, refs):
    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    from hfvg.studio_db import init_studio_db
    import temporalio.activity

    base = tmp_path / "data" / "episodes" / "ep99"
    base.mkdir(parents=True, exist_ok=True)
    (base / "BEATMAP.md").write_text("A01 (2.5s)")
    (base / "shots.json").write_text(json.dumps({"A01": {
        "prompt": "Test shot", "params": {"model": "kling", "duration": 3, "aspect_ratio": "9:16"}}}))
    (base / "refs.json").write_text(json.dumps(refs))
    (base / "prompt_kit.json").write_text("{}")
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.chdir(tmp_path)
    await init_studio_db(db)
    orig = temporalio.activity.info
    temporalio.activity.info = lambda: type("o", (), {"workflow_id": "wf"})()
    try:
        await preflight_validate_episode_activity("ep99", None, False)
        return []
    except PreflightError as e:
        return [p for p in e.problems if "Ref " in p or "to_generate" in p]
    finally:
        temporalio.activity.info = orig


def _refs(out, planned):
    return {"per_shot": {"A01": [{"key": "duck_cuff", "path": str(out)}]}, "to_generate": planned}


@pytest.mark.asyncio
async def test_planned_ref_missing_on_disk_is_ok(tmp_path, monkeypatch):
    out = tmp_path / "refs" / "cuff.png"
    planned = [{"key": "duck_cuff", "output_path": str(out), "edit_from": "/x.png", "prompt": "edit"}]
    assert await _problems(tmp_path, monkeypatch, _refs(out, planned)) == []


@pytest.mark.asyncio
async def test_unplanned_missing_ref_still_refused(tmp_path, monkeypatch):
    out = tmp_path / "refs" / "cuff.png"
    probs = await _problems(tmp_path, monkeypatch, _refs(out, []))
    assert any("listed in per_shot but file missing" in p for p in probs)


@pytest.mark.asyncio
async def test_planned_at_other_path_still_refused(tmp_path, monkeypatch):
    out = tmp_path / "refs" / "cuff.png"
    planned = [{"key": "duck_cuff", "output_path": str(tmp_path / "other.png"), "prompt": "edit"}]
    probs = await _problems(tmp_path, monkeypatch, _refs(out, planned))
    assert any("listed in per_shot but file missing" in p for p in probs)


@pytest.mark.asyncio
async def test_planned_without_prompt_refused(tmp_path, monkeypatch):
    out = tmp_path / "refs" / "cuff.png"
    planned = [{"key": "duck_cuff", "output_path": str(out)}]
    probs = await _problems(tmp_path, monkeypatch, _refs(out, planned))
    assert any("missing prompt" in p for p in probs)
    assert any("listed in per_shot but file missing" in p for p in probs)
