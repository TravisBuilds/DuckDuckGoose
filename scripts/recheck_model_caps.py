#!/usr/bin/env python3
"""
Recheck model capabilities against upstream schema drift.

Usage:
    python scripts/recheck_model_caps.py [--fixture path/to/llms-full.txt]

Compares the registry against a saved snapshot of Higgsfield's public docs
dump (https://docs.higgsfield.ai/llms-full.txt). Each endpoint section in that
dump carries "**Endpoint ID:** `<id>`" and a "Complete JSON schema" block; the
script extracts those schemas and diffs maxItems / min/max / enum values
against the registry. A JSON fixture of the form {"models": [{"id", "schema"}]}
is still accepted (used by the unit tests).

Limits whose registry basis is intentionally tighter than upstream
(stricter_*, conservative_*, harness_policy, intersection) only drift when
upstream drops below the registry value, not when upstream is looser.
Endpoints documented upstream but not tracked in the registry are listed as
informational and do not fail the run unless --strict is given.
"""

import argparse
import json
import re
import sys
import textwrap
from pathlib import Path

_SECTION_SPLIT = re.compile(r'(?m)^# (?=[^\n]*\nSource: )')
_ENDPOINT_ID = re.compile(r'\*\*Endpoint ID:\*\* `([^`]+)`')
_SOURCE = re.compile(r'(?m)^Source: (\S+)')
_SCHEMA_BLOCK = re.compile(
    r'Complete JSON schema">\s*\n\s*```json[^\n]*\n(.*?)\n\s*```', re.S)


def parse_llms_full(text: str) -> dict:
    """Extract {"models": [{"id", "source", "schema"}]} from llms-full.txt text."""
    models = []
    for section in _SECTION_SPLIT.split(text):
        m = _ENDPOINT_ID.search(section)
        if not m:
            continue
        block = _SCHEMA_BLOCK.search(section)
        if not block:
            continue
        src = _SOURCE.search(section)
        models.append({
            "id": m.group(1),
            "source": src.group(1) if src else None,
            "schema": json.loads(textwrap.dedent(block.group(1))),
        })
    return {"models": models}


def load_fixture(fixture_path: str) -> dict:
    """Load a JSON fixture or a saved llms-full.txt docs snapshot."""
    with open(fixture_path, 'r', encoding='utf-8') as f:
        text = f.read()
    stripped = text.lstrip()
    if stripped.startswith('{'):
        return json.loads(stripped)
    parsed = parse_llms_full(text)
    if not parsed["models"]:
        raise ValueError(
            f"{fixture_path}: neither JSON nor an llms-full.txt dump with "
            "'Endpoint ID' + 'Complete JSON schema' sections")
    return parsed

def load_registry(registry_path: str) -> dict:
    """Load the model capabilities registry."""
    with open(registry_path, 'r') as f:
        return json.load(f)


# Array fields with maxItems -> registry limit key
_MAX_ITEMS_LIMITS = {
    'image_urls': 'max_refs',
    'images': 'max_refs',
    'video_urls': 'max_videos',
    'audio_urls': 'max_audios',
}
# Enum fields -> registry limit key
_ENUM_LIMITS = {
    'aspect_ratio': 'aspect_ratios',
    'resolution': 'resolutions',
    'quality': 'quality',
    'batch_size': 'batch_size',
}
_TIGHTER_BASES = ('stricter', 'conservative', 'harness_policy', 'intersection')


def _is_tighter(limit: dict) -> bool:
    basis = str(limit.get('basis', ''))
    return basis.startswith(_TIGHTER_BASES)


def list_tighter_limits(registry: dict, fixture: dict) -> list[str]:
    """Informational: registry caps deliberately below the upstream maxItems."""
    notes = []
    fixture_models = {m['id']: m for m in fixture.get('models', [])}
    for model_id, caps in sorted(registry.get('models', {}).items()):
        props = fixture_models.get(model_id, {}).get('schema', {}).get('properties', {})
        for prop_name, key in _MAX_ITEMS_LIMITS.items():
            limit = caps.get('limits', {}).get(key)
            up = props.get(prop_name, {}).get('maxItems')
            if limit and up is not None and _is_tighter(limit):
                value = limit.get('value')
                if isinstance(value, int) and value < up:
                    notes.append(f"{model_id}: {key} registry={value} < upstream={up} "
                                 f"(basis {limit.get('basis')})")
    return notes


def _is_untracked_new(diff: str) -> bool:
    return diff.startswith("New model in fixture")


