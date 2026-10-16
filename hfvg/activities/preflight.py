"""Preflight validation activity - check all inputs before spending credits."""

import json
import os
from pathlib import Path
from typing import Any

from temporalio import activity

from hfvg.errors import FatalError


class PreflightError(FatalError):
    """Preflight validation failed - missing or invalid inputs."""

    def __init__(self, message: str, problems: list[str], *args, **kwargs):
        super().__init__(message, *args, **kwargs)
        self.problems = problems


@activity.defn
async def preflight_validate_episode_activity(
    episode_id: str,
    shots_subset: list[str] | None = None,
    is_live: bool = False,
) -> dict[str, Any]:
    """
    Validate all episode inputs before spending credits.
    
    Checks:
    1. Beatmap exists and is parseable
    2. shots.json exists, shot IDs match beatmap
    3. refs.json exists and is valid
    4. prompt_kit.json exists and is valid JSON
    5. All required refs (used in shots) either exist or are in to_generate
    6. Model configs are valid (supported model, valid params)
    7. Live mode: API key is configured
    
    Raises PreflightError with all problems if validation fails.
    """
    from hfvg.episode_parser import parse_beatmap
    
    base_path = Path(f"data/episodes/{episode_id}")
    problems = []
    
    # 1. Check beatmap exists and is parseable
    beatmap_path = base_path / "beatmap.txt"
    if not beatmap_path.exists():
        problems.append(f"Missing required file: beatmap.txt")
        beatmap_shots = set()
    else:
        try:
            beatmap = parse_beatmap(str(beatmap_path))
            beatmap_shots = {shot["shot_id"] for shot in beatmap}
        except Exception as e:
            problems.append(f"Invalid beatmap.txt: {e}")
            beatmap_shots = set()
    
    # 2. Check shots.json exists and IDs match beatmap
    shots_json_path = base_path / "shots.json"
    if not shots_json_path.exists():
        problems.append(f"Missing required file: shots.json")
        shots_data = {}
    else:
        try:
            shots_raw = json.loads(shots_json_path.read_text())
            # shots.json can be dict with shot_id keys or have a "shots" array
            if isinstance(shots_raw, dict) and "shots" in shots_raw:
                shots_data = {s["shot_id"]: s for s in shots_raw["shots"] if "shot_id" in s}
            elif isinstance(shots_raw, dict):
                shots_data = shots_raw
            else:
                problems.append("Invalid shots.json: must be object or object with 'shots' array")
                shots_data = {}
            
            shots_ids = set(shots_data.keys())
            
            if beatmap_shots and shots_ids != beatmap_shots:
                only_beatmap = sorted(beatmap_shots - shots_ids)
                only_shots = sorted(shots_ids - beatmap_shots)
                if only_beatmap:
                    problems.append(f"Shot IDs in beatmap but not shots.json: {only_beatmap}")
                if only_shots:
                    problems.append(f"Shot IDs in shots.json but not beatmap: {only_shots}")
        except Exception as e:
            problems.append(f"Invalid shots.json: {e}")
            shots_data = {}
    
    # 3. Check refs.json exists and is valid
    refs_json_path = base_path / "refs.json"
    if not refs_json_path.exists():
        problems.append(f"Missing required file: refs.json")
        available_refs = set()
        to_generate_refs = set()
    else:
        try:
            refs_raw = json.loads(refs_json_path.read_text())
            available_refs = {ref["key"] for ref in refs_raw.get("available", [])}
            to_generate_refs = {ref["key"] for ref in refs_raw.get("to_generate", [])}
            
            # Check that available refs have existing files
            for ref in refs_raw.get("available", []):
                ref_path = Path(ref["output_path"])
                if not ref_path.exists():
                    problems.append(
                        f"Ref '{ref['key']}' listed as available but file missing: {ref['output_path']}"
                    )
            
            # Check that to_generate refs have prompts
            for ref in refs_raw.get("to_generate", []):
                if not ref.get("prompt"):
                    problems.append(f"Ref '{ref['key']}' in to_generate missing prompt")
        except Exception as e:
            problems.append(f"Invalid refs.json: {e}")
            available_refs = set()
            to_generate_refs = set()
    
    # 4. Check prompt_kit.json exists and is valid JSON
    prompt_kit_path = base_path / "prompt_kit.json"
    if not prompt_kit_path.exists():
        problems.append(f"Missing required file: prompt_kit.json")
    else:
        try:
            json.loads(prompt_kit_path.read_text())
        except Exception as e:
            problems.append(f"Invalid prompt_kit.json: {e}")
    
    # 5. Check that all refs used in shots are available or to_generate
    # Extract ref keys from shot prompts (look for r_* patterns)
    all_available = available_refs | to_generate_refs
    used_refs = set()
    
    for shot_id, shot_data in shots_data.items():
        if shots_subset and shot_id not in shots_subset:
            continue
        
        prompt = shot_data.get("prompt", "")
        # Simple extraction: find r_\w+ patterns
        import re
        refs_in_prompt = re.findall(r'\br_\w+\b', prompt)
        used_refs.update(refs_in_prompt)
    
    missing_refs = used_refs - all_available
    if missing_refs:
        problems.append(
            f"Refs used in shots but not available or to_generate: {sorted(missing_refs)}"
        )
    
    # 6. Validate model configs
    supported_models = {"kling", "higgsfield", "seedance"}  # Add more as supported
    valid_aspect_ratios = {"1:1", "16:9", "9:16", "4:3", "3:4"}
    
    for shot_id, shot_data in shots_data.items():
        if shots_subset and shot_id not in shots_subset:
            continue
        
        params = shot_data.get("params", {})
        model = params.get("model")
        
        if not model:
            problems.append(f"Shot {shot_id}: missing params.model")
        elif model not in supported_models:
            problems.append(f"Shot {shot_id}: unsupported model '{model}'")
        
        aspect_ratio = params.get("aspect_ratio")
        if aspect_ratio and aspect_ratio not in valid_aspect_ratios:
            problems.append(f"Shot {shot_id}: invalid aspect_ratio '{aspect_ratio}'")
        
        duration = params.get("duration")
        if duration is not None and (not isinstance(duration, (int, float)) or duration <= 0):
            problems.append(f"Shot {shot_id}: invalid duration {duration}")
    
    # 7. Live mode: check API key
    if is_live:
        api_key = os.getenv("HIGGSFIELD_API_KEY")
        if not api_key:
            problems.append("Live mode requires HIGGSFIELD_API_KEY environment variable")
    
    # Raise if any problems found
    if problems:
        raise PreflightError(
            f"Episode {episode_id} preflight validation failed with {len(problems)} problem(s):\n"
            + "\n".join(f"  - {p}" for p in problems),
            problems=problems,
        )
    
    activity.logger.info(f"Episode {episode_id} passed preflight validation")
    
    return {
        "status": "passed",
        "episode_id": episode_id,
        "validated_shots": len([s for s in shots_data if not shots_subset or s in shots_subset]),
        "available_refs": len(available_refs),
        "to_generate_refs": len(to_generate_refs),
    }
