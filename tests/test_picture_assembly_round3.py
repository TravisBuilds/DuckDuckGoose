"""Tests for Round 3: Picture assembly, motion QC, and clip prompts."""

import json
import os
import pytest
import subprocess
import tempfile
from pathlib import Path

from hfvg.activities.media import assemble_mute_edit
from hfvg.activities.qc import check_assembly_qc
from hfvg.qc.motion import check_motion, MotionCheckResult, _run_stillgate_analysis
from hfvg.qc.animal_count import DuckPresenceCheck, FakeAnimalCounter
from hfvg.shot_data import parse_shots


# Skip if ffmpeg not available
pytestmark = pytest.mark.skipif(
    os.system("which ffmpeg > /dev/null 2>&1") != 0,
    reason="ffmpeg required"
)


def generate_test_video(output_path: str, duration: float, pattern: str = "testsrc", fps: int = 30):
    """Generate a test video using ffmpeg."""
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"{pattern}=size=1080x1920:rate={fps}:duration={duration}",
        "-pix_fmt", "yuv420p",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-an",
        output_path
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.decode()}")


@pytest.mark.asyncio
async def test_assemble_mute_edit_basic():
    """Test: Basic mute assembly from multiple clips."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Generate 3 test clips
        clip1 = tmpdir / "A01.mp4"
        clip2 = tmpdir / "A02.mp4"
        clip3 = tmpdir / "A03.mp4"
        
        generate_test_video(str(clip1), 3.0)
        generate_test_video(str(clip2), 4.0)
        generate_test_video(str(clip3), 2.0)
        
        shots = [
            {"shot_id": "A01", "duration": 3.0},
            {"shot_id": "A02", "duration": 4.0},
            {"shot_id": "A03", "duration": 2.0},
        ]
        
        clip_paths = {
            "A01": str(clip1),
            "A02": str(clip2),
            "A03": str(clip3),
        }
        
        # Run assembly
        result = await assemble_mute_edit(
            "test-episode",
            shots,
            clip_paths,
            str(tmpdir / "output")
        )
        
        # Verify result
        assert result["assembly_path"]
        assert Path(result["assembly_path"]).exists()
        assert result["duration"] == pytest.approx(9.0, abs=0.1)
        assert len(result["manifest"]["clips"]) == 3
        assert result["manifest"]["format"]["width"] == 1080
        assert result["manifest"]["format"]["height"] == 1920
        assert result["manifest"]["format"]["fps"] == 30


@pytest.mark.asyncio
async def test_assemble_mute_edit_with_trimming():
    """Test: Assembly with per-shot trimming."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Generate a 5-second clip
        clip1 = tmpdir / "A01.mp4"
        generate_test_video(str(clip1), 5.0)
        
        shots = [
            {
                "shot_id": "A01",
                "duration": 5.0,
                "trim_in": 1.0,  # Trim first second
                "trim_out": 4.0,  # Trim last second
            },
        ]
        
        clip_paths = {"A01": str(clip1)}
        
        # Run assembly
        result = await assemble_mute_edit(
            "test-episode",
            shots,
            clip_paths,
            str(tmpdir / "output")
        )
        
        # Verify trimming
        assert result["duration"] == pytest.approx(3.0, abs=0.1)
        assert result["manifest"]["clips"][0]["trim_in"] == 1.0
        assert result["manifest"]["clips"][0]["trim_out"] == 4.0


@pytest.mark.asyncio
async def test_assemble_mute_edit_idempotent():
    """Test: Assembly is idempotent (returns existing file)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        clip1 = tmpdir / "A01.mp4"
        generate_test_video(str(clip1), 2.0)
        
        shots = [{"shot_id": "A01", "duration": 2.0}]
        clip_paths = {"A01": str(clip1)}
        output_dir = str(tmpdir / "output")
        
        # First call
        result1 = await assemble_mute_edit("test-ep", shots, clip_paths, output_dir)
        
        # Second call should return existing file
        result2 = await assemble_mute_edit("test-ep", shots, clip_paths, output_dir)
        
        assert result1["assembly_path"] == result2["assembly_path"]
        assert result1["duration"] == result2["duration"]


def test_motion_check_stillgate_direct():
    """Test: Direct stillgate motion analysis."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Generate test video with motion
        video = tmpdir / "test.mp4"
        generate_test_video(str(video), 5.0, "testsrc")
        
        manifest = {
            "clips": [
                {"id": "A01", "seg_s": 5.0}
            ],
            "dissolves": {}
        }
        
        # Run motion check
        results = check_motion(str(video), manifest)
        
        assert len(results) == 1
        assert results[0].shot_id == "A01"
        assert results[0].mean_fd > 0
        assert results[0].peak_fd > 0
        assert results[0].verdict in ["PASS", "WATCH", "FAIL"]


