"""User attribution regression: audio jobs record the episode owner's user_id."""

import os
import aiosqlite
import httpx
import pytest
import respx
from temporalio.testing import ActivityEnvironment

from hfvg.activities import audio as act
from hfvg.budget import BudgetLedger


API = "https://api.elevenlabs.io"
MP3 = b"ID3" + b"\x00" * 256


@pytest.fixture
async def live(monkeypatch):
    """Set up live environment for audio activities."""
    from hfvg.studio_db import init_studio_db
    
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    monkeypatch.delenv("ELEVENLABS_USD_PER_CREDIT", raising=False)
    
    db_path = os.environ["DATABASE_PATH"]
    await init_studio_db(db_path)


async def _run(activity_fn, *args):
    env = ActivityEnvironment()
    return await env.run(activity_fn, *args)


def _audio(extra_headers: dict | None = None) -> httpx.Response:
    headers = {"content-type": "audio/mpeg"}
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Response(200, headers=headers, content=MP3)


async def test_audio_jobs_attributed_to_episode_owner(live):
    """Audio activities record the episode owner's user_id on job rows."""
    from hfvg.studio_db import create_episode
    from hfvg.users import UserManager
    
    db_path = os.environ["DATABASE_PATH"]
    
    # Create a test user
    mgr = UserManager(db_path)
    await mgr.init_db()
    test_user = await mgr.create_user(
        email="owner@example.com",
        name="Test Owner",
        password="test123",
        role="member"
    )
    owner_id = test_user["user_id"]
    
    # Create episode owned by test user
    await create_episode(db_path, "ep_attr_test", "./BEATMAP.md", user_id=owner_id)
    
    # Initialize budget for the episode
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.ensure_audio_line("ep_attr_test", "el_tts_final")
    
    # Mock ElevenLabs TTS call
    with respx.mock() as mock:
        mock.post(f"{API}/v1/text-to-speech/voice123/with-timestamps").mock(
            return_value=_audio({"character-cost": "50"})
        )
        
        # Generate voiceover with owner's user_id
        result = await _run(
            act.generate_voiceover,
            "ep_attr_test",
            "Hello world",
            "voice123",
            "eleven_v3",
            "line1",
            None,
            1,
            owner_id  # user_id parameter
        )
    
    # Verify the job was created and attributed correctly
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT user_id, episode_id, status FROM job_ledger WHERE job_id = ?",
            (result["job_id"],)
        ) as cur:
            row = await cur.fetchone()
    
    assert row is not None, "Job row not found"
    assert row[0] == owner_id, f"Expected user_id={owner_id}, got {row[0]}"
    assert row[1] == "ep_attr_test"
    assert row[2] == "committed"


async def test_audio_jobs_fallback_to_legacy_when_no_owner(live):
    """Audio activities fall back to LEGACY_USER_ID for unowned episodes."""
    from hfvg.users import LEGACY_USER_ID
    from hfvg.studio_db import create_episode
    
    db_path = os.environ["DATABASE_PATH"]
    
    # Create episode with no owner
    await create_episode(db_path, "ep_unowned", "./BEATMAP.md", user_id=None)
    
    ledger = BudgetLedger(db_path)
    await ledger.ensure_audio_line("ep_unowned", "el_sfx")
    
    with respx.mock() as mock:
        mock.post(f"{API}/v1/sound-generation").mock(
            return_value=_audio({"character-cost": "100"})
        )
        
        # Generate SFX with no user_id (simulating old workflow or unowned episode)
        result = await _run(
            act.generate_sfx,
            "ep_unowned",
            "door knock",
            3.0,
            "sfx1",
            None,
            None  # user_id=None
        )
    
    # Verify the job fell back to LEGACY_USER_ID
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT user_id FROM job_ledger WHERE job_id = ?",
            (result["job_id"],)
        ) as cur:
            row = await cur.fetchone()
    
    assert row is not None
    assert row[0] == LEGACY_USER_ID, f"Expected LEGACY_USER_ID, got {row[0]}"
