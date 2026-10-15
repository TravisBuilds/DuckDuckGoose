"""Tests for audio stage activities."""

import json
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from hfvg.activities.audio_stage import (
    MixConfig,
    burn_logo_overlay,
    draft_vo_script,
    extract_frame_at_timecode,
    generate_music,
    generate_sfx_library,
    generate_vo_take,
    mix_audio_stems,
    slice_vo_lines,
)
from hfvg.config import config


@pytest.fixture
def temp_video(tmp_path):
    """Create a temporary dummy video file."""
    video_path = tmp_path / "test_video.mp4"
    
    # Create a 5-second silent black video
    import subprocess
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=black:s=1080x1920:d=5",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac",
        "-t", "5",
        str(video_path)
    ]
    
    subprocess.run(cmd, check=True)
    return video_path


@pytest.fixture
def sample_script_lines():
    """Sample VO script lines."""
    return [
        {
            "n": 1,
            "in": 0.4,
            "out": 4.5,
            "jp": "きょうのお客様は……",
            "en": "Today's guests are...",
            "shot": "P1",
            "beat": "arrival"
        },
        {
            "n": 2,
            "in": 8.6,
            "out": 14.3,
            "jp": "お父さまの、バーチさん。",
            "en": "Father, Birch.",
            "shot": "A01",
            "beat": "introduction"
        }
    ]


@pytest.fixture
def sample_cues():
    """Sample SFX cue list."""
    return [
        {
            "t": 0.0,
            "end": 5.0,
            "type": "ambience",
            "description": "morning mountain air"
        },
        {
            "t": 4.0,
            "end": 8.0,
            "type": "event",
            "description": "door opening"
        }
    ]


@pytest.mark.asyncio
async def test_draft_vo_script(tmp_path):
    """Test drafting VO script in dry-run mode."""
    beatmap_path = tmp_path / "beatmap.md"
    beatmap_path.write_text("# Beatmap\n\n## Shot P1\nArrival\n")
    
    shot_list = [
        {"shot_id": "P1", "duration": 4.0},
        {"shot_id": "A01", "duration": 6.0},
    ]
    
    result = await draft_vo_script(
        beatmap_path=str(beatmap_path),
        shot_list=shot_list,
    )
    
    assert "script_json" in result
    assert "frame_extracts" in result
    assert "lines" in result
    assert len(result["lines"]) > 0


@pytest.mark.asyncio
async def test_extract_frame_at_timecode(temp_video, tmp_path):
    """Test frame extraction in dry-run mode."""
    output_path = tmp_path / "frame.png"
    
    result = await extract_frame_at_timecode(
        video_path=str(temp_video),
        timecode=2.5,
        output_path=str(output_path),
    )
    
    assert result == str(output_path)
    assert output_path.exists()


@pytest.mark.asyncio
async def test_generate_vo_take(sample_script_lines):
    """Test VO generation in dry-run mode."""
    result = await generate_vo_take(
        script_lines=sample_script_lines,
    )
    
    assert "take_url" in result
    assert "job_id" in result
    assert "cost_credits" in result
    assert result["cost_credits"] > 0


@pytest.mark.asyncio
async def test_slice_vo_lines(sample_script_lines, tmp_path):
    """Test VO line slicing in dry-run mode."""
    take_path = tmp_path / "take.wav"
    take_path.write_text("dummy wav")
    
    result = await slice_vo_lines(
        take_path=str(take_path),
        script_lines=sample_script_lines,
        output_dir=str(tmp_path / "lines"),
    )
    
    assert "lines" in result
    assert "transcription" in result
    assert len(result["lines"]) == len(sample_script_lines)


@pytest.mark.asyncio
async def test_generate_music(tmp_path):
    """Test music generation in dry-run mode."""
    result = await generate_music(
        duration=115.0,
        style="Ghibli chamber score",
        reuse_library=True,
    )
    
    assert "music_url" in result
    assert "reused" in result
    assert "cost_credits" in result
    
    # Should reuse in dry-run mode
    assert result["reused"] is True
    assert result["cost_credits"] == 0


@pytest.mark.asyncio
async def test_generate_sfx_library(sample_cues):
    """Test SFX generation in dry-run mode."""
    result = await generate_sfx_library(
        cue_list=sample_cues,
    )
    
    assert "cues" in result
    assert "generated" in result
    assert "cost_credits" in result
    assert len(result["cues"]) == len(sample_cues)


