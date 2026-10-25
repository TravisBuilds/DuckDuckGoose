"""Round 7 B1 (G2 display): a pre-approved still is a local file (never uploaded until its clip is
submitted); the authenticated still route serves it, and only files inside the episode's
outputs/<shot>/ directory."""

import json
import sys

import aiosqlite
import pytest

from hfvg.studio_db import create_episode, init_studio_db


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "s.db")
    await init_studio_db(db_path)
    await create_episode(db_path, "ep80")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ADMIN_SECRET", "q" * 40)
    sys.modules.pop("api.main", None)
    from fastapi.testclient import TestClient
    from api.main import app
    client = TestClient(app)
    yield tmp_path, db_path, client
    sys.modules.pop("api.main", None)


async def _row(db_path, shot_id, still_url, pre_approved=True):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO shots (id, episode_id, shot_id, status, still_url, qc_results) VALUES (?,?,?,?,?,?)",
            (f"ep80-{shot_id}", "ep80", shot_id, "still_ready", still_url,
             json.dumps({"still_qc": {"pre_approved": pre_approved}})))
        await db.commit()


@pytest.mark.asyncio
async def test_still_route_serves_episode_outputs_only_behind_auth(env):
    tmp_path, db_path, client = env
    good = tmp_path / "data" / "episodes" / "ep80" / "outputs" / "A01" / "still_v1.jpg"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    await _row(db_path, "A01", str(good))
    url = "/api/episodes/ep80/shots/A01/still"
    assert client.get(url).status_code in (401, 403)
    cookies = client.post("/api/login", json={"admin_secret": "q" * 40}).cookies
    r = client.get(url, cookies=cookies)
    assert r.status_code == 200 and r.content == good.read_bytes()
    assert r.headers["content-type"] == "image/jpeg"

    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"x")
    await _row(db_path, "A02", str(outside))
    assert client.get("/api/episodes/ep80/shots/A02/still", cookies=cookies).status_code == 404
    other_shot = tmp_path / "data" / "episodes" / "ep80" / "outputs" / "A01" / "still_v1.jpg"
    await _row(db_path, "A03", str(other_shot))  # another shot's directory
    assert client.get("/api/episodes/ep80/shots/A03/still", cookies=cookies).status_code == 404
    traversal = str(tmp_path / "data" / "episodes" / "ep80" / "outputs" / "A04" / ".." / ".." / ".." / ".." / "secret.jpg")
    await _row(db_path, "A04", traversal)
    assert client.get("/api/episodes/ep80/shots/A04/still", cookies=cookies).status_code == 404
    txt = tmp_path / "data" / "episodes" / "ep80" / "outputs" / "A05" / "notes.txt"
    txt.parent.mkdir(parents=True)
    txt.write_text("x")
    await _row(db_path, "A05", str(txt))
    assert client.get("/api/episodes/ep80/shots/A05/still", cookies=cookies).status_code == 404
    await _row(db_path, "A06", "https://cdn.example.com/s.jpg")
    assert client.get("/api/episodes/ep80/shots/A06/still", cookies=cookies).status_code == 404
    assert client.get("/api/episodes/ep80/shots/A0%2E%2E/still", cookies=cookies).status_code in (400, 404)


@pytest.mark.asyncio
async def test_still_route_serves_dry_placeholder_stills_from_the_run_dir(env, monkeypatch):
    """A dry-run generated still is stored as a path relative to the run dir
    (data/dry_run_outputs/still-dry-*.jpg); G2 must be able to show it like a pre-approved one,
    and only image files inside the dry-run output dir are served."""
    tmp_path, db_path, client = env
    monkeypatch.chdir(tmp_path)  # the worker / API run from the run dir
    dry = tmp_path / "data" / "dry_run_outputs" / "still-dry-abc123.jpg"
    dry.parent.mkdir(parents=True)
    dry.write_bytes(b"\xff\xd8\xff\xe0dryjpeg")
    await _row(db_path, "A03", "data/dry_run_outputs/still-dry-abc123.jpg", pre_approved=False)
    cookies = client.post("/api/login", json={"admin_secret": "q" * 40}).cookies
    r = client.get("/api/episodes/ep80/shots/A03/still", cookies=cookies)
    assert r.status_code == 200, r.status_code
    assert r.content == dry.read_bytes() and r.headers["content-type"] == "image/jpeg"

    clip = tmp_path / "data" / "dry_run_outputs" / "clip-dry-abc123.mp4"
    clip.write_bytes(b"x")
    await _row(db_path, "A04", "data/dry_run_outputs/clip-dry-abc123.mp4", pre_approved=False)
    assert client.get("/api/episodes/ep80/shots/A04/still", cookies=cookies).status_code == 404
    (tmp_path / "data" / "elsewhere.jpg").write_bytes(b"x")
    await _row(db_path, "A05", "data/dry_run_outputs/../elsewhere.jpg", pre_approved=False)
    assert client.get("/api/episodes/ep80/shots/A05/still", cookies=cookies).status_code == 404
