"""G4.10: No still-only shots gate (HARNESS-GATES v1.1).

Wraps /workspace/stillgate.py to check per-shot motion on mute and final.
FAIL if peak_fd < 3.0 AND mean_fd < 0.15
WATCH if peak_fd < 6.0 OR mean_fd < 0.25
"""

import json
import subprocess
from pathlib import Path
from typing import Any


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


def check_motion(mute_path: str | Path, manifest: dict[str, Any]) -> list[MotionCheckResult]:
    """
    Run G4.10 motion check on a mute cut.
    
    Args:
        mute_path: Path to mute video file
        manifest: Manifest dict with clips[] (id, seg_s) and dissolves{}
    
    Returns:
        List of MotionCheckResult, one per shot
    
    Raises:
        RuntimeError: If stillgate.py fails
    """
    stillgate_path = Path("/workspace/stillgate.py")
    if not stillgate_path.exists():
        raise FileNotFoundError(f"stillgate.py not found at {stillgate_path}")
    
    # Write manifest to temp file for stillgate.py
    import tempfile
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(manifest, f)
        manifest_path = f.name
    
    try:
        # Run stillgate.py
        result = subprocess.run(
            ["python3", str(stillgate_path), str(mute_path), manifest_path],
            capture_output=True,
            text=True,
            check=True
        )
        
        # Parse output (one line per shot)
        results = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            
            # Parse: "O03    0.00-   2.92 mean  0.083 peak   1.00 FAIL"
            parts = line.split()
            if len(parts) < 9:
                continue
            
            shot_id = parts[0]
            t0 = float(parts[1])
            t1 = float(parts[2])
            mean_fd = float(parts[4])
            peak_fd = float(parts[6])
            verdict = parts[7]
            
            results.append(MotionCheckResult(shot_id, t0, t1, mean_fd, peak_fd, verdict))
        
        return results
        
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"stillgate.py failed: {e.stderr}") from e
    
    finally:
        # Cleanup temp manifest
        try:
            Path(manifest_path).unlink()
        except:
            pass


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
    stillgate_path = Path("/workspace/stillgate.py")
    if not stillgate_path.exists():
        raise FileNotFoundError(f"stillgate.py not found at {stillgate_path}")
    
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
        
        with open(output_path, 'w') as f:
            json.dump(output_data, f, indent=1)
    
    return results


def create_manifest(clips: list[dict[str, Any]], dissolves: dict[str, float] | None = None) -> dict[str, Any]:
    """
    Create a manifest dict for stillgate.py.
    
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
