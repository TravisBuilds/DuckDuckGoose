"""
Shared audio processing utilities for both CLI and activities.

This module contains the core audio mixing, encoding, and measurement
functions used by both the parity CLI and the Temporal activities.
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


def get_video_duration(video_path: Path) -> float:
    """Get video duration using ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        str(video_path)
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())


def rebuild_mix_stems(
    video_path: Path,
    stems: dict[str, Path | None],
    output_path: Path,
    duration: float,
) -> None:
    """
    Rebuild mix from stems using ffmpeg.
    
    This implements the proven Ep03/Ep04 mix chain exactly:
    1. Load all 4 stems: SFX, VOX, VO, BGM (in that order)
    2. Apply VO -2.5 dB gain trim
    3. amix with dropout_transition=1, normalize=0
    4. Master makeup +4.7 dB
    5. alimiter at 0.86 (≈-1.3 dBFS)
    6. Additional limiter pass to ensure true peak ≤ -1.0 dBTP
    7. apad + atrim to exact duration
    """
    # Ordered stem inputs: SFX, VOX, VO, BGM
    stem_order = ["sfx", "vox", "vo", "bgm"]
    inputs = []
    stem_indices = []
    
    for stem_name in stem_order:
        stem_path = stems.get(stem_name)
        if stem_path:
            inputs.extend(["-i", str(stem_path)])
            stem_indices.append(len(inputs) // 2 - 1)
        else:
            # Missing optional stem (e.g., VOX) - use silent audio
            inputs.extend(["-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=stereo"])
            stem_indices.append(len(inputs) // 2 - 1)
    
    # Add video
    video_idx = len(inputs) // 2
    inputs.extend(["-i", str(video_path)])
    
    # Build filter chain matching reference, with additional limiter for -1.0 dBTP target
    def A(idx, label):
        return f"[{idx}:a]aformat=sample_rates=48000:channel_layouts=stereo,apad=whole_dur={duration:.3f},atrim=0:{duration:.3f}[{label}];"
    
    filter_parts = []
    filter_parts.append(A(stem_indices[0], "s"))   # SFX
    filter_parts.append(A(stem_indices[1], "x"))   # VOX
    filter_parts.append(A(stem_indices[2], "vo"))  # VO
    filter_parts.append(A(stem_indices[3], "bgm")) # BGM
    
    # VO gain trim -2.5 dB, then amix all 4 stems, with additional headroom limiter for true peak
    filter_parts.append(
        f"[vo]volume=-2.5dB[vog];"
        f"[s][x][vog][bgm]amix=inputs=4:duration=longest:dropout_transition=1:normalize=0,"
        f"volume=4.7dB,"
        f"alimiter=limit=0.86:attack=1:release=60:level=disabled,"
        f"alimiter=limit=0.79:attack=1:release=60:level=disabled,"  # Additional pass for -1.0 dBTP
        f"apad=whole_dur={duration:.3f},"
        f"atrim=0:{duration:.3f},"
        f"asetpts=PTS-STARTPTS[a]"
    )
    
    filter_complex = "".join(filter_parts)
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", f"{video_idx}:v:0",  # Video
        "-map", "[a]",  # Mixed audio
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-t", f"{duration:.6f}",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    subprocess.run(cmd, check=True)


def burn_ass_subtitles(
    video_path: Path,
    ass_path: Path,
    fonts_dir: Path,
    output_path: Path,
) -> None:
    """
    Burn ASS subtitles onto video using libx264.
    
    Matches reference: fontsdir, libx264 crf18 fast preset.
    """
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(video_path),
        "-vf", f"ass={ass_path}:fontsdir={fonts_dir}",
        "-c:a", "copy",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    subprocess.run(cmd, check=True)


def create_phone_encode(
    video_path: Path,
    output_path: Path,
    max_size_mb: float,
    duration: float,
) -> None:
    """
    Create phone-optimized copy at 540px wide, <5 MB.
    
    Uses 2-pass encoding to hit the target size precisely.
    """
    # Target: 5 MB total
    # Audio: 128 kbps for duration seconds
    audio_kbps = 128
    audio_mb = (audio_kbps * duration) / (8 * 1024)
    
    # Video gets the rest
    target_video_mb = max_size_mb - audio_mb
    target_video_kbps = int((target_video_mb * 8 * 1024) / duration)
    
    # Use 2-pass encoding for precise size control
    with tempfile.TemporaryDirectory() as tmpdir:
        passlog = Path(tmpdir) / "pass"
        
        # Pass 1
        cmd_pass1 = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", str(video_path),
            "-vf", "scale=540:-2",
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", f"{target_video_kbps}k",
            "-pass", "1",
            "-passlogfile", str(passlog),
            "-an",
            "-f", "null",
            "/dev/null"
        ]
        subprocess.run(cmd_pass1, check=True)
        
        # Pass 2
        cmd_pass2 = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-i", str(video_path),
            "-vf", "scale=540:-2",
            "-c:v", "libx264",
            "-preset", "medium",
            "-b:v", f"{target_video_kbps}k",
            "-pass", "2",
            "-passlogfile", str(passlog),
            "-c:a", "aac",
            "-b:a", f"{audio_kbps}k",
            "-movflags", "+faststart",
            str(output_path)
        ]
        subprocess.run(cmd_pass2, check=True)


