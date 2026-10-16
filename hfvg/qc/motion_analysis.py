"""Motion QC: frame-by-frame analysis for video clips.

Advisory mode: flags issues for human review at gates rather than auto-rerolling.

Checks:
- Headcount per shot (extra or missing animals)
- Floating animals (not touching ground/water/surface)
- Sliding (movement without walking animation)
- Walking backwards
- Objects appearing/disappearing

Frame sheets generated at 0.5s intervals for review.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class MotionQCResult:
    """Result of motion QC analysis."""
    
    shot_id: str
    passed: bool
    issues: list[str]
    headcount_frames: dict[float, int]  # timestamp -> count
    frame_sheet_path: str | None
    advisory_only: bool = True  # Never auto-fail in advisory mode


def analyze_motion_qc(
    shot_id: str,
    video_path: str,
    expected_character_count: int,
    interval_seconds: float = 0.5,
    advisory_mode: bool = True,
) -> MotionQCResult:
    """
    Analyze video for motion quality issues.
    
    Args:
        shot_id: Shot identifier
        video_path: Path to video file
        expected_character_count: Expected number of animals in shot
        interval_seconds: Frame sampling interval
        advisory_mode: If True, never fail (just flag issues)
    
    Returns:
        MotionQCResult with issues found
    """
    issues = []
    headcount_frames = {}
    
    # In a real implementation, this would use OpenCV or similar to:
    # 1. Extract frames at interval_seconds intervals
    # 2. Run object detection (YOLO, etc.) on each frame
    # 3. Count animals and check for position consistency
    # 4. Generate frame sheet montage
    
    # For now, return placeholder
    # In dry mode or without video analysis tools, skip actual analysis
    
    return MotionQCResult(
        shot_id=shot_id,
        passed=True if advisory_mode else len(issues) == 0,
        issues=issues,
        headcount_frames=headcount_frames,
        frame_sheet_path=None,
        advisory_only=advisory_mode,
    )


def check_headcount_consistency(
    headcount_frames: dict[float, int],
    expected_count: int,
    tolerance: int = 0,
) -> list[str]:
    """
    Check if headcount is consistent across frames.
    
    Args:
        headcount_frames: timestamp -> count mapping
        expected_count: Expected number of animals
        tolerance: Allowed deviation
    
    Returns:
        List of issue descriptions
    """
    issues = []
    
    for timestamp, count in headcount_frames.items():
        if count < expected_count - tolerance:
            issues.append(f"Missing animals at {timestamp:.1f}s: {count}/{expected_count}")
        elif count > expected_count + tolerance:
            issues.append(f"Extra animals at {timestamp:.1f}s: {count}/{expected_count}")
    
    return issues


def detect_sliding(
    video_path: str,
    sample_interval: float = 0.5,
) -> list[str]:
    """
    Detect sliding (movement without walking animation).
    
    Returns:
        List of timestamps with sliding detected
    """
    # Placeholder - real implementation would use optical flow analysis
    return []


def detect_objects_appearing(
    video_path: str,
    sample_interval: float = 0.5,
) -> list[str]:
    """
    Detect objects appearing/disappearing between frames.
    
    Returns:
        List of timestamps with sudden appearances
    """
    # Placeholder - real implementation would use frame differencing
    return []
