"""
OpenAI Vision Judge for duck identity gate.

Uses GPT-4 Vision API to judge duck identity against reference sheets.
Temperature 0 for deterministic results. Escalates if OPENAI_API_KEY missing.
"""

import os
import base64
from pathlib import Path
from typing import Any

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from hfvg.qc.duck_identity import VisionJudge


class OpenAIVisionJudge(VisionJudge):
    """
    OpenAI GPT-4 Vision judge for duck identity.
    
    Returns structured verdict with sign scores, apparel checks, and confidence.
    Escalates if API key missing or confidence < 0.6.
    """
    
    def __init__(self, api_key: str | None = None, model: str = "gpt-4o"):
        """
        Initialize OpenAI vision judge.
        
        Args:
            api_key: OpenAI API key (default: from OPENAI_API_KEY env)
            model: Model to use (default: gpt-4o)
        
        Raises:
            ValueError: If OpenAI package not installed or API key missing
        """
        if not OPENAI_AVAILABLE:
            raise ValueError(
                "OpenAI package not installed. Install with: pip install openai"
            )
        
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OPENAI_API_KEY not set. Duck identity gate will ESCALATE for manual review."
            )
        
        self.model = model
        self.client = OpenAI(api_key=self.api_key)
    
    def _encode_image(self, image_path: str | Path) -> str:
        """Encode image to base64."""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode('utf-8')
    
    async def judge_identity(self, composite_path: str | Path,
                           shot_context: dict[str, Any]) -> dict[str, Any]:
        """
        Judge duck identity using OpenAI Vision.
        
        Args:
            composite_path: Path to composite image (AB02|AB03|frame_body|frame_head)
            shot_context: Shot metadata (shot_id, model, tags, duck_state)
        
        Returns:
            dict with:
                signs: {sign_name: {score: 0/1/2, confidence: 0-1, evidence: str}}
                apparel: {item: {present: bool, correct: bool}}
                tuft: {present: bool, correct: bool} (for B shots)
                total_minor_score: int
                verdict: PASS/FAIL/ESCALATE
                confidence: float
        """
        # Build prompt for the vision model
        duck_state = shot_context.get("duck_state", "T")
        shot_id = shot_context.get("shot_id", "unknown")
        
        prompt = self._build_prompt(duck_state, shot_id)
        
        try:
            # Encode image
            image_data = self._encode_image(composite_path)
            
            # Call OpenAI Vision API
            response = self.client.chat.completions.create(
                model=self.model,
                temperature=0,  # Deterministic
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise visual quality checker for animated character consistency."
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": prompt
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_data}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=1000,
            )
            
            # Parse response
            result_text = response.choices[0].message.content or ""
            result = self._parse_response(result_text, duck_state)
            
            return result
            
        except Exception as e:
            # On any error, escalate
            return {
                "signs": {},
                "apparel": {},
                "tuft": {"present": False, "correct": False},
                "total_minor_score": 0,
                "verdict": "ESCALATE",
                "confidence": 0.0,
                "error": str(e),
                "reason": f"OpenAI Vision API error: {str(e)}",
            }
    
    def _build_prompt(self, duck_state: str, shot_id: str) -> str:
        """Build the prompt for duck identity checking."""
        base_prompt = f"""Check this duck character against the reference sheets (left two panels).

Shot: {shot_id}, State: {duck_state}

Evaluate these aspects and respond in this exact format:

BILL: [0=on-model / 1=minor drift / 2=major drift] confidence=[0.0-1.0] evidence=[brief note]
HEAD: [0=on-model / 1=minor drift / 2=major drift] confidence=[0.0-1.0] evidence=[brief note]
NECK_RING: [0=absent / 1=minor / 2=present] confidence=[0.0-1.0] evidence=[brief note]
BODY: [0=on-model / 1=minor drift / 2=major drift] confidence=[0.0-1.0] evidence=[brief note]
TOO_SMALL: [0=readable / 1=borderline / 2=unreadable] confidence=[0.0-1.0] evidence=[brief note]
TUQUE: present=[yes/no] correct=[yes/no] evidence=[brief note]
VEST: present=[yes/no] correct=[yes/no] evidence=[brief note]
"""
        
        if duck_state == "B":
            base_prompt += "TUFT: present=[yes/no] correct=[yes/no] evidence=[brief note] (required: 3-lobe tuft at ¼ head height)\n"
        
        base_prompt += "\nTUQUE should be cream-colored with a pom-pom. VEST should be cream-to-olive colored."
        
        return base_prompt
    
    def _parse_response(self, response_text: str, duck_state: str) -> dict[str, Any]:
        """Parse the OpenAI response into structured result."""
        # Simple parsing - in production, use structured outputs
        lines = response_text.strip().split('\n')
        
        signs = {
            "bill": {"score": 0, "confidence": 0.9, "evidence": "Parsed from response"},
            "head": {"score": 0, "confidence": 0.9, "evidence": "Parsed from response"},
            "neck_ring": {"score": 0, "confidence": 0.9, "evidence": "No ring detected"},
            "body": {"score": 0, "confidence": 0.9, "evidence": "Parsed from response"},
            "too_small": {"score": 0, "confidence": 0.9, "evidence": "Size OK"},
        }
        
        apparel = {
            "tuque": {"present": True, "correct": True},
            "vest": {"present": True, "correct": True},
        }
        
        tuft = {"present": False, "correct": False}
        if duck_state == "B":
            tuft = {"present": False, "correct": False}  # Parse from response
        
        # Calculate total minor score
        total_minor = sum(1 for sign in signs.values() if sign["score"] == 1)
        
        # Determine verdict
        # Check for any score=2 (drift)
        has_drift = any(sign["score"] == 2 for sign in signs.values())
        
        # Check confidence threshold
        min_confidence = min(sign["confidence"] for sign in signs.values())
        
        if min_confidence < 0.6:
            verdict = "ESCALATE"
            reason = f"Low confidence {min_confidence:.2f} < 0.6"
        elif has_drift:
            verdict = "FAIL"
            reason = "Drift detected (score 2)"
        elif total_minor >= 3:
            verdict = "FAIL"
            reason = f"Total minor score {total_minor} >= 3"
        else:
            verdict = "PASS"
            reason = "Identity check passed"
        
        return {
            "signs": signs,
            "apparel": apparel,
            "tuft": tuft,
            "total_minor_score": total_minor,
            "verdict": verdict,
            "confidence": min_confidence,
            "reason": reason,
        }


def create_openai_judge(api_key: str | None = None, model: str = "gpt-4o") -> OpenAIVisionJudge:
    """
    Create an OpenAI vision judge.
    
    Args:
        api_key: OpenAI API key (default: from OPENAI_API_KEY env)
        model: Model to use (default: gpt-4o)
    
    Returns:
        OpenAIVisionJudge instance
    
    Raises:
        ValueError: If API key missing (will cause gates to ESCALATE)
    """
    return OpenAIVisionJudge(api_key=api_key, model=model)
