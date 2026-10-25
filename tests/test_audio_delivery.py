"""Tests for audio generation and delivery activities."""

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from hfvg.activities.audio import generate_voiceover, MIN_VO_GAP_S
from hfvg.activities.media import mix_audio_final


def generate_test_tone(path: Path, duration: float, freq: int = 440, sample_rate: int = 48000):
    """Generate a test tone using ffmpeg (no network, no paid calls)."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"sine=frequency={freq}:duration={duration}:sample_rate={sample_rate}",
        "-ac", "2", "-c:a", "pcm_s16le", str(path)
    ], check=True, capture_output=True)


def generate_test_video(path: Path, duration: float, width: int = 1080, height: int = 1920, fps: int = 30):
    """Generate a test video using ffmpeg."""
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i",
        f"color=c=blue:s={width}x{height}:r={fps}:d={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(path)
    ], check=True, capture_output=True)


@pytest.mark.asyncio
async def test_generate_voiceover_dry_run():
    """Test VO generation in dry-run mode."""
    result = await generate_voiceover(
        episode_id="test_ep",
        script="This is a test voiceover script.",
        voice_id="test_voice_id",
        model="eleven_v3"
    )
    
    assert result.startswith("file://")
    assert "dry" in result


@pytest.mark.asyncio
async def test_generate_voiceover_locked_path():
    """Test using a pre-rendered locked VO file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        # Create a locked VO file
        locked_vo = tmp / "locked_vo.wav"
        generate_test_tone(locked_vo, 5.0, freq=220)
        
        result = await generate_voiceover(
            episode_id="test_ep",
            locked_audio_path=str(locked_vo)
        )
        
        assert result.startswith("file://")
        result_path = Path(result.replace("file://", ""))
        assert result_path.is_file()


@pytest.mark.asyncio
async def test_mix_audio_final_basic():
    """Test basic audio mix with synthetic inputs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        # Create test video (5 seconds)
        mute_video = tmp / "mute.mp4"
        generate_test_video(mute_video, 5.0)
        
        # Create test VO lines (with proper gaps)
        vo1 = tmp / "vo1.wav"
        vo2 = tmp / "vo2.wav"
        generate_test_tone(vo1, 1.0, freq=440)
        generate_test_tone(vo2, 1.0, freq=880)
        
        vo_lines = [
            {"path": str(vo1), "in": 1.0, "dur": 1.0, "gain_db": 0.0},
            {"path": str(vo2), "in": 3.0, "dur": 1.0, "gain_db": 0.0},  # 1.0s gap (> 0.3s min)
        ]
        
        # Run mix
        result = await mix_audio_final(
            episode_id="test_ep",
            mute_video_path=str(mute_video),
            vo_lines=vo_lines,
            bgm_config=None,
            sfx_cues=[],
            vox_calls=[],
            subtitle_script=[],
            output_dir=str(tmp / "delivery")
        )
        
        # Check outputs exist
        assert Path(result["master_path"]).is_file()
        assert Path(result["nosubs_path"]).is_file()
        assert Path(result["phone_path"]).is_file()
        assert Path(result["subs_path"]).is_file()
        
        # Check QC
        qc = result["qc"]
        assert "loudness_lufs" in qc
        assert "true_peak_dbtp" in qc
        assert "duration" in qc
        assert abs(qc["duration"] - 5.0) < 0.1  # Duration should match mute video


@pytest.mark.asyncio
async def test_mix_audio_vo_gap_validation():
    """Test that VO gap validation catches gaps that are too small."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        mute_video = tmp / "mute.mp4"
        generate_test_video(mute_video, 5.0)
        
        vo1 = tmp / "vo1.wav"
        vo2 = tmp / "vo2.wav"
        generate_test_tone(vo1, 1.0, freq=440)
        generate_test_tone(vo2, 1.0, freq=880)
        
        # Gap of 0.2s is too small (< 0.3s minimum)
        vo_lines = [
            {"path": str(vo1), "in": 1.0, "dur": 1.0, "gain_db": 0.0},
            {"path": str(vo2), "in": 2.2, "dur": 1.0, "gain_db": 0.0},  # Only 0.2s gap
        ]
        
        # Should raise an error
        with pytest.raises(Exception) as exc_info:
            await mix_audio_final(
                episode_id="test_ep",
                mute_video_path=str(mute_video),
                vo_lines=vo_lines,
                bgm_config=None,
                sfx_cues=[],
                vox_calls=[],
                subtitle_script=[],
                output_dir=str(tmp / "delivery")
            )
        
        assert "gap too small" in str(exc_info.value).lower() or "0.3" in str(exc_info.value)


@pytest.mark.asyncio
async def test_mix_audio_idempotent_reuse():
    """Test that mix_audio_final reuses existing outputs when input key matches."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        mute_video = tmp / "mute.mp4"
        generate_test_video(mute_video, 3.0)
        
        vo1 = tmp / "vo1.wav"
        generate_test_tone(vo1, 0.5, freq=440)
        
        vo_lines = [{"path": str(vo1), "in": 1.0, "dur": 0.5, "gain_db": 0.0}]
        
        # First run
        result1 = await mix_audio_final(
            episode_id="test_ep",
            mute_video_path=str(mute_video),
            vo_lines=vo_lines,
            output_dir=str(tmp / "delivery")
        )
        
        assert result1["reused"] is False
        
        # Second run with same inputs should reuse
        result2 = await mix_audio_final(
            episode_id="test_ep",
            mute_video_path=str(mute_video),
            vo_lines=vo_lines,
            output_dir=str(tmp / "delivery")
        )
        
        assert result2["reused"] is True
        assert result2["master_path"] == result1["master_path"]


@pytest.mark.asyncio
async def test_mix_audio_with_subtitles():
    """Test subtitle generation and burning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        mute_video = tmp / "mute.mp4"
        generate_test_video(mute_video, 5.0)
        
        vo1 = tmp / "vo1.wav"
        generate_test_tone(vo1, 1.0, freq=440)
        
        vo_lines = [{"path": str(vo1), "in": 1.0, "dur": 1.0, "gain_db": 0.0}]
        
        subtitle_script = [
            {"in": 1.0, "out": 2.0, "text": "This is a test subtitle."}
        ]
        
        result = await mix_audio_final(
            episode_id="test_ep",
            mute_video_path=str(mute_video),
            vo_lines=vo_lines,
            subtitle_script=subtitle_script,
            output_dir=str(tmp / "delivery")
        )
        
        # Check subtitle files exist
        subs_path = Path(result["subs_path"])
        assert subs_path.is_file()
        assert subs_path.suffix == ".ass"
        
        # Check subtitle content
        content = subs_path.read_text()
        assert "test subtitle" in content.lower()
        assert "Dialogue:" in content


@pytest.mark.asyncio
async def test_mix_audio_phone_encode_size_cap():
    """Test that phone encode stays under size limit."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        
        # Create a longer video to test size capping
        mute_video = tmp / "mute.mp4"
        generate_test_video(mute_video, 10.0)
        
        result = await mix_audio_final(
            episode_id="test_ep",
            mute_video_path=str(mute_video),
            vo_lines=[],
            output_dir=str(tmp / "delivery")
        )
        
        phone_path = Path(result["phone_path"])
        assert phone_path.is_file()
        
        # Check size is reasonable
        size_mb = phone_path.stat().st_size / (1024 * 1024)
        assert size_mb < 50  # Should be under 50 MB limit


def test_vo_gap_constant():
    """Test that MIN_VO_GAP_S is set correctly."""
    assert MIN_VO_GAP_S >= 0.3
    assert MIN_VO_GAP_S <= 0.4
