"""Animal count QC: duck presence and count verification.

Configurable checks for duck presence/identity and animal count.
Model-dependent checks are behind an interface with deterministic local fallback.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class AnimalCounter(ABC):
    """Abstract interface for animal counting in images/videos."""
    
    @abstractmethod
    async def count_animals(self, media_path: str | Path, 
                          expected_type: str = "duck") -> dict[str, Any]:
        """
        Count animals in media.
        
        Args:
            media_path: Path to image or video file
            expected_type: Expected animal type (duck, cat, etc.)
        
        Returns:
            dict with:
                - found: bool (any animals detected)
                - count: int (number detected)
                - confidence: float (0-1)
                - type_match: bool (detected type matches expected)
        """
        pass


class FakeAnimalCounter(AnimalCounter):
    """Fake counter for deterministic testing (always passes)."""
    
    async def count_animals(self, media_path: str | Path,
                          expected_type: str = "duck") -> dict[str, Any]:
        """Fake counter: always returns expected count of 1."""
        return {
            "found": True,
            "count": 1,
            "confidence": 0.95,
            "type_match": True,
            "method": "fake_deterministic",
        }


class DuckPresenceCheck:
    """
    Check for duck presence and count in stills/clips.
    
    Falls back to deterministic fake when real counter is unavailable.
    Escalates to human review when confidence is low.
    """
    
    def __init__(self, counter: AnimalCounter | None = None,
                 confidence_threshold: float = 0.7):
        """
        Initialize duck presence checker.
        
        Args:
            counter: Animal counter (default: FakeAnimalCounter for tests)
            confidence_threshold: Minimum confidence for auto-approval
        """
        self.counter = counter or FakeAnimalCounter()
        self.confidence_threshold = confidence_threshold
    
    async def check_still(self, image_path: str | Path,
                        expected_count: int = 1,
                        shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Check duck presence in a still image.
        
        Args:
            image_path: Path to still image
            expected_count: Expected number of ducks
            shot_context: Optional shot metadata
        
        Returns:
            dict with:
                - passed: bool
                - escalate: bool (needs human review)
                - reason: str
                - details: dict (counter results)
        """
        if not Path(image_path).exists():
            return {
                "passed": False,
                "escalate": True,
                "reason": f"Image not found: {image_path}",
                "details": {},
            }
        
        # Run counter
        result = await self.counter.count_animals(image_path, "duck")
        
        # Check confidence
        if result["confidence"] < self.confidence_threshold:
            return {
                "passed": False,
                "escalate": True,
                "reason": f"Low confidence {result['confidence']:.2f} < {self.confidence_threshold}",
                "details": result,
            }
        
        # Check type match
        if not result["type_match"]:
            return {
                "passed": False,
                "escalate": False,
                "reason": f"Wrong animal type (expected duck)",
                "details": result,
            }
        
        # Check count
        if result["count"] != expected_count:
            return {
                "passed": False,
                "escalate": False,
                "reason": f"Duck count mismatch: expected {expected_count}, got {result['count']}",
                "details": result,
            }
        
        return {
            "passed": True,
            "escalate": False,
            "reason": f"Duck presence confirmed (count={result['count']})",
            "details": result,
        }
    
    async def check_clip(self, video_path: str | Path,
                       expected_count: int = 1,
                       shot_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """
        Check duck presence in a video clip.
        
        For now, extracts first frame and checks that.
        Full implementation would check multiple frames.
        
        Args:
            video_path: Path to video file
            expected_count: Expected number of ducks
            shot_context: Optional shot metadata
        
        Returns:
            dict with passed, escalate, reason, details
        """
        if not Path(video_path).exists():
            return {
                "passed": False,
                "escalate": True,
                "reason": f"Video not found: {video_path}",
                "details": {},
            }
        
        # For now, just run on the video file directly
        # Real implementation would extract frames first
        result = await self.counter.count_animals(video_path, "duck")
        
        if result["confidence"] < self.confidence_threshold:
            return {
                "passed": False,
                "escalate": True,
                "reason": f"Low confidence {result['confidence']:.2f}",
                "details": result,
            }
        
        if not result["type_match"]:
            return {
                "passed": False,
                "escalate": False,
                "reason": "Wrong animal type",
                "details": result,
            }
        
        if result["count"] != expected_count:
            return {
                "passed": False,
                "escalate": False,
                "reason": f"Duck count mismatch: expected {expected_count}, got {result['count']}",
                "details": result,
            }
        
        return {
            "passed": True,
            "escalate": False,
            "reason": f"Duck presence confirmed (count={result['count']})",
            "details": result,
        }


def create_duck_check(counter: AnimalCounter | None = None,
                     confidence_threshold: float = 0.7) -> DuckPresenceCheck:
    """
    Create a duck presence checker.
    
    For tests: create_duck_check() uses fake deterministic counter
    For production: create_duck_check(RealCounter()) uses real model
    """
    return DuckPresenceCheck(counter=counter, confidence_threshold=confidence_threshold)