def measure_loudness_ebur128(audio_path: Path) -> dict[str, float]:
    """
    Measure LUFS and true peak using ffmpeg ebur128 filter.
    
    Parse failures are fatal errors.
    """
    cmd = [
        "ffmpeg", "-v", "info",
        "-i", str(audio_path),
        "-filter:a", "ebur128=peak=true",
        "-f", "null",
        "-"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    stderr = result.stderr
    
    # Parse ebur128 output from stderr
    lufs = None
    lra = None
    peak = None
    
    for line in stderr.split('\n'):
        if 'I:' in line and 'LUFS' in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'I:':
                    try:
                        lufs = float(parts[i + 1])
                    except (IndexError, ValueError):
                        pass
        elif 'LRA:' in line and 'LU' in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'LRA:':
                    try:
                        lra = float(parts[i + 1])
                    except (IndexError, ValueError):
                        pass
        elif 'Peak:' in line and 'dBFS' in line:
            parts = line.split()
            for i, part in enumerate(parts):
                if part == 'Peak:':
                    try:
                        peak = float(parts[i + 1])
                    except (IndexError, ValueError):
                        pass
    
    # Parse failures are errors
    if lufs is None:
        raise ValueError(f"Failed to parse integrated LUFS from ebur128 output")
    if lra is None:
        raise ValueError(f"Failed to parse LRA from ebur128 output")
    if peak is None:
        raise ValueError(f"Failed to parse true peak from ebur128 output")
    
    return {
        "integrated_lufs": lufs,
        "loudness_range": lra,
        "true_peak_dbtp": peak,
    }


def burn_logo_overlay_filter(
    video_path: Path,
    logo_path: Path,
    output_path: Path,
    width_pct: float = 0.62,
    fade_duration: float = 2.5,
    visible_duration: float = 6.5,
) -> None:
    """
    Burn logo overlay onto video using ffmpeg overlay filter.
    
    Logo appears at end: width_pct of frame width, centered, fade-in over fade_duration,
    visible for last visible_duration seconds.
    """
    # Get video duration
    duration = get_video_duration(video_path)
    
    # Logo timing
    logo_start = duration - visible_duration
    
    # Scale logo to width_pct of video width (1080px * width_pct)
    target_width = int(1080 * width_pct)
    
    # Build overlay filter with fade
    filter_complex = (
        f"[1:v]scale={target_width}:-1[logo];"
        f"[logo]fade=t=in:st={logo_start}:d={fade_duration}:alpha=1[logofade];"
        f"[0:v][logofade]overlay=(W-w)/2:(H-h)/2:enable='between(t,{logo_start},{duration})'"
    )
    
    cmd = [
        "ffmpeg", "-y", "-loglevel", "warning",
        "-i", str(video_path),
        "-i", str(logo_path),
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(output_path)
    ]
    
    subprocess.run(cmd, check=True)
