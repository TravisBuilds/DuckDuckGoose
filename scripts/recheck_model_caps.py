#!/usr/bin/env python3
"""
Recheck model capabilities against upstream schema drift.

Usage:
    python scripts/recheck_model_caps.py [--fixture path/to/llms.txt]

Compares registry model IDs and basic schema structure against
a saved Higgsfield /llms API response fixture to detect drift.
"""

import argparse
import json
import sys
from pathlib import Path

def load_fixture(fixture_path: str) -> dict:
    """Load and parse the Higgsfield /llms fixture."""
    with open(fixture_path, 'r') as f:
        return json.load(f)

def load_registry(registry_path: str) -> dict:
    """Load the model capabilities registry."""
    with open(registry_path, 'r') as f:
        return json.load(f)

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
    
    # Check for removed models
    for model_id in registry_models:
        if model_id not in fixture_models:
            caps = registry_models[model_id]
            # Only report if status is active (not deprecated/testing)
            status = caps.get('status', '')
            if 'active' in status:
                diffs.append(f"Active model missing from fixture: {model_id}")
    
    # Check schema field differences for matching models
    for model_id in set(fixture_models.keys()) & set(registry_models.keys()):
        fixture_model = fixture_models[model_id]
        registry_model = registry_models[model_id]
        
        # Check if schema structure changed
        fixture_schema = fixture_model.get('schema', {})
        
        # Look for major schema changes
        if 'properties' in fixture_schema:
            fixture_props = set(fixture_schema['properties'].keys())
            
            # Check registry limits against fixture schema
            registry_limits = registry_model.get('limits', {})
            
            # Check for new required fields
            fixture_required = set(fixture_schema.get('required', []))
            
            # Check if maxItems changed (ref cap drift)
            for prop_name, prop_schema in fixture_schema.get('properties', {}).items():
                if 'maxItems' in prop_schema:
                    # This is a potential ref cap field
                    max_items = prop_schema['maxItems']
                    
                    # Check if we have this limit tracked
                    if prop_name == 'image_urls' or prop_name == 'images':
                        registry_limit = registry_limits.get('max_refs', {})
                        if registry_limit:
                            registry_value = registry_limit.get('value')
                            if registry_value != max_items:
                                diffs.append(
                                    f"{model_id}: max_refs drift - "
                                    f"registry={registry_value}, fixture={max_items}"
                                )
    
    return diffs

def main():
    parser = argparse.ArgumentParser(description='Recheck model caps against fixture')
    parser.add_argument(
        '--fixture',
        default='tests/fixtures/higgsfield_llms_full_2026-10-19.txt',
        help='Path to Higgsfield /llms fixture'
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
    
    if diffs:
        print(f"⚠️  Found {len(diffs)} drift(s):\n")
        for diff in diffs:
            print(f"  • {diff}")
        print(f"\n{'='*60}\n")
        sys.exit(1)
    else:
        print("✓ No drift detected. Registry is up-to-date.\n")
        print(f"{'='*60}\n")
        sys.exit(0)

if __name__ == '__main__':
    main()
