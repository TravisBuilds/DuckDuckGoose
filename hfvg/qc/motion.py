"""G4.10: No still-only shots gate (HARNESS-GATES v1.1).

Motion analysis via frame difference on mute and final cuts.
FAIL if peak_fd < 3.0 AND mean_fd < 0.15
WATCH if peak_fd < 6.0 OR mean_fd < 0.25
"""

import asyncio
import json
import subprocess
import numpy as np
from pathlib import Path
from typing import Any

from hfvg import subproc


# Thresholds from the original stillgate tool (G4.10)
FAIL_PEAK = 3.0
FAIL_MEAN = 0.15
WATCH_PEAK = 6.0
WATCH_MEAN = 0.25


class MotionCheckResult:
    """Result of a per-shot motion check."""
    
    def __init__(self, shot_id: str, t0: float, t1: float, 
                 mean_fd: float, peak_fd: float, verdict: str):
        """Initialize motion check result."""
        self.shot_id = shot_id
        self.t0 = t0
        self.t1 = t1
        self.mean_fd = mean_fd
        self.peak_fd = peak_fd
        self.verdict = verdict  # PASS, WATCH, or FAIL
    
    def is_pass(self) -> bool:
        """Check if motion check passed."""
        return self.verdict == "PASS"
    
    def is_fail(self) -> bool:
        """Check if motion check failed (still-only)."""
        return self.verdict == "FAIL"
    
    def is_watch(self) -> bool:
        """Check if motion check needs judge review."""
        return self.verdict == "WATCH"
    
    def __repr__(self):
        return (f"MotionCheckResult({self.shot_id}, "
                f"{self.t0:.2f}-{self.t1:.2f}s, "
                f"mean={self.mean_fd:.3f}, peak={self.peak_fd:.2f}, "
                f"{self.verdict})")


def _stillgate_decode(mute_path: str | Path, manifest: dict[str, Any]) -> tuple[list[str], int, int, int]:
    """The stillgate's ffmpeg decode of a mute cut: 180x320 gray (a landscape edit: 320 wide at its own
    shape, e.g. 320x180 for 16:9) at the analysis rate. Returns (cmd, W, H, fps)."""
    W, H = 180, 320
    fmt = manifest.get("format") or {}
    if fmt.get("width", 0) > fmt.get("height", 0):
        W, H = 320, max(2, int(round(320 * fmt["height"] / fmt["width"] / 2)) * 2)
    fps = 24
    
    # Decode video to grayscale, resampled to the analysis rate: segment windows are indexed as
    # seconds * fps (_stillgate_rows), so the decoded stream MUST be at that rate (the picture edit
    # is 30 fps; decoding it natively would shift every window after the first shot).
    cmd = [
        "ffmpeg",
        "-v", "error",
        "-i", str(mute_path),
        "-vf", f"fps={fps},scale={W}:{H}",
        "-f", "rawvideo",
        "-pix_fmt", "gray",
        "-"
    ]
    return cmd, W, H, fps


def _run_stillgate_analysis(mute_path: str | Path, manifest: dict[str, Any]) -> list[dict]:
    """
    Run stillgate motion analysis on a mute cut (direct implementation).
    
    Decode at 180x320 gray (a landscape edit: 320 wide at its own shape, e.g. 320x180 for 16:9),
    consecutive-frame |diff| (dissolve overlaps excluded).
    
    Returns:
        List of per-shot dicts with id, t0, t1, mean_fd, peak_fd, verdict
    """
    cmd, W, H, fps = _stillgate_decode(mute_path, manifest)
    result = subprocess.run(cmd, capture_output=True, check=True)
    return _stillgate_rows(result.stdout, W, H, fps, manifest)


