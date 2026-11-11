"""Hosted layout regression: the worker runs with cwd /app (the API with /app/api) and
EPISODES_DIR=/data/episodes, DATA_DIR=/data. Episode inputs, generated outputs and dry-run files
must resolve from those env dirs; a relative data/... path resolves against the cwd instead, so a
live run failed preflight with "Missing required file: BEATMAP.md" although the files existed.

Every test runs from a cwd with no data/ folder while DATA_DIR / EPISODES_DIR point at another temp
dir holding a valid episode, and asserts the activity read or wrote under the env dirs and created
nothing under the cwd.
"""

import dataclasses
import hashlib
import json
import sys
from pathlib import Path

import aiosqlite
import pytest
from PIL import Image
from temporalio.testing import ActivityEnvironment

EP = "ep42"


@dataclasses.dataclass
class Hosted:
    cwd: Path
    data: Path
    episodes: Path
    db: str

    @property
    def episode(self) -> Path:
        return self.episodes / EP

    def assert_cwd_clean(self) -> None:
        stray = sorted(str(p.relative_to(self.cwd)) for p in self.cwd.rglob("*"))
        assert stray == [], f"wrote under the cwd instead of DATA_DIR / EPISODES_DIR: {stray}"


def _write_episode(ep: Path) -> None:
    refs = ep / "refs"
    refs.mkdir(parents=True)
    Image.new("RGB", (64, 64), (40, 90, 160)).save(refs / "duck.png")
    (ep / "BEATMAP.md").write_text(
        "# Episode 42\n\n## A01\nThe duck sets a cup on the table.\n\n## A02\nThe duck looks out of the window.\n",
        encoding="utf-8")
    (ep / "shots.json").write_text(json.dumps({"shots": [
        {"id": "A01", "prompt": "the duck sets a cup on the table, r_duck", "count": 1, "characters": ["D"],
         "wet": False, "model": "kling", "duration": 4, "end_frame": False, "ref_keys": ["r_duck"],
         "object_contact": True},
        {"id": "A02", "prompt": "the duck looks out of the window, r_duck", "count": 1, "characters": ["D"],
         "wet": False, "model": "kling", "duration": 4, "end_frame": False, "ref_keys": ["r_duck"],
         "object_contact": False},
    ]}), encoding="utf-8")
    duck = {"key": "r_duck", "path": str(refs / "duck.png")}
    (ep / "refs.json").write_text(json.dumps({"per_shot": {"A01": [duck], "A02": [duck]}}), encoding="utf-8")
    (ep / "prompt_kit.json").write_text(json.dumps({"style": "storybook"}), encoding="utf-8")


