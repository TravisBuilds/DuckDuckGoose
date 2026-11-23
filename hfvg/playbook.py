"""Playbook rule validators for production readiness.

Encodes mechanical checks from PIPELINE-LESSONS.md:
- Credit caps per round
- Draft-first tier enforcement
- Moderation-risk routing recommendation
- The show's own playbook rules (show profile ``playbook.rules``, hfvg.show_profile), each of one type:
  ``role_exclusion`` (a shot without a role carries a hard negative; tags that may never hold the
  role), ``pose_words`` (words a prompt may not use near another; words a tagged shot must use) and
  ``world_refs`` (environment shots include a locked world ref). Messages are the show's.
"""

from typing import Any

from hfvg.show_profile import ALWAYS, PlaybookRule, ShowProfile, evaluate_when


class PlaybookViolation(Exception):
    """Raised when a playbook rule is violated."""

    pass


class PlaybookValidator:
    """Validates episode plans against playbook rules (the show's rules: ``profile``)."""

    def __init__(self, credit_target: float = 1200.0, credit_cap: float = 1500.0,
                 profile: ShowProfile | None = None):
        self.credit_target = credit_target
        self.credit_cap = credit_cap
        self.round_spend: dict[str, float] = {}
        self.profile = profile

    def rules(self) -> tuple[PlaybookRule, ...]:
        return self.profile.playbook.rules if self.profile is not None else ()

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
        - Project owner only sees drafts that passed QC
        - Finals rendered only for owner-approved shots
        - No exploratory finals
        """
        errors = []
        
        # Check if this is a final without draft approval
        if shot.get("tier") == "final" and not shot.get("draft_approved"):
            errors.append(
                f"Shot {shot.get('shot_id')}: Cannot render final without "
                f"Project owner approval of draft. Draft-first rule (§5.6.2)."
            )
        
        # Check if draft passed QC
        if shot.get("tier") == "draft" and not shot.get("qc_passed"):
            errors.append(
                f"Shot {shot.get('shot_id')}: Draft must pass QC before "
                f"showing to project owner. Draft-first rule (§5.6.1)."
            )
        
        return errors

    def validate_rule(self, rule: PlaybookRule, shot: dict[str, Any],
                      world_refs: list[str] | None = None) -> list[str]:
        """One show rule on one shot (a playbook view: shot_id, prompt, tags, refs, optional type,
        shot_meta and absent_roles). ``world_refs``: the locked world refs a ``world_refs`` rule checks."""
        check = {"role_exclusion": self._role_exclusion, "pose_words": self._pose_words,
                 "world_refs": self._world_refs}[rule.type]
        return check(rule, shot, world_refs or [])

    def role_absent(self, rule: PlaybookRule, shot: dict[str, Any]) -> bool:
        """The shot declares the rule's role absent: an absent tag, ``absent_roles`` (set by
        hfvg.moderation_lint.playbook_view from the role's flag) or the role key the show's beatmap
        codes write (with its ``absent`` value)."""
        role = rule.params.get("role", "")
        tags = shot.get("tags", [])
        if any(t in tags for t in rule.params.get("absent_tags", [])) or role in shot.get("absent_roles", []):
            return True
        codes = self.profile.character_codes.values() if self.profile is not None else ()
        return any(c.role == role and c.key and shot.get(c.key) == c.absent for c in codes)

    def _role_exclusion(self, rule: PlaybookRule, shot: dict[str, Any], world_refs: list[str]) -> list[str]:
        errors = []
        text = rule.params.get("require_text", "")
        if self.role_absent(rule, shot) and text and text.lower() not in shot.get("prompt", "").lower():
            errors.append(rule.messages["message"].format(shot_id=shot.get("shot_id"), word=text))
        forbidden = [t for t in rule.params.get("forbid_tags", []) if t in shot.get("tags", [])]
        if forbidden:
            errors.append(rule.messages["forbid_message"].format(shot_id=shot.get("shot_id"), word=forbidden[0]))
        return errors

    def _pose_words(self, rule: PlaybookRule, shot: dict[str, Any], world_refs: list[str]) -> list[str]:
        p = rule.params
        meta, tags = shot.get("shot_meta") or {}, shot.get("tags", [])
        if not evaluate_when(p.get("when", ALWAYS), meta, tags):
            return []
        errors = []
        prompt = shot.get("prompt", "").lower()
        near = p.get("forbid_near", "").lower()
        for word in p.get("words", []):
            if word.lower() in prompt and near in prompt:
                errors.append(rule.messages["message"].format(shot_id=shot.get("shot_id"), word=word))
        required = p.get("require_any", [])
        if required and evaluate_when(p.get("require_when", ALWAYS), meta, tags):
            if not any(r.lower() in prompt for r in required):
                errors.append(rule.messages["require_message"].format(shot_id=shot.get("shot_id"), word=required[0]))
        return errors

    def _world_refs(self, rule: PlaybookRule, shot: dict[str, Any], world_refs: list[str]) -> list[str]:
        p = rule.params
        tags = shot.get("tags", [])
        if shot.get("type") not in p.get("types", []) and not any(t in tags for t in p.get("tags", [])):
            return []
        has_world_ref = any(any(world_ref in ref for world_ref in world_refs) for ref in shot.get("refs", []))
        if has_world_ref or any(t in tags for t in p.get("exempt_tags", [])):
            return []
        return [rule.messages["message"].format(shot_id=shot.get("shot_id"), word="")]

    def validate_shot(
        self, 
        shot: dict[str, Any], 
        world_refs: list[str] | None = None
    ) -> list[str]:
        """
        Run all playbook validations on a shot: draft-first, then the show's rules in order (a
        ``world_refs`` rule only when ``world_refs`` are given).
        
        Returns:
            List of error messages (empty if valid)
        """
        errors = self.validate_draft_first(shot)
        for rule in self.rules():
            if rule.type != "world_refs" or world_refs:
                errors.extend(self.validate_rule(rule, shot, world_refs))
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