def _stillgate_rows(raw: bytes, W: int, H: int, fps: int, manifest: dict[str, Any]) -> list[dict]:
    """Per-shot consecutive-frame |diff| of the decoded frames (dissolve overlaps excluded)."""
    frames = np.frombuffer(raw, np.uint8).reshape(-1, H, W).astype(np.int16)
    
    # Build timeline
    dissolves = manifest.get("dissolves", {})
    t = 0
    rows = []
    for clip in manifest["clips"]:
        s = t
        e = t + clip["seg_s"]
        rows.append((clip["id"], s, e))
        t = e - dissolves.get(clip["id"], 0)
    
    # Analyze each shot
    results = []
    for i, (shot_id, s, e) in enumerate(rows):
        # Exclude dissolve overlap regions
        a = s if i == 0 else max(s, rows[i - 1][2])
        b = e if i + 1 == len(rows) else min(e, rows[i + 1][1])
        
        # Extract segment frames
        seg = frames[int(round(a * fps)) + 1 : int(round(b * fps)) - 1]
        
        if len(seg) <= 1:
            # Not enough frames for analysis
            results.append({
                "id": shot_id,
                "t0": round(a, 2),
                "t1": round(b, 2),
                "mean_fd": 0.0,
                "peak_fd": 0.0,
                "verdict": "WATCH",
            })
            continue
        
        # Frame differences
        diffs = np.abs(np.diff(seg, axis=0)).reshape(len(seg) - 1, -1)
        mean_fd = float(diffs.mean())
        peak_fd = float(np.median(np.percentile(diffs, 99.5, axis=1)))
        
        # Verdict
        if peak_fd < FAIL_PEAK and mean_fd < FAIL_MEAN:
            verdict = "FAIL"
        elif peak_fd < WATCH_PEAK or mean_fd < WATCH_MEAN:
            verdict = "WATCH"
        else:
            verdict = "PASS"
        
        results.append({
            "id": shot_id,
            "t0": round(a, 2),
            "t1": round(b, 2),
            "mean_fd": round(mean_fd, 3),
            "peak_fd": round(peak_fd, 2),
            "verdict": verdict,
        })
    
    return results


def check_motion(mute_path: str | Path, manifest: dict[str, Any]) -> list[MotionCheckResult]:
    """
    Run G4.10 motion check on a mute cut.
    
    Args:
        mute_path: Path to mute video file
        manifest: Manifest dict with clips[] (id, seg_s) and dissolves{}
    
    Returns:
        List of MotionCheckResult, one per shot
    
    Raises:
        RuntimeError: If analysis fails
    """
    if not Path(mute_path).exists():
        raise FileNotFoundError(f"Mute video not found: {mute_path}")
    
    # Run analysis
    try:
        return _results(_run_stillgate_analysis(mute_path, manifest))
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Motion analysis failed: {e.stderr}") from e
    except Exception as e:
        raise RuntimeError(f"Motion analysis failed: {e}")


async def check_motion_async(mute_path: str | Path, manifest: dict[str, Any]) -> list[MotionCheckResult]:
    """check_motion for activities: the decode is a child of the event loop (hfvg.subproc: heartbeats
    while it runs, killed with a cancelled attempt), the frame analysis runs in a worker thread."""
    if not Path(mute_path).exists():
        raise FileNotFoundError(f"Mute video not found: {mute_path}")
    cmd, W, H, fps = _stillgate_decode(mute_path, manifest)
    try:
        code, out, err = await subproc.run(cmd, f"stillgate decode {Path(mute_path).name}")
        if code != 0:
            raise RuntimeError(err.decode(errors="replace")[-800:])
        rows = await subproc.keep_alive(asyncio.to_thread(_stillgate_rows, out, W, H, fps, manifest),
                                        "stillgate analysis")
    except Exception as e:
        raise RuntimeError(f"Motion analysis failed: {e}") from e
    return _results(rows)


def _results(raw_results: list[dict]) -> list[MotionCheckResult]:
    return [MotionCheckResult(shot_id=r["id"], t0=r["t0"], t1=r["t1"], mean_fd=r["mean_fd"],
                              peak_fd=r["peak_fd"], verdict=r["verdict"]) for r in raw_results]


def check_motion_with_json(mute_path: str | Path, manifest_path: str | Path,
                          output_path: str | Path | None = None) -> list[MotionCheckResult]:
    """
    Run G4.10 motion check with manifest and output JSON files.
    
    Args:
        mute_path: Path to mute video file
        manifest_path: Path to manifest JSON file
        output_path: Optional path to write results JSON
    
    Returns:
        List of MotionCheckResult
    """
    # Load manifest
    with open(manifest_path) as f:
        manifest = json.load(f)
    
    # Run check
    results = check_motion(mute_path, manifest)
    
    # Write output JSON if requested
    if output_path:
        output_data = [
            {
                "id": r.shot_id,
                "t0": round(r.t0, 2),
                "t1": round(r.t1, 2),
                "mean_fd": round(r.mean_fd, 3),
                "peak_fd": round(r.peak_fd, 2),
                "verdict": r.verdict,
            }
            for r in results
        ]
        
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(output_data, f, indent=1)
    
    return results


def create_manifest(clips: list[dict[str, Any]], dissolves: dict[str, float] | None = None) -> dict[str, Any]:
    """
    Create a manifest dict for the stillgate analysis.
    
    Args:
        clips: List of clip dicts with 'id' and 'seg_s' (duration in seconds)
        dissolves: Optional dict of {shot_id: dissolve_duration_s}
    
    Returns:
        Manifest dict with clips[] and dissolves{}
    """
    return {
        "clips": clips,
        "dissolves": dissolves or {}
    }
