"""Tests for audio generation error handling (mutation coverage)."""

import pytest
from unittest.mock import AsyncMock, patch
from temporalio.exceptions import ApplicationError

from hfvg.providers.elevenlabs import ElevenLabsError


@pytest.mark.asyncio
async def test_elevenlabs_permanent_error_is_non_retryable(tmp_path, monkeypatch):
    """Mutation #393: ElevenLabsError with permanent=True should be non-retryable."""
    from hfvg.activities.audio import generate_voiceover
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    monkeypatch.setenv("SMOKE_TEST_OVERRIDE", "true")
    
    # Initialize database
    from hfvg.studio_db import init_studio_db
    from hfvg.budget import BudgetLedger
    await init_studio_db(db_path)
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_episode_budget("ep01")
    
    # Set manual balance for preflight check
    await ledger.set_manual_balance(100_000_000, set_by="test", note="test balance")
    
    # Ensure audio line exists (el_vo_takes for TTS/voiceover)
    await ledger.ensure_audio_line("ep01", "el_vo_takes")
    
    # Increase cap so we don't hit budget limits
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE budget_lines SET cap_usd_micros = ? WHERE episode_id = ? AND line_name = ?",
            (1_000_000, "ep01", "el_vo_takes")
        )
        await db.commit()
    
    # Create a permanent ElevenLabs error (401 is permanent)
    permanent_error = ElevenLabsError("Invalid API key", status_code=401)
    
    with patch('hfvg.providers.elevenlabs.ElevenLabsProvider.submit_audio', new_callable=AsyncMock) as mock_submit:
        mock_submit.side_effect = permanent_error
        
        # Should raise ApplicationError with non_retryable=True
        with pytest.raises(ApplicationError) as exc_info:
            await generate_voiceover(
                episode_id="ep01",
                script="Test line",
                voice_id="test-voice",
            )
        
        # Verify it's non-retryable
        assert exc_info.value.non_retryable, "Permanent ElevenLabs errors must be non-retryable"
        assert exc_info.value.type == "ElevenLabsPermanentError"
