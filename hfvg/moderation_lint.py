"""Moderation-risk lint and routing check run before every paid still / clip submit (B5).

Wraps the playbook rules (hfvg.playbook.PlaybookValidator) so they actually run in preflight
instead of only in unit tests:

- ``duck_role``: guest-only shots carry the hard negative, the host never joins the guest pack;
- ``animal_poses``: guest animals stay quadrupedal (no upright / carrying wording);
- ``same_resort``: environment shots include a locked world ref (only when world refs are
  configured with HFVG_PLAYBOOK_WORLD_REFS);
- ``moderation_routing``: moderation-risk content (wet / mud / pool / ...) is recommended to Kling by
  PlaybookValidator.validate_moderation_routing; a clip routed elsewhere is flagged. The existing
  hard routing rule (hfvg.routing.plan_route: never Seedance for ``wet``) is unchanged and still
  refuses on its own.

The draft-first rule is NOT evaluated here: it is a post-QC rule (a draft cannot have passed QC
before it is submitted).

Every rule is ADVISORY by default: violations are recorded (and logged) but never refuse a submit.
HFVG_PLAYBOOK_HARD_RULES (comma-separated rule names) makes chosen rules hard: a live submit with a
hard violation is refused before estimate / reserve / provider call; a dry run records
"would_refuse" and continues. Unknown rule names fail loudly.
"""

from __future__ import annotations

import os
from typing import Any

from hfvg.playbook import PlaybookValidator

HARD_RULES_ENV = "HFVG_PLAYBOOK_HARD_RULES"
WORLD_REFS_ENV = "HFVG_PLAYBOOK_WORLD_REFS"
RULES = ("duck_role", "animal_poses", "same_resort", "moderation_routing")


class ModerationLintError(ValueError):
    """A hard playbook rule was violated on a live submit (or the rule config is invalid)."""


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def hard_rules() -> frozenset[str]:
    """Rules configured hard via HFVG_PLAYBOOK_HARD_RULES (default: none, all advisory)."""
    names = _csv_env(HARD_RULES_ENV)
    unknown = sorted(set(names) - set(RULES))
    if unknown:
        raise ModerationLintError(
            f"{HARD_RULES_ENV} names unknown playbook rules {unknown}; known rules: {list(RULES)}"
        )
    return frozenset(names)


def _model_family(model: str | None) -> str | None:
    if not model:
        return None
    m = str(model).lower()
    for fam in ("kling", "seedance"):
        if m.startswith(fam):
            return fam
    return m


def playbook_view(
    shot_id: str,
    prompt: str,
    shot_meta: dict[str, Any] | None,
    tags: list[str] | None = None,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """Map the harness shot fields onto the dict PlaybookValidator expects."""
    meta = shot_meta or {}
    view_tags = set(tags or [])
    if meta.get("wet"):
        view_tags.add("wet")
    view: dict[str, Any] = {
        "shot_id": shot_id,
        "prompt": prompt or "",
        "tags": sorted(view_tags),
        "refs": list(refs or []),
    }
    # Only an explicit duck_present=false (shots.json) on a shot with characters marks it guest-only;
    # an undeclared duck_present makes no claim (shot_data defaults "duck" to False).
    if meta.get("duck_declared") and meta.get("duck") is False and meta.get("characters"):
        view["duck_role"] = "absent"
    return view


def evaluate(
    shot_id: str,
    prompt: str,
    shot_meta: dict[str, Any] | None,
    *,
    kind: str,
    model: str | None = None,
    tags: list[str] | None = None,
    refs: list[str] | None = None,
    live: bool,
) -> dict[str, Any]:
    """Run the rules and decide. Returns the decision record stored in state:

    {"kind", "rules": {rule: {"violations": [...], "hard": bool}}, "routing": {...},
     "hard_violations": [...], "advisory_violations": [...],
     "decision": "pass" | "advisory" | "refuse" | "would_refuse", "live": bool}

    Live + any hard violation raises ModerationLintError (the record is attached as ``.record``).
    """
    hard = hard_rules()
    v = PlaybookValidator()
    view = playbook_view(shot_id, prompt, shot_meta, tags, refs)
    world_refs = _csv_env(WORLD_REFS_ENV)
    found: dict[str, list[str]] = {
        "duck_role": v.validate_duck_role(view),
        "animal_poses": v.validate_animal_poses(view),
        "same_resort": v.validate_same_resort(view, world_refs) if world_refs else [],
        "moderation_routing": [],
    }
    rec = v.validate_moderation_routing(view)
    routing = {
        "recommended": _model_family(rec.get("model")),
        "reason": rec.get("reason"),
        "actual": _model_family(model),
        "applies": kind == "clip",
    }
    moderation_risk = str(rec.get("reason", "")).lower().startswith("wet")
    routing["moderation_risk"] = moderation_risk
    if kind == "clip" and moderation_risk and routing["actual"] and routing["actual"] != routing["recommended"]:
        found["moderation_routing"].append(
            f"Shot {shot_id}: moderation-risk content routed to {routing['actual']}; "
            f"playbook recommends {routing['recommended']} ({rec.get('reason')})"
        )
    rules = {name: {"violations": msgs, "hard": name in hard} for name, msgs in found.items()}
    hard_v = [m for name, r in rules.items() if r["hard"] for m in r["violations"]]
    adv_v = [m for name, r in rules.items() if not r["hard"] for m in r["violations"]]
    if hard_v:
        decision = "refuse" if live else "would_refuse"
    elif adv_v:
        decision = "advisory"
    else:
        decision = "pass"
    record = {
        "kind": kind,
        "rules": rules,
        "routing": routing,
        "hard_rules": sorted(hard),
        "hard_violations": hard_v,
        "advisory_violations": adv_v,
        "decision": decision,
        "live": live,
    }
    if decision == "refuse":
        err = ModerationLintError("Playbook moderation lint refused: " + "; ".join(hard_v))
        err.record = record  # type: ignore[attr-defined]
        raise err
    return record
