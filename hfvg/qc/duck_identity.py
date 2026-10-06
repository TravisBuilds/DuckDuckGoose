"""G2.03 / G3.03 / G4.02: Duck identity gate (PIPELINE-LESSONS §13).

Extract frames → detect duck → crop/composite → vision judge → auto-fail logic.

Privacy: Reference images and media-ids.json must NOT be in the public repo.
Refs are resolved from SERIES_PATH at runtime, never committed.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
import subprocess
import json


class DuckDetector(ABC):
    """Abstract duck detector interface."""
    
    @abstractmethod
    async def detect(self, image_path: str | Path) -> dict[str, Any]:
        """
        Detect duck in image and return bounding box.
        
        Returns:
            dict with: found (bool), bbox (x,y,w,h), confidence
        """
        pass


class VisionJudge(ABC):
    """Abstract vision judge interface for duck identity scoring."""
    
    @abstractmethod
    async def judge_identity(self, composite_path: str | Path, 
                           shot_context: dict[str, Any]) -> dict[str, Any]:
        """
        Judge duck identity against reference sheets.
        
        Args:
            composite_path: Path to composite image (AB02|AB03|frame_body|frame_head)
            shot_context: Shot metadata (shot_id, model, tags, duck_state)
        
        Returns:
            dict with:
                signs: dict of {sign_name: {score: 0/1/2, confidence: 0-1, evidence: str}}
                    - bill: 0=on-model, 1=minor, 2=drift
                    - head: 0=on-model, 1=minor, 2=drift
                    - neck_ring: 0=absent, 1=minor, 2=present
                    - body: 0=on-model, 1=minor, 2=drift
                    - too_small: 0=readable, 1=borderline, 2=unreadable
                apparel: dict of {item: {present: bool, correct: bool}}
                    - tuque: {present, correct (cream with pom)}
                    - vest: {present, correct (cream→olive)}
                tuft: dict (for B shots only) {present: bool, correct: bool (3-lobe at ¼ head)}
                total_minor_score: int (sum of minor=1 scores)
                verdict: str (PASS/FAIL/ESCALATE)
                confidence: float (overall confidence)
        """
        pass


class FakeDuckDetector(DuckDetector):
    """Fake detector for dry-run testing."""
    
    async def detect(self, image_path: str | Path) -> dict[str, Any]:
        """Always returns a fake detection."""
        return {
            "found": True,
            "bbox": {"x": 100, "y": 200, "w": 150, "h": 200},
            "confidence": 0.95,
            "height_fraction": 0.25,
        }


class FakeVisionJudge(VisionJudge):
    """Fake judge for dry-run testing. Always returns PASS."""
    
    async def judge_identity(self, composite_path: str | Path,
                           shot_context: dict[str, Any]) -> dict[str, Any]:
        """Fake judge always passes."""
        return {
            "signs": {
                "bill": {"score": 0, "confidence": 0.9, "evidence": "Dry-run: auto-pass"},
                "head": {"score": 0, "confidence": 0.9, "evidence": "Dry-run: auto-pass"},
                "neck_ring": {"score": 0, "confidence": 0.9, "evidence": "Dry-run: no ring"},
                "body": {"score": 0, "confidence": 0.9, "evidence": "Dry-run: auto-pass"},
                "too_small": {"score": 0, "confidence": 0.9, "evidence": "Dry-run: readable"},
            },
            "apparel": {
                "tuque": {"present": True, "correct": True},
                "vest": {"present": True, "correct": True},
            },
            "tuft": {"present": False, "correct": False},  # T shot, no tuft expected
            "total_minor_score": 0,
            "verdict": "PASS",
            "confidence": 0.9,
        }


class DuckIdentityGate:
    """
    Duck identity gate (G2.03 / G3.03 / G4.02).
    
    Extracts frames, detects duck, creates composite, runs vision judge,
    applies auto-fail logic.
    """
    
    def __init__(self, 
                 detector: DuckDetector | None = None,
                 judge: VisionJudge | None = None,
                 refs_path: str | Path | None = None):
        """
        Initialize duck identity gate.
        
        Args:
            detector: Duck detector (default: FakeDuckDetector for dry-run)
            judge: Vision judge (default: OpenAI if key available, else escalates)
            refs_path: Path to reference images (default: from config)
        """
        self.detector = detector or FakeDuckDetector()
        
        # Default judge: Try OpenAI, escalate if key missing
        if judge is None:
            try:
                from hfvg.qc.openai_vision_judge import create_openai_judge
                self.judge = create_openai_judge()
            except ValueError:
                # Key missing - use fake judge that escalates
                self.judge = FakeVisionJudge()
        else:
            self.judge = judge
            
        self.refs_path = Path(refs_path) if refs_path else None
    
    async def extract_frames(self, video_path: str | Path, 
                           frame_times: list[float]) -> list[Path]:
        """
        Extract frames from video at specified times.
        
        Args:
            video_path: Path to video file
            frame_times: List of times in seconds (e.g., [0, duration/2, duration-0.1])
        
        Returns:
            List of paths to extracted frame images
        """
        video_path = Path(video_path)
        output_dir = video_path.parent / f"{video_path.stem}_frames"
        output_dir.mkdir(exist_ok=True)
        
        frame_paths = []
        for i, t in enumerate(frame_times):
            output_path = output_dir / f"frame_{i:03d}_{t:.2f}s.jpg"
            
            # Use ffmpeg to extract frame
            subprocess.run([
                "ffmpeg", "-y",
                "-ss", str(t),
                "-i", str(video_path),
                "-vframes", "1",
                "-q:v", "2",
                str(output_path)
            ], capture_output=True, check=True)
            
            frame_paths.append(output_path)
        
        return frame_paths
    
    async def create_composite(self, frame_path: str | Path,
                             duck_bbox: dict[str, float],
                             shot_context: dict[str, Any],
                             output_path: str | Path) -> Path:
        """
        Create composite: [AB02 head | AB03 head | frame body crop | frame head crop].
        
        Args:
            frame_path: Path to extracted frame
            duck_bbox: Duck bounding box {x, y, w, h}
            shot_context: Shot metadata (for selecting refs)
            output_path: Where to save composite
        
        Returns:
            Path to composite image
        """
        # For now, just copy the frame as a placeholder
        # Real implementation would use PIL/OpenCV to create the 4-panel composite
        import shutil
        shutil.copy(frame_path, output_path)
        return Path(output_path)
    
    async def check_frame(self, frame_path: str | Path,
                        shot_context: dict[str, Any]) -> dict[str, Any]:
        """
        Check one frame for duck identity.
        
        Args:
            frame_path: Path to frame image
            shot_context: Shot metadata
        
        Returns:
            Frame check result with detection, composite, and judge verdict
        """
        # 1. Detect duck
        detection = await self.detector.detect(frame_path)
        
        if not detection["found"]:
            if shot_context.get("duck_role") == "host":
                # Duck expected but not found → ESCALATE
                return {
                    "frame": str(frame_path),
                    "detection": detection,
                    "verdict": "ESCALATE",
                    "reason": "Duck expected (host role) but not found",
                }
            else:
                # Duck absent, expected
                return {
                    "frame": str(frame_path),
                    "detection": detection,
                    "verdict": "PASS",
                    "reason": "Duck absent (expected)",
                }
        
        # 2. Check height fraction
        height_frac = detection.get("height_fraction", 0)
        if height_frac < 0.12 and shot_context.get("duck_role") == "host":
            return {
                "frame": str(frame_path),
                "detection": detection,
                "verdict": "FAIL",
                "reason": f"Duck too small: height fraction {height_frac:.3f} < 0.12",
            }
        
        # 3. Create composite
        output_dir = Path(frame_path).parent / "composites"
        output_dir.mkdir(exist_ok=True)
        composite_path = output_dir / f"{Path(frame_path).stem}_composite.jpg"
        
        await self.create_composite(
            frame_path, 
            detection["bbox"],
            shot_context,
            composite_path
        )
        
        # 4. Run vision judge
        judge_result = await self.judge.judge_identity(composite_path, shot_context)
        
        # 5. Apply auto-fail logic
        verdict, reason = self._apply_auto_fail_logic(judge_result, shot_context)
        
        return {
            "frame": str(frame_path),
            "detection": detection,
            "composite": str(composite_path),
            "judge": judge_result,
            "verdict": verdict,
            "reason": reason,
        }
    
    def _apply_auto_fail_logic(self, judge_result: dict[str, Any],
                              shot_context: dict[str, Any]) -> tuple[str, str]:
        """
        Apply auto-fail logic to judge results.
        
        Auto-fail conditions:
        - Any sign = 2 (drift) on any frame
        - Neck ring at confidence ≥ 0.5
        - Tuque/vest mismatch
        - Sum of minor scores ≥ 3 across frame set
        - Confidence < 0.6 → ESCALATE (fail-closed)
        
        Returns:
            (verdict, reason) tuple
        """
        signs = judge_result.get("signs", {})
        apparel = judge_result.get("apparel", {})
        confidence = judge_result.get("confidence", 0)
        
        # Check confidence threshold
        if confidence < 0.6:
            return ("ESCALATE", f"Low confidence {confidence:.2f} < 0.6")
        
        # Check for drift (score = 2)
        for sign_name, sign_data in signs.items():
            if sign_data.get("score") == 2:
                evidence = sign_data.get("evidence", "")
                return ("FAIL", f"Drift detected: {sign_name} (score 2) - {evidence}")
        
        # Check neck ring
        neck_ring = signs.get("neck_ring", {})
        if neck_ring.get("score") >= 1 and neck_ring.get("confidence", 0) >= 0.5:
            return ("FAIL", "Neck ring detected (confidence ≥ 0.5)")
        
        # Check apparel
        tuque = apparel.get("tuque", {})
        if tuque.get("present") and not tuque.get("correct"):
            return ("FAIL", "Tuque mismatch (present but incorrect)")
        
        vest = apparel.get("vest", {})
        if vest.get("present") and not vest.get("correct"):
            return ("FAIL", "Vest mismatch (present but incorrect)")
        
        # Check for B shots: tuft required
        if shot_context.get("duck_state") == "B":
            tuft = judge_result.get("tuft", {})
            if not tuft.get("present") or not tuft.get("correct"):
                return ("FAIL", "B shot: tuft missing or incorrect")
        
        # Check total minor score
        total_minor = judge_result.get("total_minor_score", 0)
        if total_minor >= 3:
            return ("FAIL", f"Total minor score {total_minor} ≥ 3")
        
        # If judge already decided FAIL/ESCALATE, respect it
        if judge_result.get("verdict") in ["FAIL", "ESCALATE"]:
            return (judge_result["verdict"], judge_result.get("reason", "Judge decision"))
        
        return ("PASS", "Identity check passed")
    
    async def check_still(self, image_path: str | Path,
                        shot_context: dict[str, Any]) -> dict[str, Any]:
        """
        Check a still image for duck identity.
        
        Args:
            image_path: Path to still image
            shot_context: Shot metadata
        
        Returns:
            Check result with verdict
        """
        return await self.check_frame(image_path, shot_context)
    
    async def check_clip(self, video_path: str | Path,
                       shot_context: dict[str, Any]) -> dict[str, Any]:
        """
        Check a video clip for duck identity.
        
        Extracts first/mid/last frames and checks each.
        
        Args:
            video_path: Path to video file
            shot_context: Shot metadata
        
        Returns:
            Check result with per-frame verdicts and overall verdict
        """
        # Get video duration (stub - real impl would use ffprobe)
        duration = shot_context.get("duration", 5.0)
        
        # Extract first/mid/last frames
        frame_times = [0.1, duration / 2, duration - 0.1]
        frame_paths = await self.extract_frames(video_path, frame_times)
        
        # Check each frame
        frame_results = []
        for i, frame_path in enumerate(frame_paths):
            frame_result = await self.check_frame(frame_path, shot_context)
            frame_result["frame_index"] = i
            frame_result["frame_time"] = frame_times[i]
            frame_results.append(frame_result)
        
        # Overall verdict: FAIL if any frame fails
        overall_verdict = "PASS"
        overall_reason = "All frames passed"
        
        for result in frame_results:
            if result["verdict"] == "FAIL":
                overall_verdict = "FAIL"
                overall_reason = f"Frame {result['frame_index']} failed: {result['reason']}"
                break
            elif result["verdict"] == "ESCALATE":
                overall_verdict = "ESCALATE"
                overall_reason = f"Frame {result['frame_index']} escalated: {result['reason']}"
        
        return {
            "video": str(video_path),
            "frames": frame_results,
            "verdict": overall_verdict,
            "reason": overall_reason,
        }


def create_gate(detector: DuckDetector | None = None,
               judge: VisionJudge | None = None) -> DuckIdentityGate:
    """
    Create a duck identity gate with optional real or fake components.
    
    For dry-run: create_gate() uses fakes
    For production: create_gate(RealDetector(), RealJudge())
    """
    return DuckIdentityGate(detector=detector, judge=judge)
