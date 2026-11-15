"""Blocking cast work runs off the worker's event loop, which runs every activity, their heartbeats
and the aiosqlite transactions of the others: the parse activity's cast planning (lock hashes,
lineup and grid sheets, a SQLite lock another transaction holds), the G2 reference re-check, and
the normalization of a reference picture before its upload."""

from __future__ import annotations

import asyncio
import json
import time

import aiosqlite
import httpx
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from hfvg import cast_plan, cast_store
from hfvg.activities import workflow_support
from hfvg.activities.workflow_support import parse_beatmap_activity, recheck_refs_activity
from hfvg.paths import episode_dir
from tests.cast_helpers import complete_sheet, image_bytes, new_character, upload

EP = "ep43loop"
SLOW_S = 0.8  # what each slowed call blocks its thread for
MAX_PAUSE_S = 0.4  # the loop never waits this long while the slowed work runs in its thread
BEATMAP = (
    "| # | ID | Screen (gen) | Room | TOD | Chars | Action | Purpose | In | Out | Model |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|\n"
    "| 1 | A01 | 2.5 (3.5) | Pier | Morning | P M | Synthetic action | beat | – | – | K |\n"
)


@pytest.fixture(autouse=True)
def cast_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CAST_STORAGE_DIR", str(tmp_path / "private-cast"))
    monkeypatch.setenv("DRY_RUN", "true")
    for var in ("MODEL_PATH_STILL", "MODEL_MAX_REFS_JSON", "SHOTS_JSON_PATH", "REFS_JSON_PATH", "PROMPT_LINT_KIT_PATH"):
        monkeypatch.delenv(var, raising=False)


def _episode(cast: list) -> str:
    """EP's shots.json (shot A01 with this cast), refs.json and BEATMAP.md; returns the beatmap path."""
    folder = episode_dir(EP)
    folder.mkdir(parents=True, exist_ok=True)
    shot = {"id": "A01", "count": 1, "characters": [], "wet": False, "exterior": True, "water": False,
            "model": "kling", "duration": 4, "end_frame": False, "aspect_ratio": "9:16",
            "prompt": "A quiet pier at dawn.", "ref_keys": [], "cast": cast}
    (folder / "shots.json").write_text(json.dumps({"episode_id": EP, "shots": [shot]}))
    (folder / "refs.json").write_text(json.dumps({"episode_id": EP, "per_shot": {"A01": []}}))
    beatmap = folder / "BEATMAP.md"
    beatmap.write_text(BEATMAP)
    return str(beatmap)


def _slowed(fn):
    def slow(*args, **kwargs):
        time.sleep(SLOW_S)
        return fn(*args, **kwargs)
    return slow


async def _longest_pause(awaitable) -> tuple[object, float]:
    """(its result, the longest time the event loop did not run a 10 ms ticker while awaiting it)."""
    pauses: list[float] = []
    done = asyncio.Event()

    async def tick():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            pauses.append(now - last)
            last = now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0)
    try:
        result = await awaitable
    finally:
        done.set()
        await ticker
    return result, max(pauses)


async def test_the_parse_activity_plans_the_cast_off_the_event_loop(monkeypatch):
    """A cast episode's plan verifies the locked sheets' hashes and builds lineup and grid sheets
    (hundreds of ms for a whole episode): in a thread, while the loop keeps running. The plan is the
    one planning directly makes, every time (replays see the same activity result)."""
    pip = new_character(("episode", EP), name="Pip")
    complete_sheet(pip)
    cast_store.approve_version(pip["character_id"], 1, "t")
    upload(new_character(("episode", EP), name="Moss"), seed=2)
    beatmap = _episode(["pip", "moss"])
    verify_lock = cast_store.verify_lock
    monkeypatch.setattr(cast_store, "verify_lock", _slowed(verify_lock))
    out, pause = await _longest_pause(ActivityEnvironment().run(parse_beatmap_activity, beatmap, EP, False))
    assert pause < MAX_PAUSE_S, f"cast planning held up the event loop for {pause:.2f}s"
    shot = out["shots"][0]
    assert [c["character"] for c in shot["shot_meta"]["cast"]] == ["pip", "moss"]
    assert shot["shot_meta"]["cast"][0]["locked"] is True
    assert shot["ref_keys"][0].startswith(cast_plan.COMP_PREFIX)  # the lineup, built in the thread
    monkeypatch.setattr(cast_store, "verify_lock", verify_lock)
    assert out == workflow_support._parse_and_plan(beatmap, EP, False)
    assert await ActivityEnvironment().run(parse_beatmap_activity, beatmap, EP, False) == out


