"""The first series' playbook rules under the validator method names they had before show profiles
(hfvg.playbook.PlaybookValidator runs them as data: hfvg.compat.legacy_show.PLAYBOOK)."""

from __future__ import annotations

from typing import Any

from hfvg.compat.legacy_show import PLAYBOOK, series_profile
from hfvg.playbook import PlaybookValidator


class LegacyPlaybookValidator(PlaybookValidator):
    def __init__(self, credit_target: float = 1200.0, credit_cap: float = 1500.0):
        super().__init__(credit_target, credit_cap, profile=series_profile())

    def _rule(self, rule_id: str):
        return next(r for r in PLAYBOOK.rules if r.id == rule_id)

    def validate_same_resort(self, shot: dict[str, Any], world_refs: list[str]) -> list[str]:
        """ONE-RESORT RULE (§6): every room is generated from locked resort frames."""
        return self.validate_rule(self._rule("same_resort"), shot, world_refs)

    def validate_duck_role(self, shot: dict[str, Any]) -> list[str]:
        """The duck is the host, never a fifth guest (§5); guest-only shots carry "no duck"."""
        return self.validate_rule(self._rule("duck_role"), shot)

    def validate_animal_poses(self, shot: dict[str, Any]) -> list[str]:
        """NO UPRIGHT/CARRYING rule (§7): guest animals stay quadrupedal."""
        return self.validate_rule(self._rule("animal_poses"), shot)

    def validate_shot(self, shot: dict[str, Any], world_refs: list[str] | None = None) -> list[str]:
        """Draft-first, then the world refs (when given), the host role and the guest poses."""
        errors = self.validate_draft_first(shot)
        if world_refs:
            errors.extend(self.validate_same_resort(shot, world_refs))
        errors.extend(self.validate_duck_role(shot))
        errors.extend(self.validate_animal_poses(shot))
        return errors
