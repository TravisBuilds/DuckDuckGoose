"""Plate specs and manifests in the first series' key spelling (accepted for one release by
hfvg.plate_composite; docs/SHOW-PROFILE.md).

Old spelling -> current spelling:

- ``duck_height_frac`` -> ``scale_ref_height_frac``
- ``rules.adult_to_duck`` / ``rules.pup_to_duck`` -> ``rules.role_to_scale_ref`` {"adult": ..., "pup": ...}
- ``rules.adult_back_max_frac`` -> ``rules.back_max_frac`` {"adult": ...}
- layer ``height_frac_of_duck`` -> ``height_frac_of_scale_ref``
- manifest row ``duck_h`` / ``ratio_to_duck`` (and the ``ratio_to_duck`` check) -> ``scale_ref_h`` /
  ``ratio_to_scale_ref``; the writer emits both, so a reader of the old keys keeps working.

A spec in the old spelling is validated exactly as before (same defaults, same messages)."""

from __future__ import annotations

from typing import Any

FRAC_KEY = "duck_height_frac"
LAYER_FRAC_KEY = "height_frac_of_duck"
ROLES = ("adult", "pup")
ROLE_RULE_KEYS = {"adult": "adult_to_duck", "pup": "pup_to_duck"}
BACK_RULE_KEY = "adult_back_max_frac"
DEFAULT_RULES = {"adult_to_duck": [1.8, 2.2], "pup_to_duck": [0.85, 1.15], "adult_back_max_frac": 0.9}
ROLE_MESSAGE = "role must be adult or pup"
OLD_KEYS = {"top": (FRAC_KEY,), "rules": (*ROLE_RULE_KEYS.values(), BACK_RULE_KEY), "layer": (LAYER_FRAC_KEY,)}
# current row/check key -> old key, written next to each other
ROW_ALIASES = {"scale_ref_h": "duck_h", "ratio_to_scale_ref": "ratio_to_duck"}
CHECK_ALIASES = {"ratio_to_scale_ref": "ratio_to_duck"}


def old_keys_used(spec: dict) -> list[str]:
    """The old-spelling keys a spec uses (top level, rules and layers)."""
    rules = spec.get("rules") if isinstance(spec.get("rules"), dict) else {}
    layers = [L for L in spec.get("layers") or [] if isinstance(L, dict)] if isinstance(spec.get("layers"), list) else []
    used = [k for k in OLD_KEYS["top"] if k in spec] + [f"rules.{k}" for k in OLD_KEYS["rules"] if k in rules]
    return used + sorted({f"layers[].{k}" for L in layers for k in OLD_KEYS["layer"] if k in L})


def rules_view(rules: dict) -> tuple[dict[str, Any], dict[str, Any]]:
    """(role -> [lo, hi], role -> back max fraction) of validated old-spelling rules."""
    return ({role: rules[key] for role, key in ROLE_RULE_KEYS.items()}, {"adult": rules[BACK_RULE_KEY]})


def with_old_keys(row: dict, aliases: dict[str, str] = ROW_ALIASES) -> dict:
    """``row`` with each old key next to its current one (same value)."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value
        if key in aliases:
            out[aliases[key]] = value
    return out


def row_ratio(row: dict) -> Any:
    """A manifest row's ratio to the scale reference, from a manifest of either spelling."""
    return row.get("ratio_to_scale_ref", row.get("ratio_to_duck"))