async def test_the_parse_activity_waits_for_a_database_lock_off_the_event_loop():
    """Another activity holds the studio database (an aiosqlite transaction, which the event loop
    commits). Planning waits for that lock in its thread; on the loop it would hold up the very
    commit it waits for until the busy timeout ("database is locked")."""
    image = upload(new_character(("episode", EP), name="Pip"), seed=1)
    beatmap = _episode(["pip"])
    db = await aiosqlite.connect(cast_store.db_path())
    try:
        await db.execute("BEGIN EXCLUSIVE")  # readers wait too (rollback journal)

        async def commit_soon():
            await asyncio.sleep(0.2)
            await db.commit()

        holder = asyncio.create_task(commit_soon())
        out = await asyncio.wait_for(ActivityEnvironment().run(parse_beatmap_activity, beatmap, EP, False), timeout=10)
        await holder
    finally:
        await db.close()
    assert out["shots"][0]["ref_keys"] == [cast_plan.image_key(image)]


async def test_the_ref_recheck_runs_off_the_event_loop(monkeypatch):
    """G2's re-check of which references exist now (cast pictures and sheets too) checks files: in a thread."""
    image = upload(new_character(("episode", EP), name="Pip"), seed=1)
    _episode(["pip"])
    key = cast_plan.image_key(image)
    monkeypatch.setattr(cast_plan, "resolve_cast_refs", _slowed(cast_plan.resolve_cast_refs))
    out, pause = await _longest_pause(ActivityEnvironment().run(recheck_refs_activity, EP, {"A01": [key]}))
    assert pause < MAX_PAUSE_S, f"the reference re-check held up the event loop for {pause:.2f}s"
    assert out == {"A01": {"refs": [str(cast_store.image_path(image))], "missing_refs": []}}


async def test_a_reference_picture_is_normalized_off_the_event_loop(tmp_path, monkeypatch):
    """Every still uploads its references normalized (the character sheets' too): decoding, scaling
    and encoding a large picture, and reading it for the upload, run in a thread."""
    import hfvg.image_normalize as image_normalize
    from hfvg.providers.higgsfield_still import HiggsfieldStillProvider
    source = tmp_path / "pip.png"
    source.write_bytes(image_bytes(seed=3))
    monkeypatch.setattr(image_normalize, "normalize_reference_image", _slowed(image_normalize.normalize_reference_image))
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "test_id:test_secret")
    monkeypatch.setenv("HIGGSFIELD_BASE_URL", "http://mock.api")
    puts = []
    with respx.mock:
        respx.post("http://mock.api/files/generate-upload-url").mock(return_value=httpx.Response(200, json={
            "upload_url": "http://mock.upload/put", "public_url": "http://mock.cdn/pip.jpg",
            "upload_headers": {"Content-Type": "image/jpeg"}}))
        respx.put("http://mock.upload/put").mock(side_effect=lambda r: (puts.append(r.content), httpx.Response(200))[1])
        provider = HiggsfieldStillProvider()
        try:
            result, pause = await _longest_pause(provider.upload_reference(str(source), version=1))
        finally:
            await provider.close()
    assert pause < MAX_PAUSE_S, f"normalizing the reference held up the event loop for {pause:.2f}s"
    assert result == {"public_url": "http://mock.cdn/pip.jpg"}
    assert len(puts) == 1 and puts[0][:3] == b"\xff\xd8\xff"  # the normalized JPEG, not the PNG
