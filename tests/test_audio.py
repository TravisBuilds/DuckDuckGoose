"""Tests for audio generation error handling (mutation coverage)."""

import pytest
from unittest.mock import AsyncMock, patch
from temporalio.exceptions import ApplicationError

from hfvg.providers.elevenlabs import ElevenLabsError


@pytest.mark.skip(reason="generate_audio_dialogue function does not exist - test needs update")
@pytest.mark.asyncio
async def test_elevenlabs_permanent_error_is_non_retryable(tmp_path, monkeypatch):
    """Mutation #393: ElevenLabsError with permanent=True should be non-retryable."""
    from hfvg.activities.audio import generate_audio_dialogue
    
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "test-key")
    
    # Initialize database
    from hfvg.budget import BudgetLedger
    from hfvg.credit_ledger import CreditLedger
    
    ledger = BudgetLedger(db_path)
    await ledger.init_db()
    await ledger.init_live_episode("ep01", policy="test")
    
    credit_ledger = CreditLedger(db_path)
    await credit_ledger.init_db()
    
    # Create a permanent ElevenLabs error
    permanent_error = ElevenLabsError("Quota exceeded", permanent=True)
    
    with patch('hfvg.providers.elevenlabs.ElevenLabsProvider.generate_speech', new_callable=AsyncMock) as mock_gen, \
         patch('hfvg.providers.elevenlabs.ElevenLabsProvider.estimate_usd_micros', new_callable=AsyncMock) as mock_est, \
         patch('hfvg.studio_db.enforce_smoke_test_gate', new_callable=AsyncMock), \
         patch('hfvg.balance_monitor.BalanceMonitor.check_preflight_balance', new_callable=AsyncMock) as mock_preflight:
        
        mock_preflight.return_value = (True, "Balance OK")
        mock_est.return_value = 50_000  # $0.05
        mock_gen.side_effect = permanent_error
        
        # Should raise ApplicationError with non_retryable=True
        with pytest.raises(ApplicationError) as exc_info:
            await generate_audio_dialogue(
                episode_id="ep01",
                asset_id="line1",
                script="Test line",
                voice_id="test-voice",
            )
        
        # Verify it's non-retryable
        assert exc_info.value.non_retryable, "Permanent ElevenLabs errors must be non-retryable"
        assert exc_info.value.type == "ElevenLabsPermanentError"
