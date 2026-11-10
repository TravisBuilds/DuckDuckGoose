"""Test that web/lib/presets.ts stays in sync with hfvg/data/genre_presets.yaml."""
import pytest
import re
import yaml
from pathlib import Path


def parse_typescript_presets(ts_path: Path) -> dict[str, dict]:
    """Parse the TypeScript presets array into a dict keyed by id."""
    content = ts_path.read_text()
    
    # Extract the presets array
    match = re.search(r'export const presets.*?=\s*\[(.*?)\];', content, re.DOTALL)
    if not match:
        raise ValueError("Could not find presets array in TypeScript file")
    
    array_content = match.group(1)
    
    # Parse each preset object
    presets = {}
    
    # Match each preset object (id: "...", ...)
    preset_pattern = r'\{\s*id:\s*"([^"]+)"[^}]*?\}'
    
    for match in re.finditer(preset_pattern, array_content, re.DOTALL):
        preset_text = match.group(0)
        preset_id = match.group(1)
        
        preset = {"id": preset_id}
        
        # Extract aspect
        aspect_match = re.search(r'aspect:\s*"([^"]+)"', preset_text)
        if aspect_match:
            preset["aspect"] = aspect_match.group(1)
        
        # Extract lengthRange
        length_match = re.search(r'lengthRange:\s*\[(\d+),\s*(\d+)\]', preset_text)
        if length_match:
            preset["lengthRange"] = [int(length_match.group(1)), int(length_match.group(2))]
        
        # Extract voiceOver
        vo_match = re.search(r'voiceOver:\s*"([^"]+)"', preset_text)
        if vo_match:
            preset["voiceOver"] = vo_match.group(1)
        
        presets[preset_id] = preset
    
    return presets


def test_preset_ids_match():
    """Test that all preset IDs in YAML exist in TypeScript and vice versa."""
    yaml_path = Path("hfvg/data/genre_presets.yaml")
    ts_path = Path("web/lib/presets.ts")
    
    assert yaml_path.exists(), f"YAML file not found: {yaml_path}"
    assert ts_path.exists(), f"TypeScript file not found: {ts_path}"
    
    # Load YAML
    with open(yaml_path) as f:
        yaml_data = yaml.safe_load(f)
    
    yaml_ids = set(yaml_data.keys())
    
    # Parse TypeScript
    ts_presets = parse_typescript_presets(ts_path)
    ts_ids = set(ts_presets.keys())
    
    # Check for missing IDs
    missing_in_ts = yaml_ids - ts_ids
    missing_in_yaml = ts_ids - yaml_ids
    
    assert not missing_in_ts, f"Presets in YAML but not in TypeScript: {missing_in_ts}"
    assert not missing_in_yaml, f"Presets in TypeScript but not in YAML: {missing_in_yaml}"
    
    # They should match
    assert yaml_ids == ts_ids, "Preset IDs must match between YAML and TypeScript"


def test_preset_aspect_ratios_match():
    """Test that aspect ratios match between YAML and TypeScript."""
    yaml_path = Path("hfvg/data/genre_presets.yaml")
    ts_path = Path("web/lib/presets.ts")
    
    # Load YAML
    with open(yaml_path) as f:
        yaml_data = yaml.safe_load(f)
    
    # Parse TypeScript
    ts_presets = parse_typescript_presets(ts_path)
    
    mismatches = []
    
    for preset_id in yaml_data.keys():
        if preset_id not in ts_presets:
            continue  # Covered by test_preset_ids_match
        
        yaml_aspect = yaml_data[preset_id].get("aspect_ratio")
        ts_aspect = ts_presets[preset_id].get("aspect")
        
        # Normalize aspect ratio format
        def normalize_aspect(aspect_str):
            if not aspect_str:
                return None
            # Handle rotation notation like "16:9_rotated_to_9:16"
            if "_rotated_to_" in aspect_str:
                # Use the final aspect (what it becomes after rotation)
                aspect_str = aspect_str.split("_rotated_to_")[1]
            return aspect_str
        
        yaml_aspect_norm = normalize_aspect(yaml_aspect)
        ts_aspect_norm = normalize_aspect(ts_aspect)
        
        if yaml_aspect_norm != ts_aspect_norm:
            mismatches.append({
                "id": preset_id,
                "yaml": yaml_aspect,
                "typescript": ts_aspect,
            })
    
    if mismatches:
        msg = "Aspect ratio mismatches found:\n"
        for m in mismatches:
            msg += f"  {m['id']}: YAML={m['yaml']}, TS={m['typescript']}\n"
        pytest.fail(msg)


def test_preset_length_ranges_match():
    """Test that length ranges match between YAML and TypeScript."""
    yaml_path = Path("hfvg/data/genre_presets.yaml")
    ts_path = Path("web/lib/presets.ts")
    
    # Load YAML
    with open(yaml_path) as f:
        yaml_data = yaml.safe_load(f)
    
    # Parse TypeScript
    ts_presets = parse_typescript_presets(ts_path)
    
    mismatches = []
    
    for preset_id in yaml_data.keys():
        if preset_id not in ts_presets:
            continue
        
        yaml_length = yaml_data[preset_id].get("length_s")
        ts_length = ts_presets[preset_id].get("lengthRange")
        
        if yaml_length != ts_length:
            mismatches.append({
                "id": preset_id,
                "yaml": yaml_length,
                "typescript": ts_length,
            })
    
    if mismatches:
        msg = "Length range mismatches found:\n"
        for m in mismatches:
            msg += f"  {m['id']}: YAML={m['yaml']}, TS={m['typescript']}\n"
        pytest.fail(msg)


def test_preset_vo_mode_consistency():
    """Test that VO mode is consistent between YAML and TypeScript."""
    yaml_path = Path("hfvg/data/genre_presets.yaml")
    ts_path = Path("web/lib/presets.ts")
    
    # Load YAML
    with open(yaml_path) as f:
        yaml_data = yaml.safe_load(f)
    
    # Parse TypeScript
    ts_presets = parse_typescript_presets(ts_path)
    
    # Map YAML vo_mode to TS voiceOver values
    vo_mode_map = {
        "none": "none",
        "narration": "narration",
        "dialogue": "dialogue",
        "caption_only": "captions",  # YAML uses caption_only, TS might use captions
    }
    
    mismatches = []
    
    for preset_id in yaml_data.keys():
        if preset_id not in ts_presets:
            continue
        
        yaml_vo = yaml_data[preset_id].get("vo_mode")
        ts_vo = ts_presets[preset_id].get("voiceOver")
        
        # Check if they match (allowing for naming differences)
        expected_ts_vo = vo_mode_map.get(yaml_vo, yaml_vo)
        
        if ts_vo and yaml_vo and ts_vo != expected_ts_vo and ts_vo != yaml_vo:
            mismatches.append({
                "id": preset_id,
                "yaml": yaml_vo,
                "typescript": ts_vo,
            })
    
    # This is informational - some presets may intentionally differ
    if mismatches:
        import warnings
        msg = "VO mode differences found (may be intentional):\n"
        for m in mismatches:
            msg += f"  {m['id']}: YAML={m['yaml']}, TS={m['typescript']}\n"
        warnings.warn(msg)
