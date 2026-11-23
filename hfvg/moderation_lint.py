"""Moderation-risk lint and routing check run before every paid still / clip submit (B5).

Runs the show's playbook rules (show profile ``playbook.rules``, hfvg.playbook.PlaybookValidator) so
they actually run in preflight instead of only in unit tests, and one rule every show has:

- ``moderation_routing``: moderation-risk content (wet / mud / pool / ...) is recommended to Kling by
  PlaybookValidator.validate_moderation_routing; a clip routed elsewhere is flagged. The existing
  hard routing rule (hfvg.routing.plan_route: never Seedance for ``wet``) is unchanged and still
  refuses on its own.

A show without playbook rules gets only ``moderation_routing``. Episodes read through the legacy
adapter keep the rules (and record keys) they always had (hfvg.compat.legacy_show.PLAYBOOK). A
``world_refs`` rule runs only when its env variable (default HFVG_PLAYBOOK_WORLD_REFS) lists refs.

The draft-first rule is NOT evaluated here: it is a post-QC rule (a draft cannot have passed QC
before it is submitted).

Every rule is ADVISORY by default: violations are recorded (and logged) but never refuse a submit.
The profile's ``playbook.hard`` and HFVG_PLAYBOOK_HARD_RULES (comma-separated rule names) make chosen
rules hard: a live submit with a hard violation is refused before estimate / reserve / provider call;
a dry run records "would_refuse" and continues. Unknown rule names fail loudly.
"""

from __future__ import annotations

import os
from typing import Any

from hfvg.compat import legacy_show
from hfvg.playbook import PlaybookValidator
from hfvg.show_profile import ROUTING_RULE, ShowProfile, ShowProfileError, resolve_show_profile

HARD_RULES_ENV = "HFVG_PLAYBOOK_HARD_RULES"
WORLD_REFS_ENV = "HFVG_PLAYBOOK_WORLD_REFS"


class ModerationLintError(ValueError):
    """A hard playbook rule was violated on a live submit (or the rule config is invalid)."""


def _csv_env(name: str) -> list[str]:
    return [v.strip() for v in os.getenv(name, "").split(",") if v.strip()]


def show_profile(profile: ShowProfile | None = None, episode_id: str | None = None) -> ShowProfile:
    """The profile whose rules run: ``profile``, else the episode's (a show.json that does not load
    raises ModerationLintError), else the legacy adapter's."""
    if profile is not None:
        return profile
    if episode_id:
        try:
            return resolve_show_profile(episode_id)
        except ShowProfileError as e:
            raise ModerationLintError(f"Show profile does not load: {e}") from e
    return legacy_show.series_profile()


def rule_names(profile: ShowProfile) -> list[str]:
    """The record's rule names, in order: the show's playbook rules, then moderation_routing."""
    return [*profile.playbook.rule_ids(), ROUTING_RULE]


def hard_rules(profile: ShowProfile | None = None) -> frozenset[str]:
    """The show's hard rules: its ``playbook.hard`` plus HFVG_PLAYBOOK_HARD_RULES (default: none, all
    advisory). Names the env gives for rules this show does not have are accepted when they are the
    legacy adapter's (one deployment setting serves every show) and ignored."""
    profile = show_profile(profile)
    known = rule_names(profile)
    names = _csv_env(HARD_RULES_ENV)
    unknown = sorted(set(names) - set(known) - set(legacy_show.RULE_IDS))
    if unknown:
        raise ModerationLintError(
            f"{HARD_RULES_ENV} names unknown playbook rules {unknown}; known rules: {known}"
        )
    return frozenset(n for n in names if n in known) | frozenset(profile.playbook.hard)


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
    profile: ShowProfile | None = None,
) -> dict[str, Any]:
    """Map the harness shot fields onto the dict PlaybookValidator expects. A role_exclusion rule's
    role is absent (``absent_roles``) when the shot declares the rule's ``absent_flag`` false on a shot
    with characters; a flag the shot does not declare makes no claim."""
    meta = shot_meta or {}
    view_tags = set(tags or [])
    if meta.get("wet"):
        view_tags.add("wet")
    view: dict[str, Any] = {
        "shot_id": shot_id,
        "prompt": prompt or "",
        "tags": sorted(view_tags),
        "refs": list(refs or []),
        "shot_meta": meta,
    }
    profile = show_profile(profile)
    absent = [r.params["role"] for r in profile.playbook.rules
              if r.type == "role_exclusion" and r.params.get("absent_flag") and meta.get("characters")
              and profile.flag_declared_false(meta, r.params["absent_flag"])]
    if absent:
        view["absent_roles"] = absent
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
    profile: ShowProfile | None = None,
    episode_id: str | None = None,
) -> dict[str, Any]:
    """Run the rules and decide. Returns the decision record stored in state:

    {"kind", "rules": {rule: {"violations": [...], "hard": bool}}, "routing": {...},
     "hard_violations": [...], "advisory_violations": [...],
     "decision": "pass" | "advisory" | "refuse" | "would_refuse", "live": bool}

    The rules are those of ``profile`` (else the episode's show profile, else the legacy adapter's).
    Live + any hard violation raises ModerationLintError (the record is attached as ``.record``).
    """
    profile = show_profile(profile, episode_id)
    hard = hard_rules(profile)
    v = PlaybookValidator(profile=profile)
    view = playbook_view(shot_id, prompt, shot_meta, tags, refs, profile)
    found: dict[str, list[str]] = {}
    for rule in profile.playbook.rules:
        if rule.type == "world_refs":
            world_refs = _csv_env(rule.params.get("env") or WORLD_REFS_ENV)
            found[rule.id] = v.validate_rule(rule, view, world_refs) if world_refs else []
        else:
            found[rule.id] = v.validate_rule(rule, view)
    found[ROUTING_RULE] = []
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
        found[ROUTING_RULE].append(
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