@pytest.fixture
def hosted(tmp_path, monkeypatch):
    cwd = tmp_path / "app"
    cwd.mkdir()
    data = tmp_path / "volume" / "data"
    episodes = data / "episodes"
    _write_episode(episodes / EP)
    db = str(data / "studio.db")
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("EPISODES_DIR", str(episodes))
    monkeypatch.setenv("DATABASE_PATH", db)
    monkeypatch.setenv("DRY_RUN", "true")
    for name in ("HFVG_EPISODES_DIR", "SHOTS_JSON_PATH", "REFS_JSON_PATH", "ELEVENLABS_OUTPUT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return Hosted(cwd, data, episodes, db)


@pytest.mark.asyncio
async def test_preflight_reads_the_episode_from_episodes_dir(hosted):
    from hfvg.activities.preflight import PreflightError, preflight_validate_episode_activity
    try:
        result = await ActivityEnvironment().run(preflight_validate_episode_activity, EP)
    except PreflightError as e:
        result = {"status": "failed", "problems": e.problems}
    assert result["status"] == "passed", result
    assert result["validated_shots"] == 2 and result["object_contact"] == {"A01": "tag"}, result
    hosted.assert_cwd_clean()


@pytest.mark.asyncio
async def test_save_asset_lands_in_the_data_dir_outputs_the_api_reads(hosted):
    from hfvg.activities.save_assets import save_asset_locally
    from hfvg.still_selection import outputs_dir, still_file
    src = hosted.data / "incoming" / "still.jpg"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\xff\xd8\xff\xe0 still v2")
    local = await ActivityEnvironment().run(save_asset_locally, EP, "A01", str(src), "still", 2)
    expected = hosted.data / "episodes" / EP / "outputs" / "A01" / "still_v2.jpg"
    assert Path(local) == expected, local
    assert expected.read_bytes() == src.read_bytes()
    assert still_file(outputs_dir(EP, "A01"), 2) == expected  # the G2 still lookup finds it
    hosted.assert_cwd_clean()


@pytest.mark.asyncio
async def test_mute_edit_reads_shot_metadata_from_episodes_dir(hosted, monkeypatch):
    """GAP-76 strict mode: A01 is tagged object_contact only in <EPISODES_DIR>/ep42/shots.json, so the
    0.75x retime is refused before any render; without that file the edit would go on to ffmpeg."""
    from hfvg.activities.media import assemble_mute_edit
    monkeypatch.setenv("DRY_RUN", "false")
    clip = hosted.data / "clips" / "A01.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"locked clip bytes for A01")
    sha = hashlib.sha256(clip.read_bytes()).hexdigest()
    (hosted.episode / "edit_plan.json").write_text(json.dumps({
        "order": ["A01"], "slow_action": "refuse",
        "shot_ops": {"A01": {"sha256": sha, "reason": "slow the cup beat", "segments": [[0.0, 3.0]],
                             "interp": "rife"}},
    }), encoding="utf-8")
    shots = [{"shot_id": "A01", "duration": 4.0, "trim_in": 0.0, "trim_out": 4.0}]
    try:
        await ActivityEnvironment().run(assemble_mute_edit, EP, shots, {"A01": str(clip)})
        outcome = "assembled"
    except Exception as e:  # noqa: BLE001 - any other outcome means the shots.json metadata was not read
        outcome = f"{type(e).__name__}: {e}"
    assert "A01: object-contact shot at 0.750x" in outcome and "re-take at 1.0x" in outcome, outcome
    hosted.assert_cwd_clean()


@pytest.mark.asyncio
async def test_ref_generation_dry_run_scratch_is_under_data_dir(hosted):
    from hfvg.activities.ref_generation import generate_ref_activity, generate_ref_edit_activity
    from hfvg.credit_ledger import CreditLedger
    from hfvg.studio_db import create_episode, init_studio_db
    await init_studio_db(hosted.db)
    await create_episode(hosted.db, EP)
    ledger = CreditLedger(hosted.db)
    await ledger.init_db()
    await ledger.create_episode_line(EP, cap_credits=100)
    env = ActivityEnvironment()
    env.info = dataclasses.replace(env.info, workflow_id="ep42-run")
    scratch = hosted.data / "dry_run_outputs" / "ep42-run" / "refs"

    real = hosted.episode / "refs" / "table.png"
    r = await env.run(generate_ref_activity, EP, "r_table", "a wooden table", str(real), 1)
    assert r["status"] == "completed" and (scratch / "table.png").is_file(), (r, sorted(hosted.data.rglob("*")))
    assert not real.exists()

    edited = hosted.episode / "refs" / "duck_wet.png"
    e = await env.run(generate_ref_edit_activity, EP, "r_duck_wet", "the duck, wet", str(edited), 1,
                      str(hosted.episode / "refs" / "duck.png"))
    assert e["status"] == "completed" and (scratch / "duck_wet.png").is_file(), e
    assert not edited.exists()
    hosted.assert_cwd_clean()


@pytest.mark.asyncio
async def test_reconcile_lands_late_assets_under_episodes_dir(hosted, monkeypatch, tmp_path):
    from hfvg.activities import reconcile
    assert reconcile._output_root() == hosted.episodes
    src = hosted.data / "late-src.png"
    Image.new("RGB", (8, 8), (10, 20, 30)).save(src)
    job = {"job_id": "still-ep42-A01-v3", "episode_id": EP, "shot_id": "A01", "job_type": "still"}
    r = await ActivityEnvironment().run(reconcile.land_late_asset, job, str(src))
    landed = hosted.episode / "outputs" / "A01" / "late" / "still-ep42-A01-v3.png"
    assert r.get("landed") is True and r.get("local_path") == str(landed) and landed.is_file(), r
    hosted.assert_cwd_clean()

    override = tmp_path / "late-root"
    monkeypatch.setenv("HFVG_EPISODES_DIR", str(override))
    assert reconcile._output_root() == override  # the explicit override still wins


@pytest.mark.asyncio
async def test_stills_written_by_the_worker_are_served_by_the_api_from_another_cwd(hosted, monkeypatch):
    """The worker (cwd /app) records where it wrote a still as still_url; the API (cwd /app/api) must
    resolve the same outputs and dry-run dirs to serve it at G2."""
    from hfvg.activities.save_assets import save_asset_locally
    from hfvg.activities.studio_generation import poll_job_status
    from hfvg.studio_db import create_episode, init_studio_db
    await init_studio_db(hosted.db)
    await create_episode(hosted.db, EP)
    env = ActivityEnvironment()
    polled = await env.run(poll_job_status, "still-ep42-A02-dry", "still", EP)
    placeholder = hosted.data / "dry_run_outputs" / "still-ep42-A02-dry.jpg"
    assert polled["status"] == "completed" and polled["output_url"] == str(placeholder), polled
    saved = await env.run(save_asset_locally, EP, "A01", polled["output_url"], "still", 1)
    assert saved == str(hosted.data / "episodes" / EP / "outputs" / "A01" / "still_v1.jpg"), saved
    hosted.assert_cwd_clean()

    stills = {"A01": saved, "A02": polled["output_url"]}
    async with aiosqlite.connect(hosted.db) as db:
        for shot_id, still_url in stills.items():
            await db.execute(
                "INSERT INTO shots (id, episode_id, shot_id, status, still_url, qc_results) VALUES (?,?,?,?,?,?)",
                (f"{EP}-{shot_id}", EP, shot_id, "still_ready", still_url,
                 json.dumps({"still_qc": {"pre_approved": shot_id == "A01"}})))
        await db.commit()
    api_cwd = hosted.cwd / "api"
    api_cwd.mkdir()
    monkeypatch.chdir(api_cwd)
    monkeypatch.setenv("ADMIN_SECRET", "h" * 40)
    sys.modules.pop("api.main", None)
    try:
        from fastapi.testclient import TestClient
        from api.main import app
        client = TestClient(app)
        cookies = client.post("/api/login", json={"admin_secret": "h" * 40}).cookies
        served = {s: client.get(f"/api/episodes/{EP}/shots/{s}/still", cookies=cookies) for s in stills}
    finally:
        sys.modules.pop("api.main", None)
    assert {s: r.status_code for s, r in served.items()} == {"A01": 200, "A02": 200}
    assert all(r.content == Path(stills[s]).read_bytes() for s, r in served.items())
    assert list(api_cwd.rglob("*")) == []


@pytest.mark.asyncio
async def test_run_report_and_story_shape_cache_live_under_data_dir(hosted):
    from hfvg.activities.run_report import generate_run_report
    from hfvg.activities.story_shaping import _get_cache_db_path
    report = await ActivityEnvironment().run(
        generate_run_report, EP, False, "2026-11-11T10:00:00+00:00", "2026-11-11T10:05:00+00:00",
        0, 100, False, 2, [])
    reports = hosted.data / "reports"
    assert {Path(p).parent for p in report.values()} == {reports}, report
    assert all(Path(p).is_file() for p in report.values())
    assert _get_cache_db_path() == hosted.data / "story_shape_cache.db"
    hosted.assert_cwd_clean()