def check_schema_drift(registry: dict, fixture: dict) -> list[str]:
    """
    Check for schema drift between registry and fixture.
    
    Returns list of drift descriptions.
    """
    diffs = []
    
    # Get models from fixture
    fixture_models = {m['id']: m for m in fixture.get('models', [])}
    registry_models = registry.get('models', {})
    
    # Check for new models in fixture not in registry
    for model_id in fixture_models:
        if model_id not in registry_models:
            diffs.append(f"New model in fixture: {model_id}")
    
    # Check for removed models (only models served through Higgsfield;
    # ElevenLabs and MCP-only entries are not in the Higgsfield docs)
    for model_id in registry_models:
        if model_id not in fixture_models:
            caps = registry_models[model_id]
            if caps.get('provider', 'higgsfield') != 'higgsfield':
                continue
            # Only report if status is active (not deprecated/testing)
            status = caps.get('status', '')
            if 'active' in status:
                diffs.append(f"Active model missing from fixture: {model_id}")
    
    # Check schema field differences for matching models
    for model_id in sorted(set(fixture_models.keys()) & set(registry_models.keys())):
        fixture_model = fixture_models[model_id]
        registry_model = registry_models[model_id]
        fixture_schema = fixture_model.get('schema', {})
        registry_limits = registry_model.get('limits', {})
        props = fixture_schema.get('properties', {})

        for prop_name, prop_schema in props.items():
            # maxItems (ref cap drift and friends)
            if 'maxItems' in prop_schema and prop_name in _MAX_ITEMS_LIMITS:
                key = _MAX_ITEMS_LIMITS[prop_name]
                max_items = prop_schema['maxItems']
                registry_limit = registry_limits.get(key, {})
                if registry_limit:
                    registry_value = registry_limit.get('value')
                    if registry_value != max_items:
                        tighter_ok = (_is_tighter(registry_limit)
                                      and isinstance(registry_value, int)
                                      and registry_value <= max_items)
                        if not tighter_ok:
                            label = "max_refs drift" if key == 'max_refs' else f"{key} drift"
                            diffs.append(
                                f"{model_id}: {label} - "
                                f"registry={registry_value}, fixture={max_items}"
                            )
            # min/max duration
            if prop_name == 'duration' and 'duration_s' in registry_limits:
                reg = registry_limits['duration_s'].get('value') or {}
                for bound, key in (('minimum', 'min'), ('maximum', 'max')):
                    if bound in prop_schema and reg.get(key) != prop_schema[bound]:
                        diffs.append(
                            f"{model_id}: duration_s.{key} drift - "
                            f"registry={reg.get(key)}, fixture={prop_schema[bound]}")
            # enums
            if 'enum' in prop_schema and prop_name in _ENUM_LIMITS:
                key = _ENUM_LIMITS[prop_name]
                registry_limit = registry_limits.get(key)
                if not registry_limit:
                    continue
                reg = registry_limit.get('value')
                up = prop_schema['enum']
                if not isinstance(reg, list):
                    continue
                if _is_tighter(registry_limit):
                    extra = [v for v in reg if v not in up]
                    if extra:
                        diffs.append(f"{model_id}: {key} drift - registry allows "
                                     f"{extra} not in fixture {up}")
                elif sorted(map(str, reg)) != sorted(map(str, up)):
                    diffs.append(f"{model_id}: {key} drift - registry={reg}, fixture={up}")
    
    return diffs

def main():
    parser = argparse.ArgumentParser(description='Recheck model caps against fixture')
    parser.add_argument(
        '--fixture',
        default='tests/fixtures/higgsfield_llms_full_2026-10-19.txt',
        help='Path to a saved https://docs.higgsfield.ai/llms-full.txt snapshot (or JSON fixture)'
    )
    parser.add_argument(
        '--strict',
        action='store_true',
        help='Also fail on upstream endpoints not tracked in the registry'
    )
    parser.add_argument(
        '--registry',
        default='hfvg/data/model_caps.json',
        help='Path to model capabilities registry'
    )
    
    args = parser.parse_args()
    
    # Check paths exist
    registry_path = Path(args.registry)
    fixture_path = Path(args.fixture)
    
    if not registry_path.exists():
        print(f"❌ Registry not found: {registry_path}", file=sys.stderr)
        sys.exit(1)
    
    if not fixture_path.exists():
        print(f"❌ Fixture not found: {fixture_path}", file=sys.stderr)
        sys.exit(1)
    
    # Load data
    try:
        registry = load_registry(str(registry_path))
        fixture = load_fixture(str(fixture_path))
    except Exception as e:
        print(f"❌ Failed to load data: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Check drift
    diffs = check_schema_drift(registry, fixture)
    
    # Report
    print(f"\n{'='*60}")
    print(f"Model Capabilities Drift Check")
    print(f"{'='*60}\n")
    print(f"Registry: {registry_path}")
    print(f"Fixture:  {fixture_path}")
    print(f"Checked:  {registry.get('checked', 'unknown')}\n")
    
    untracked = [d for d in diffs if _is_untracked_new(d)]
    drifts = [d for d in diffs if not _is_untracked_new(d)]
    if args.strict:
        drifts, untracked = diffs, []

    for note in list_tighter_limits(registry, fixture):
        print(f"  (tighter than upstream, not drift) {note}")
    print()

    if untracked:
        print(f"ℹ️  {len(untracked)} upstream endpoint(s) not tracked in the registry "
              f"(informational; --strict to fail):\n")
        for diff in untracked:
            print(f"  • {diff}")
        print()

    if drifts:
        print(f"⚠️  Found {len(drifts)} drift(s):\n")
        for diff in drifts:
            print(f"  • {diff}")
        print(f"\n{'='*60}\n")
        sys.exit(1)
    else:
        print("✓ No drift detected for tracked models.\n")
        print(f"{'='*60}\n")
        sys.exit(0)

if __name__ == '__main__':
    main()
