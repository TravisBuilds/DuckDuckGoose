"""Compatibility shim: the duck host identity gate of the first series (G2.03 / G3.03 / G4.02).

The pipeline judges identity only for locked cast-library characters (hfvg.qc.identity), so a duck
host that is a locked cast character is checked like any other. This module keeps the first
series' gate for the code and tests written against it: ``DUCK_HOST_PROFILE`` holds its rules as
profile data (signs bill, head, neck ring, body, too small; apparel tuque and vest; the tuft in "B"
shots; the 0.12 height floor for the host), ``DuckIdentityGate`` runs them through the generic gate
with a folder of reference images, and its detector and judge interfaces take no criteria.
``create_gate()`` defaults to the always-pass fakes, which are for dry run only: a live check with
them escalates. Nothing in the pipeline calls this gate.

Privacy: Reference images and media-ids.json must NOT be in the public repo.
Refs are resolved from SERIES_PATH at runtime, never committed.
"""

import inspect
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from hfvg.qc.identity import IdentityCriteria, IdentityGate, IdentityProfile

REF_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")


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
    """Fake detector for dry-run testing (also usable as the generic gate's detector)."""

    dry_run_only = True  # a live gate with it escalates

    async def detect(self, image_path: str | Path, criteria: IdentityCriteria | None = None) -> dict[str, Any]:
        """Always returns a fake detection."""
        return {
            "found": True,
            "bbox": {"x": 100, "y": 200, "w": 150, "h": 200},
            "confidence": 0.95,
            "height_fraction": 0.25,
        }


class FakeVisionJudge(VisionJudge):
    """Fake judge for dry-run testing. Always returns PASS (also usable as the generic gate's judge)."""

    dry_run_only = True  # a live gate with it escalates

    async def judge_identity(self, composite_path: str | Path, shot_context: dict[str, Any],
                             criteria: IdentityCriteria | None = None) -> dict[str, Any]:
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


DUCK_HOST_PROFILE = IdentityProfile(
    name="duck_host",
    label="Duck",
    signs={
        "bill": "bill shape and length",
        "head": "head shape",
        "neck_ring": "a white neck ring (0 absent, 2 clearly present)",
        "body": "body shape",
        "too_small": "is the duck big enough to read (0 readable, 1 borderline, 2 unreadable)",
    },
    apparel={"tuque": "cream tuque with a pom", "vest": "vest fading from cream to olive"},
    hint_signs=("neck_ring",),
    state_key="duck_state",
    state_features={"B": ("tuft", "3-lobe tuft at a quarter of the head height")},
    min_height_fraction=0.12,
    presence=("duck_role", "host"),
    presence_note="host role",
)


def _ref_images(refs_path: Path | None) -> tuple[tuple[str, str], ...]:
    if refs_path is None or not refs_path.is_dir():
        return ()
    return tuple(("ref", str(p)) for p in sorted(refs_path.iterdir()) if p.suffix.lower() in REF_SUFFIXES)


def _takes(fn: Any, n: int) -> bool:
    """True when ``fn`` takes ``n`` positional arguments (this gate's detector and judge interfaces
    take one fewer than the generic gate's: no criteria)."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return True
    return len([p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]) >= n


class DuckIdentityGate(IdentityGate):
    """
    Duck identity gate (G2.03 / G3.03 / G4.02).
    
    Extracts frames, detects duck, creates composite, runs vision judge,
    applies auto-fail logic.
    """
    
    def __init__(self, 
                 detector: DuckDetector | None = None,
                 judge: VisionJudge | None = None,
                 refs_path: str | Path | None = None,
                 episode_id: str | None = None):
        """
        Initialize duck identity gate.
        
        Args:
            detector: Duck detector (default: FakeDuckDetector for dry-run)
            judge: Vision judge (default: FakeVisionJudge for dry-run)
            refs_path: Directory of reference images (the composite's sheet panels; default: none)
            episode_id: Episode whose qc_judges budget line pays for the vision judges' calls
        """
        self.refs_path = Path(refs_path) if refs_path else None
        super().__init__(
            detector or FakeDuckDetector(),
            judge or FakeVisionJudge(),
            IdentityCriteria(slug="duck", name="Duck", description="the duck host",
                             sheet=_ref_images(self.refs_path), profile=DUCK_HOST_PROFILE,
                             episode_id=episode_id),
        )

    async def _detect(self, image_path: str | Path) -> dict[str, Any]:
        if _takes(self.detector.detect, 2):
            return await self.detector.detect(image_path, self.criteria)
        return await self.detector.detect(image_path)

    async def _judge(self, composite_path: Path, shot_context: dict[str, Any]) -> dict[str, Any]:
        if _takes(self.judge.judge_identity, 3):
            return await self.judge.judge_identity(composite_path, shot_context, self.criteria)
        return await self.judge.judge_identity(composite_path, shot_context)


def create_gate(detector: DuckDetector | None = None,
               judge: VisionJudge | None = None) -> DuckIdentityGate:
    """
    Create a duck identity gate with optional real or fake components.
    
    For dry-run: create_gate() uses fakes
    For production: create_gate(RealDetector(), RealJudge())
    """
    return DuckIdentityGate(detector=detector, judge=judge)