@pytest.mark.asyncio
async def test_mix_audio_stems(temp_video, sample_script_lines, sample_cues, tmp_path):
    """Test audio mixing in dry-run mode."""
    # Create dummy stems
    vo_lines = [str(tmp_path / f"line_{i}.wav") for i in range(len(sample_script_lines))]
    for line_path in vo_lines:
        Path(line_path).write_text("dummy")
    
    music_path = tmp_path / "music.mp3"
    music_path.write_text("dummy")
    
    protected_windows = [
        {"start": 95.0, "end": 97.5, "label": "logo fade-in"}
    ]
    
    mix_config_dict = MixConfig().__dict__
    
    result = await mix_audio_stems(
        video_path=str(temp_video),
        vo_lines=vo_lines,
        vo_timings=sample_script_lines,
        music_path=str(music_path),
        sfx_cues=sample_cues,
        protected_windows=protected_windows,
        mix_config=mix_config_dict,
        output_dir=str(tmp_path / "output"),
    )
    
    assert "master_path" in result
    assert "nosubs_path" in result
    assert "subs_path" in result
    assert "phone_path" in result
    assert "stems" in result
    assert "loudness" in result
    assert "assertions_passed" in result
    
    # Check loudness is in expected range
    lufs = result["loudness"]["integrated_lufs"]
    assert -13.5 <= lufs <= -12.0


@pytest.mark.asyncio
async def test_burn_logo_overlay(temp_video, tmp_path):
    """Test logo overlay in dry-run mode."""
    logo_path = tmp_path / "logo.png"
    
    # Create dummy logo (1x1 transparent PNG)
    import subprocess
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", "color=white:s=100x100:d=0.1",
        "-frames:v", "1",
        str(logo_path)
    ]
    subprocess.run(cmd, check=True)
    
    output_path = tmp_path / "with_logo.mp4"
    
    result = await burn_logo_overlay(
        video_path=str(temp_video),
        logo_path=str(logo_path),
        output_path=str(output_path),
    )
    
    assert result == str(output_path)
    assert output_path.exists()


def test_mix_config_dataclass():
    """Test MixConfig dataclass defaults."""
    config = MixConfig()
    
    assert config.vo_voice == "4sirbXwrtRlmPV80MJkQ"
    assert config.vo_model == "eleven_v3"
    assert config.vo_stretch == 0.90
    assert config.target_lufs == -12.7
    assert config.target_peak_dbtp == -1.0
    assert config.vo_tags == ["[softly]", "[gently]", "[warm smile]"]


def test_protected_windows():
    """Test protected window validation logic."""
    # This would be part of mix_audio_stems assertions
    
    lines = [
        {"n": 1, "in": 0.4, "out": 4.5},
        {"n": 2, "in": 95.5, "out": 97.0},  # Overlaps logo fade-in
    ]
    
    protected = [
        {"start": 95.0, "end": 97.5, "label": "logo fade-in"}
    ]
    
    # Line 2 should fail: starts at 95.5, ends at 97.0
    # Logo fade starts at 95.0, so line must end ≤ 95.0
    
    violations = []
    for line in lines:
        for window in protected:
            if not (line["out"] <= window["start"] or line["in"] >= window["end"]):
                violations.append((line["n"], window["label"]))
    
    assert len(violations) > 0
    assert (2, "logo fade-in") in violations


@pytest.mark.asyncio
async def test_audio_stage_integration(temp_video, tmp_path):
    """Integration test: draft script → generate VO → mix."""
    # Step 1: Draft script
    beatmap_path = tmp_path / "beatmap.md"
    beatmap_path.write_text("# Beatmap\n")
    
    shot_list = [{"shot_id": "P1", "duration": 5.0}]
    
    script_result = await draft_vo_script(
        beatmap_path=str(beatmap_path),
        shot_list=shot_list,
    )
    
    lines = script_result["lines"]
    
    # Step 2: Generate VO
    vo_result = await generate_vo_take(script_lines=lines)
    
    # Step 3: Slice lines
    slice_result = await slice_vo_lines(
        take_path=vo_result["take_url"],
        script_lines=lines,
        output_dir=str(tmp_path / "lines"),
    )
    
    # Step 4: Generate music
    music_result = await generate_music(duration=5.0)
    
    # Step 5: Mix
    mix_result = await mix_audio_stems(
        video_path=str(temp_video),
        vo_lines=slice_result["lines"],
        vo_timings=lines,
        music_path=music_result["music_url"],
        sfx_cues=[],
        protected_windows=[],
        mix_config=MixConfig().__dict__,
        output_dir=str(tmp_path / "output"),
    )
    
    # Verify outputs
    assert mix_result["assertions_passed"]
    assert "master_path" in mix_result
    assert "nosubs_path" in mix_result


def test_vo_line_spacing():
    """Test VO line spacing validation (≥0.4s gap between lines)."""
    lines = [
        {"n": 1, "in": 0.4, "out": 4.5},
        {"n": 2, "in": 4.8, "out": 8.0},  # 0.3s gap - too short
        {"n": 3, "in": 8.5, "out": 12.0},  # 0.5s gap - OK
    ]
    
    min_gap = 0.4
    violations = []
    
    for i in range(len(lines) - 1):
        gap = lines[i+1]["in"] - lines[i]["out"]
        if gap < min_gap:
            violations.append((lines[i]["n"], lines[i+1]["n"], round(gap, 2)))
    
    assert len(violations) > 0
    assert (1, 2, 0.3) in violations
