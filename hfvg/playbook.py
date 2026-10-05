"""Playbook rule validators for production readiness.

Encodes mechanical checks from PIPELINE-LESSONS.md:
- Credit caps per round
- Draft-first tier enforcement
- Same-resort environment plate validation
- Duck-not-in-guest-group tag check
- No upright/carrying poses for guest animals
"""

from typing import Any


class PlaybookViolation(Exception):
    """Raised when a playbook rule is violated."""

    pass


class PlaybookValidator:
    """Validates episode plans against playbook rules."""

    def __init__(self, credit_target: float = 1200.0, credit_cap: float = 1500.0):
        self.credit_target = credit_target
        self.credit_cap = credit_cap
        self.round_spend: dict[str, float] = {}

    def validate_credit_cap(self, round_name: str, proposed_cost: float):
        """
        Validate that round doesn't exceed 80% of cap (stop and report gate).
        
        From §3.5: Stop and report when a round hits 80% of its cap.
        """
        current = self.round_spend.get(round_name, 0.0)
        new_total = current + proposed_cost
        
        cap_80 = self.credit_cap * 0.8
        
        if new_total > cap_80:
            raise PlaybookViolation(
                f"Round {round_name} would exceed 80% cap: "
                f"{new_total:.1f} > {cap_80:.1f} (cap={self.credit_cap}). "
                f"Stop and report before continuing."
            )
        
        if new_total > self.credit_cap:
            raise PlaybookViolation(
                f"Round {round_name} exceeds hard cap: "
                f"{new_total:.1f} > {self.credit_cap}. "
                f"Cannot proceed."
            )
    
    def record_spend(self, round_name: str, cost: float):
        """Record actual spend for a round."""
        self.round_spend[round_name] = self.round_spend.get(round_name, 0.0) + cost
    
    def validate_draft_first(self, shot: dict[str, Any]) -> list[str]:
        """
        Validate draft-first tier enforcement (§5.6).
        
        Rules:
        - Travis only sees drafts that passed QC
        - Finals rendered only for Travis-approved shots
        - No exploratory finals
        """
        errors = []
        
        # Check if this is a final without draft approval
        if shot.get("tier") == "final" and not shot.get("draft_approved"):
            errors.append(
                f"Shot {shot.get('shot_id')}: Cannot render final without "
                f"Travis approval of draft. Draft-first rule (§5.6.2)."
            )
        
        # Check if draft passed QC
        if shot.get("tier") == "draft" and not shot.get("qc_passed"):
            errors.append(
                f"Shot {shot.get('shot_id')}: Draft must pass QC before "
                f"showing to Travis. Draft-first rule (§5.6.1)."
            )
        
        return errors
    
    def validate_same_resort(self, shot: dict[str, Any], world_refs: list[str]) -> list[str]:
        """
        Validate ONE-RESORT RULE (§6).
        
        Every room must be generated FROM locked Ep01/Ep02 resort frames.
        """
        errors = []
        
        # Check if room generation includes world refs
        if shot.get("type") == "room_plate" or "env" in shot.get("tags", []):
            refs = shot.get("refs", [])
            
            # Check if any world refs are included
            has_world_ref = any(
                any(world_ref in ref for world_ref in world_refs)
                for ref in refs
            )
            
            if not has_world_ref and "pre-resort" not in shot.get("tags", []):
                errors.append(
                    f"Shot {shot.get('shot_id')}: Room generation must include "
                    f"world refs from locked resort frames. ONE-RESORT RULE (§6.1)."
                )
        
        return errors
    
    def validate_duck_role(self, shot: dict[str, Any]) -> list[str]:
        """
        Validate duck role rule (§5: duck is host, never fifth guest).
        
        Guest-only shots need hard negative "no duck".
        """
        errors = []
        
        # Check if shot is guest-only
        if "guest-only" in shot.get("tags", []) or shot.get("duck_role") == "absent":
            prompt = shot.get("prompt", "")
            
            # Check for hard negative
            if "no duck" not in prompt.lower():
                errors.append(
                    f"Shot {shot.get('shot_id')}: Guest-only shot must include "
                    f"'no duck' hard negative. Duck-as-host rule (§3.1)."
                )
        
        # Check if duck is in guest pack
        if "duck-in-pack" in shot.get("tags", []):
            errors.append(
                f"Shot {shot.get('shot_id')}: Duck cannot be in guest pack. "
                f"Duck is host, not fifth guest (§5)."
            )
        
        return errors
    
    def validate_animal_poses(self, shot: dict[str, Any]) -> list[str]:
        """
        Validate NO UPRIGHT/CARRYING rule (§7).
        
        Guest animals stay NatGeo-real: quadrupedal, no human-style carrying.
        """
        errors = []
        
        prompt = shot.get("prompt", "").lower()
        tags = shot.get("tags", [])
        
        # Check for anthropomorphic keywords
        forbidden = [
            "standing upright",
            "walking upright", 
            "bipedal",
            "carrying",
            "holding",
            "hand over",
            "handing",
        ]
        
        for keyword in forbidden:
            if keyword in prompt and "guest" in prompt:
                errors.append(
                    f"Shot {shot.get('shot_id')}: Prompt contains '{keyword}' "
                    f"for guest animal. NO UPRIGHT/CARRYING rule (§7)."
                )
        
        # Check that gait is specified for animal shots
        if "animal" in tags or "guest" in tags:
            if "on all fours" not in prompt and "quadrupedal" not in prompt:
                errors.append(
                    f"Shot {shot.get('shot_id')}: Animal shot must specify "
                    f"'on all fours' or 'quadrupedal' gait (§7 process)."
                )
        
        return errors
    
    def validate_shot(
        self, 
        shot: dict[str, Any], 
        world_refs: list[str] | None = None
    ) -> list[str]:
        """
        Run all playbook validations on a shot.
        
        Returns:
            List of error messages (empty if valid)
        """
        errors = []
        
        errors.extend(self.validate_draft_first(shot))
        
        if world_refs:
            errors.extend(self.validate_same_resort(shot, world_refs))
        
        errors.extend(self.validate_duck_role(shot))
        errors.extend(self.validate_animal_poses(shot))
        
        return errors
    
    def validate_moderation_routing(self, shot: dict[str, Any]) -> dict[str, str]:
        """
        Recommend model routing based on content (§3.2).
        
        Returns:
            {"model": recommended_model, "reason": explanation}
        """
        tags = shot.get("tags", [])
        
        # Route wet/mud/pool shots to Kling
        wet_tags = ["wet", "mud", "pool", "soak", "bare", "underwater"]
        if any(tag in tags for tag in wet_tags):
            return {
                "model": "kling_3.0",
                "reason": "Wet/mud content: route to Kling (never blocked in Ep02)"
            }
        
        # Complex multi-reference shots to Seedance
        refs = shot.get("refs", [])
        if len(refs) > 2:
            return {
                "model": "seedance_2.5",
                "reason": "Multi-reference shot: Seedance handles complex refs better"
            }
        
        return {
            "model": "kling_3.0",
            "reason": "Default: Kling for general content"
        }
