"""GAP-38: codec TP guard heartbeat to prevent activity timeout.

The codec TP guard runs up to 16 limiter tries (several ffmpeg passes each) and used to
send no heartbeat, so mix_and_deliver (heartbeat_timeout 2 min) was cancelled mid-guard on ep05v2
g3r2. Every guard pass must heartbeat."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from hfvg.audio_mix import codec_safe_pcm


@pytest.mark.asyncio
async def test_codec_guard_heartbeats_on_initial_pass(monkeypatch):
    """codec_safe_pcm heartbeats on the initial try."""
    heartbeat_calls = []
    
    async def mock_run_cmd(cmd, what="", *args, **kwargs):
        return "", ""
    
    async def mock_measure(path, target=None):
        # First measure (mix_wav): TP under ceiling, passes immediately
        return {
            "true_peak_dbtp": -2.5,  # Under -2.0 ceiling
            "integrated_lufs": -12.6,
            "lra": 8.0,
        }
    
    monkeypatch.setattr("hfvg.audio_mix.run_cmd", mock_run_cmd)
    monkeypatch.setattr("hfvg.audio_mix.measure_loudness", mock_measure)
    
    def beat(data):
        heartbeat_calls.append(data)
    
    loud = {"tp_ceiling_dbtp": -2.0, "target_lufs": -12.6, "tolerance_lu": 0.5}
    result_path, info = await codec_safe_pcm("/tmp/mix.wav", 192, loud, "/tmp/work", "test", heartbeat=beat)
    
    # Should heartbeat on try 0 (initial pass)
    assert len(heartbeat_calls) >= 1
    assert heartbeat_calls[0] == {"stage": "codec_tp_guard", "tag": "test", "try": 0}
    assert info["ok"] is True


@pytest.mark.asyncio
async def test_codec_guard_heartbeats_on_limiter_tries(monkeypatch):
    """codec_safe_pcm heartbeats on every limiter try when initial pass fails."""
    heartbeat_calls = []
    measure_count = {"count": 0}
    
    async def mock_run_cmd(cmd, what="", *args, **kwargs):
        return "", ""
    
    async def mock_measure(path, target=None):
        measure_count["count"] += 1
        # First measure (encoded from mix_wav): TP over ceiling (triggers limiter)
        if measure_count["count"] == 1:
            return {
                "true_peak_dbtp": -1.0,  # Over -2.0 ceiling
                "integrated_lufs": -12.6,
                "lra": 8.0,
            }
        # Second measure (PCM mix loudness for limiter planning)
        elif measure_count["count"] == 2:
            return {
                "true_peak_dbtp": -1.5,
                "integrated_lufs": -12.6,
                "lra": 8.0,
            }
        # Third+ measures (limited candidates): gradually improve until pass
        elif measure_count["count"] == 3:
            return {"true_peak_dbtp": -11.0, "integrated_lufs": -12.5, "lra": 8.0}
        elif measure_count["count"] == 4:
            return {"true_peak_dbtp": -1.8, "integrated_lufs": -12.7, "lra": 8.0}
        else:
            # Fourth limiter pass: succeeds
            return {
                "true_peak_dbtp": -2.2,  # Under ceiling
                "integrated_lufs": -12.6,
                "lra": 8.0,
            }
    
    monkeypatch.setattr("hfvg.audio_mix.run_cmd", mock_run_cmd)
    monkeypatch.setattr("hfvg.audio_mix.measure_loudness", mock_measure)
    
    def beat(data):
        heartbeat_calls.append(data)
    
    loud = {"tp_ceiling_dbtp": -2.0, "target_lufs": -12.6, "tolerance_lu": 0.5}
    result_path, info = await codec_safe_pcm("/tmp/mix.wav", 192, loud, "/tmp/work", "test", heartbeat=beat)
    
    # Should heartbeat on try 0, then on every limiter try (1, 2, 3, ...)
    assert len(heartbeat_calls) >= 3
    assert heartbeat_calls[0] == {"stage": "codec_tp_guard", "tag": "test", "try": 0}
    # Subsequent calls have try numbers incremented
    for i, call in enumerate(heartbeat_calls[1:], start=1):
        assert call["stage"] == "codec_tp_guard"
        assert call["tag"] == "test"
        assert call["try"] == i
        assert "aim_lufs" in call
        assert "step_db" in call


@pytest.mark.asyncio
async def test_codec_guard_works_without_heartbeat(monkeypatch):
    """codec_safe_pcm works when heartbeat is None (backwards compat)."""
    async def mock_run_cmd(cmd, what="", *args, **kwargs):
        return "", ""
    
    async def mock_measure(path, target=None):
        return {
            "true_peak_dbtp": -2.5,
            "integrated_lufs": -12.6,
            "lra": 8.0,
        }
    
    monkeypatch.setattr("hfvg.audio_mix.run_cmd", mock_run_cmd)
    monkeypatch.setattr("hfvg.audio_mix.measure_loudness", mock_measure)
    
    loud = {"tp_ceiling_dbtp": -2.0, "target_lufs": -12.6, "tolerance_lu": 0.5}
    # Should not raise even with heartbeat=None
    result_path, info = await codec_safe_pcm("/tmp/mix.wav", 192, loud, "/tmp/work", "test", heartbeat=None)
    assert info["ok"] is True