def test_motion_check_frozen_video():
    """Test: Frozen (still-only) video fails motion check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Generate frozen video (1 fps = static frames)
        video = tmpdir / "frozen.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "color=black:size=180x320:rate=1:duration=3",
            "-pix_fmt", "yuv420p",
            video
        ]
        subprocess.run(cmd, capture_output=True, check=True, timeout=10)
        
        manifest = {
            "clips": [{"id": "FROZEN", "seg_s": 3.0}],
            "dissolves": {}
        }
        
        results = check_motion(str(video), manifest)
        
        assert len(results) == 1
        assert results[0].verdict == "FAIL"  # Still-only should fail


@pytest.mark.asyncio
async def test_assembly_qc_integration():
    """Test: Assembly QC integrates motion check."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Generate test assembly
        assembly = tmpdir / "assembly.mp4"
        generate_test_video(str(assembly), 6.0)
        
        manifest = {
            "clips": [
                {"id": "A01", "seg_s": 3.0},
                {"id": "A02", "seg_s": 3.0},
            ],
            "dissolves": {}
        }
        
        # Run assembly QC
        result = await check_assembly_qc(
            str(assembly),
            manifest,
            "test-episode"
        )
        
        assert "verdict" in result
        assert result["verdict"] in ["PASS", "WATCH", "FAIL"]
        assert "motion_results" in result
        assert len(result["motion_results"]) == 2


def test_shot_data_clip_prompt_parsing():
    """Test: shot_data.py parses clip_prompt field."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["duck"],
                "wet": False,
                "model": "kling",
                "duration": 5.0,
                "end_frame": False,
                "prompt": "A duck waddling through grass.",
                "clip_prompt": "Ambient motion. Static camera, locked off. Real-time motion, no slow motion.",
                "ref_keys": []
            }
        ]
    }
    
    result = parse_shots(data)
    
    assert "A01" in result
    assert result["A01"]["prompt"] == "A duck waddling through grass."
    assert result["A01"]["clip_prompt"] == "Ambient motion. Static camera, locked off. Real-time motion, no slow motion."


def test_shot_data_clip_prompt_optional():
    """Test: clip_prompt is optional in shots.json."""
    data = {
        "shots": [
            {
                "id": "A01",
                "count": 1,
                "characters": ["duck"],
                "wet": False,
                "model": "kling",
                "duration": 5.0,
                "end_frame": False,
                "prompt": "A duck.",
                "ref_keys": []
            }
        ]
    }
    
    result = parse_shots(data)
    
    assert result["A01"]["clip_prompt"] == ""


@pytest.mark.asyncio
async def test_duck_presence_check_fake_counter():
    """Test: Duck presence check with fake counter."""
    checker = DuckPresenceCheck(FakeAnimalCounter())
    
    # Create a dummy image file
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff\xe0")  # Minimal JPEG header
        temp_path = f.name
    
    try:
        result = await checker.check_still(temp_path, expected_count=1)
        
        assert result["passed"] is True
        assert result["escalate"] is False
        assert result["details"]["count"] == 1
    finally:
        os.unlink(temp_path)


def test_stillgate_analysis_validates_manifest():
    """Test: Stillgate analysis handles malformed manifests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        video = Path(tmpdir) / "test.mp4"
        generate_test_video(str(video), 2.0)
        
        # Empty manifest should not crash
        manifest = {"clips": [], "dissolves": {}}
        
        results = _run_stillgate_analysis(str(video), manifest)
        
        assert isinstance(results, list)
        assert len(results) == 0


def test_motion_check_result_verdicts():
    """Test: MotionCheckResult verdict helpers."""
    pass_result = MotionCheckResult("A01", 0.0, 5.0, 1.5, 10.0, "PASS")
    watch_result = MotionCheckResult("A02", 5.0, 10.0, 0.2, 5.0, "WATCH")
    fail_result = MotionCheckResult("A03", 10.0, 15.0, 0.1, 2.0, "FAIL")
    
    assert pass_result.is_pass()
    assert not pass_result.is_watch()
    assert not pass_result.is_fail()
    
    assert not watch_result.is_pass()
    assert watch_result.is_watch()
    assert not watch_result.is_fail()
    
    assert not fail_result.is_pass()
    assert not fail_result.is_watch()
    assert fail_result.is_fail()
